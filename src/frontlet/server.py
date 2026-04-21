"""Frontlet: a minimal Front MCP server.

Exposes tools over stdio so an AI agent can:
  - list conversations (with Front's search syntax for filtering)
  - fetch one conversation's metadata (subject, status, tags, last message)
  - page through the messages on a conversation (newest-first by default)
  - page through internal teammate comments on a conversation
  - list all workspace tags (for query-building)
  - download an attachment to a local temp path
  - create and edit draft messages for human review

Auth: reads FRONT_API_TOKEN from the environment at startup.
Optional: FRONT_SENDING_CHANNEL_ID — the default channel for draft creation
(e.g. alt:address:support@yourcompany.com).
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
import unicodedata
from pathlib import Path
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

FRONT_API_BASE = "https://api2.frontapp.com"
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024  # 10 MB

SENDING_CHANNEL_ID = os.environ.get("FRONT_SENDING_CHANNEL_ID")

API_TOKEN = os.environ.get("FRONT_API_TOKEN")
if not API_TOKEN:
    print(
        "frontlet: FRONT_API_TOKEN environment variable is required.\n"
        "Get a token from Front Settings -> Company -> Developers -> API tokens, "
        "then set it in your MCP client config (e.g. Claude Desktop's 'env' block).",
        file=sys.stderr,
    )
    sys.exit(1)

mcp = FastMCP("frontlet")

_client: httpx.AsyncClient | None = None


def _http() -> httpx.AsyncClient:
    """Lazy-construct a shared async HTTP client."""
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=FRONT_API_BASE,
            headers={"Authorization": f"Bearer {API_TOKEN}"},
            timeout=httpx.Timeout(30.0, connect=10.0),
        )
    return _client


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def list_conversations(
    query: str | None = None,
    limit: int = 20,
    page_token: str | None = None,
) -> dict[str, Any]:
    """List Front conversations.

    Omit `query` to get the most recent conversations (sorted by latest
    activity). Provide `query` to filter with Front's search syntax.

    FRONT SEARCH SYNTAX — examples you can combine freely:

      STATUS
        status:open
        status:archived
        status:deleted            (trashed)
        status:spam

      ASSIGNMENT
        assignee:me
        assignee:alice@acme.com   (by teammate email)
        is:unassigned

      TAGS / LABELS
        tag:urgent
        tag:"high priority"       (multi-word tags must be quoted)
        -tag:spam                 (exclude a tag; prefix with minus)

      PEOPLE
        from:customer@example.com
        to:support@yourco.com

      INBOXES / CHANNELS
        inbox:support
        inbox:"customer success"
        channel:email
        channel:chat

      CONTENT
        subject:invoice           (word in subject line)
        body:refund               (word in body)
        "exact phrase"            (free-text match anywhere)

      DATE (ISO YYYY-MM-DD)
        after:2026-01-01
        before:2026-04-01
        after:2026-01-01 before:2026-02-01   (combine for a range)

      FLAGS
        is:starred
        is:unread
        is:discussion             (internal-only discussion threads)
        has:attachment            (at least one attachment on the conversation)

      COMBINING
        status:open tag:urgent              (space = implicit AND)
        status:open AND tag:urgent          (explicit AND)
        tag:billing OR tag:refund           (OR across alternatives)
        tag:urgent -assignee:me             (NOT via leading minus)
        (tag:urgent OR tag:vip) status:open (grouping with parens)

    Full reference: https://dev.frontapp.com/reference/search

    Pagination: pass `page_token` from the previous response's
    `next_page_token` to fetch the next page.
    """
    limit = max(1, min(limit, 100))
    params: dict[str, Any] = {"limit": limit}
    if query:
        params["q"] = query
    if page_token:
        params["page_token"] = page_token

    r = await _http().get("/conversations", params=params)
    r.raise_for_status()
    data = r.json()

    return {
        "conversations": [_trim_conversation(c) for c in data.get("_results", [])],
        "next_page_token": (data.get("_pagination") or {}).get("next"),
    }


@mcp.tool()
async def get_conversation(conversation_id: str) -> dict[str, Any]:
    """Fetch one conversation's metadata (no message bodies).

    Returns subject, status, assignee, tags, recipients, timestamps, and a
    short preview of the last message (id, author, snippet, created_at).
    Cheap call — use this to decide whether to dig in.

    Then call `list_conversation_messages(conversation_id)` for the actual
    message bodies, and `list_conversation_comments(conversation_id)` for
    internal teammate comments. Both paginate, so long threads stay cheap.
    """
    _assert_id(conversation_id, "conversation_id")
    r = await _http().get(f"/conversations/{conversation_id}")
    r.raise_for_status()
    return _trim_conversation_detail(r.json())


@mcp.tool()
async def list_conversation_messages(
    conversation_id: str,
    limit: int = 5,
    page_token: str | None = None,
    sort_order: str = "desc",
) -> dict[str, Any]:
    """List the messages on a conversation (full bodies).

    Defaults to the 5 most recent messages (`sort_order="desc"`). Pass
    `sort_order="asc"` to read oldest-first. Use `page_token` from the
    previous response's `next_page_token` to continue.

    Each message includes body/text/html, author, recipients, attachments
    metadata, and timestamps. Use `download_attachment(attachment_id)` to
    pull a file's bytes to local disk.
    """
    _assert_id(conversation_id, "conversation_id")
    if sort_order not in ("asc", "desc"):
        raise ValueError(f"sort_order must be 'asc' or 'desc', got {sort_order!r}")
    limit = max(1, min(limit, 100))
    params: dict[str, Any] = {"limit": limit, "sort_order": sort_order}
    if page_token:
        params["page_token"] = page_token

    r = await _http().get(f"/conversations/{conversation_id}/messages", params=params)
    r.raise_for_status()
    data = r.json()
    return {
        "messages": data.get("_results", []),
        "next_page_token": (data.get("_pagination") or {}).get("next"),
    }


@mcp.tool()
async def list_conversation_comments(
    conversation_id: str,
    limit: int = 20,
    page_token: str | None = None,
) -> dict[str, Any]:
    """List internal teammate comments on a conversation.

    Comments are private notes between teammates — they're not sent to the
    customer. Returns full comment bodies (`body`), author, attachments
    metadata, and timestamps. Use `page_token` from the previous response's
    `next_page_token` to continue.
    """
    _assert_id(conversation_id, "conversation_id")
    limit = max(1, min(limit, 100))
    params: dict[str, Any] = {"limit": limit}
    if page_token:
        params["page_token"] = page_token

    r = await _http().get(f"/conversations/{conversation_id}/comments", params=params)
    r.raise_for_status()
    data = r.json()
    return {
        "comments": data.get("_results", []),
        "next_page_token": (data.get("_pagination") or {}).get("next"),
    }


@mcp.tool()
async def list_tags() -> list[dict[str, Any]]:
    """List all tags in the Front workspace.

    Call this first when you don't know which tags exist. The returned names
    can be dropped straight into `list_conversations` queries — e.g. a tag
    named "urgent" becomes `tag:urgent`, and "high priority" becomes
    `tag:"high priority"`.

    Returns each tag's id, name, description, highlight color, and privacy
    flag. Auto-paginates through the full list.
    """
    results: list[dict[str, Any]] = []
    page_token: str | None = None
    while True:
        params: dict[str, Any] = {"limit": 100}
        if page_token:
            params["page_token"] = page_token
        r = await _http().get("/tags", params=params)
        r.raise_for_status()
        data = r.json()
        for t in data.get("_results", []):
            results.append(
                {
                    "id": t.get("id"),
                    "name": t.get("name"),
                    "description": t.get("description"),
                    "highlight": t.get("highlight"),
                    "is_private": t.get("is_private"),
                }
            )
        page_token = (data.get("_pagination") or {}).get("next")
        if not page_token:
            return results


@mcp.tool()
async def download_attachment(
    attachment_id: str,
    filename: str | None = None,
) -> dict[str, Any]:
    """Download a Front attachment to local disk.

    Saves under the system temp directory at
    `<tmpdir>/frontlet/<attachment_id>/<sanitized_filename>`. The path
    is returned in the response and is stable across repeated calls for the
    same attachment_id (so re-invocations just overwrite).

    Files are capped at 10 MB; larger attachments return an error. If you
    need the file long-term, move it out of the temp location.
    """
    _assert_id(attachment_id, "attachment_id")
    safe_name = _sanitize_filename(filename, attachment_id)
    out_dir = Path(tempfile.gettempdir()) / "frontlet" / attachment_id
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / safe_name

    async with _http().stream("GET", f"/download/{attachment_id}") as r:
        r.raise_for_status()
        cl = r.headers.get("content-length")
        if cl and int(cl) > MAX_ATTACHMENT_BYTES:
            raise ValueError(
                f"Attachment oversized: Content-Length {cl} bytes exceeds cap "
                f"{MAX_ATTACHMENT_BYTES}."
            )
        content_type = r.headers.get("content-type", "application/octet-stream")

        chunks: list[bytes] = []
        total = 0
        async for chunk in r.aiter_bytes():
            total += len(chunk)
            if total > MAX_ATTACHMENT_BYTES:
                raise ValueError(
                    f"Attachment oversized: stream exceeded cap "
                    f"{MAX_ATTACHMENT_BYTES} bytes mid-transfer."
                )
            chunks.append(chunk)

    out_path.write_bytes(b"".join(chunks))

    return {
        "path": str(out_path.resolve()),
        "filename": safe_name,
        "size": total,
        "content_type": content_type,
    }


@mcp.tool()
async def create_draft(
    body: str,
    to: list[str] | None = None,
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    subject: str | None = None,
    channel_id: str | None = None,
    mode: str = "shared",
) -> dict[str, Any]:
    """Create a new draft message, starting a new conversation.

    The draft is saved for human review — it is NOT sent. A teammate must
    open the draft in Front and click Send.

    Use this to start a brand-new conversation. To reply to an existing
    conversation, use `create_draft_reply` instead.

    The `body` accepts HTML for rich formatting:
      <strong>bold</strong>, <em>italic</em>, <a href="...">links</a>,
      <ul>/<ol> with <li> for lists, <p> for paragraphs, <br> for line
      breaks, <blockquote> for quotes.

    The sending channel defaults to the FRONT_SENDING_CHANNEL_ID environment
    variable. Pass `channel_id` to override (e.g. "alt:address:other@co.com").
    """
    if mode not in ("shared", "private"):
        raise ValueError(f"mode must be 'shared' or 'private', got {mode!r}")
    resolved_channel = _resolve_channel_id(channel_id)

    payload: dict[str, Any] = {"body": body, "mode": mode}
    if to:
        payload["to"] = to
    if cc:
        payload["cc"] = cc
    if bcc:
        payload["bcc"] = bcc
    if subject:
        payload["subject"] = subject

    r = await _http().post(f"/channels/{resolved_channel}/drafts", json=payload)
    r.raise_for_status()
    return _trim_draft(r.json())


@mcp.tool()
async def create_draft_reply(
    conversation_id: str,
    body: str,
    to: list[str] | None = None,
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    subject: str | None = None,
    channel_id: str | None = None,
    mode: str = "shared",
) -> dict[str, Any]:
    """Create a reply draft on an existing conversation (Reply All).

    The draft is saved for human review — it is NOT sent. A teammate must
    open the draft in Front and click Send.

    Recipients are auto-populated from the conversation's last message to
    emulate Reply All: all original TO and FROM addresses go into `to`, and
    all original CC addresses go into `cc`. The sending channel's own
    address is excluded. Pass `to` or `cc` explicitly to override the
    auto-detected recipients.

    The `body` accepts HTML for rich formatting:
      <strong>bold</strong>, <em>italic</em>, <a href="...">links</a>,
      <ul>/<ol> with <li> for lists, <p> for paragraphs, <br> for line
      breaks, <blockquote> for quotes.

    The sending channel defaults to the FRONT_SENDING_CHANNEL_ID environment
    variable. Pass `channel_id` to override.
    """
    _assert_id(conversation_id, "conversation_id")
    if mode not in ("shared", "private"):
        raise ValueError(f"mode must be 'shared' or 'private', got {mode!r}")
    resolved_channel = _resolve_channel_id(channel_id)

    if to is None or cc is None:
        auto_to, auto_cc = await _reply_all_recipients(
            conversation_id, resolved_channel
        )
        if to is None:
            to = auto_to
        if cc is None:
            cc = auto_cc

    payload: dict[str, Any] = {
        "body": body,
        "channel_id": resolved_channel,
        "mode": mode,
    }
    if to:
        payload["to"] = to
    if cc:
        payload["cc"] = cc
    if bcc:
        payload["bcc"] = bcc
    if subject:
        payload["subject"] = subject

    r = await _http().post(
        f"/conversations/{conversation_id}/drafts", json=payload
    )
    r.raise_for_status()
    return _trim_draft(r.json())


@mcp.tool()
async def edit_draft(
    draft_id: str,
    body: str,
    version: str,
    to: list[str] | None = None,
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    subject: str | None = None,
    channel_id: str | None = None,
) -> dict[str, Any]:
    """Edit an existing draft message.

    Requires the `version` string returned by `create_draft`,
    `create_draft_reply`, or a previous `edit_draft` call. If the draft was
    modified elsewhere since that version, Front rejects the edit — re-read
    the draft to get the current version and retry.

    The `body` accepts the same HTML formatting as the create tools.

    The sending channel defaults to the FRONT_SENDING_CHANNEL_ID environment
    variable. Pass `channel_id` to override.
    """
    _assert_id(draft_id, "draft_id")
    resolved_channel = _resolve_channel_id(channel_id)

    payload: dict[str, Any] = {
        "body": body,
        "channel_id": resolved_channel,
        "version": version,
    }
    if to:
        payload["to"] = to
    if cc:
        payload["cc"] = cc
    if bcc:
        payload["bcc"] = bcc
    if subject:
        payload["subject"] = subject

    r = await _http().patch(f"/drafts/{draft_id}/", json=payload)
    r.raise_for_status()
    return _trim_draft(r.json())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_channel_id(override: str | None) -> str:
    """Return the effective channel ID: explicit override > env var > error."""
    value = override or SENDING_CHANNEL_ID
    if not value:
        raise ValueError(
            "No sending channel configured. Set the FRONT_SENDING_CHANNEL_ID "
            "environment variable (e.g. alt:address:support@yourcompany.com) "
            "or pass channel_id explicitly."
        )
    return value


async def _reply_all_recipients(
    conversation_id: str, channel_id: str
) -> tuple[list[str], list[str]]:
    """Derive Reply All recipients from the conversation's last message.

    Returns (to_list, cc_list).  The sending channel's own address is
    excluded so it doesn't appear as a recipient on the draft.
    """
    r = await _http().get(
        f"/conversations/{conversation_id}/messages",
        params={"limit": 1, "sort_order": "desc"},
    )
    r.raise_for_status()
    messages = r.json().get("_results", [])
    if not messages:
        return [], []

    exclude = _channel_address(channel_id)
    last = messages[0]
    to_handles: list[str] = []
    cc_handles: list[str] = []
    for recipient in last.get("recipients") or []:
        handle = recipient.get("handle")
        if not handle or (exclude and handle.lower() == exclude.lower()):
            continue
        role = recipient.get("role")
        if role in ("from", "to"):
            to_handles.append(handle)
        elif role == "cc":
            cc_handles.append(handle)
    return to_handles, cc_handles


_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _assert_id(value: str, field: str) -> None:
    if not isinstance(value, str) or not _ID_RE.match(value):
        raise ValueError(f"Invalid {field}: {value!r} (must match [A-Za-z0-9_-]+)")


def _trim_conversation(c: dict[str, Any]) -> dict[str, Any]:
    """Compact conversation summary for list results."""
    last_msg = c.get("last_message") if isinstance(c.get("last_message"), dict) else None
    return {
        "id": c.get("id"),
        "subject": c.get("subject"),
        "status": c.get("status"),
        "assignee": (c.get("assignee") or {}).get("email") if c.get("assignee") else None,
        "tags": [t.get("name") for t in (c.get("tags") or [])],
        "last_message_at": (last_msg or {}).get("created_at"),
        "created_at": c.get("created_at"),
    }


def _trim_conversation_detail(c: dict[str, Any]) -> dict[str, Any]:
    """Single-conversation view: summary plus a preview of the last message."""
    last_msg = c.get("last_message") if isinstance(c.get("last_message"), dict) else None
    last_preview: dict[str, Any] | None = None
    if last_msg:
        author = last_msg.get("author") or {}
        last_preview = {
            "id": last_msg.get("id"),
            "type": last_msg.get("type"),
            "is_inbound": last_msg.get("is_inbound"),
            "created_at": last_msg.get("created_at"),
            "blurb": last_msg.get("blurb"),
            "author": author.get("email") or author.get("username"),
        }
    return {
        "id": c.get("id"),
        "subject": c.get("subject"),
        "status": c.get("status"),
        "is_private": c.get("is_private"),
        "assignee": (c.get("assignee") or {}).get("email") if c.get("assignee") else None,
        "tags": [t.get("name") for t in (c.get("tags") or [])],
        "recipients": [
            {"handle": r.get("handle"), "role": r.get("role")}
            for r in (c.get("recipients") or [])
        ],
        "created_at": c.get("created_at"),
        "last_message": last_preview,
    }


def _trim_draft(d: dict[str, Any]) -> dict[str, Any]:
    """Compact draft response — includes version for subsequent edits."""
    return {
        "id": d.get("id"),
        "version": d.get("version"),
        "draft_mode": d.get("draft_mode"),
        "subject": d.get("subject"),
        "blurb": d.get("blurb"),
        "recipients": [
            {"handle": r.get("handle"), "role": r.get("role")}
            for r in (d.get("recipients") or [])
        ],
        "created_at": d.get("created_at"),
    }


def _channel_address(channel_id: str) -> str | None:
    """Extract the email address from an alt:address: or alt:email: alias."""
    for prefix in ("alt:address:", "alt:email:"):
        if channel_id.startswith(prefix):
            return channel_id[len(prefix):]
    return None


_ALLOWED = re.compile(r"[^A-Za-z0-9._\- ]")
_RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$", re.IGNORECASE)


def _sanitize_filename(name: str | None, fallback_id: str) -> str:
    """Produce a safe filesystem name; fall back to `attachment-<id>.bin`."""
    if not name:
        return f"attachment-{fallback_id}.bin"
    s = unicodedata.normalize("NFKC", name)
    if ".." in s or "/" in s or "\\" in s or "\x00" in s:
        return f"attachment-{fallback_id}.bin"
    s = _ALLOWED.sub("_", s)
    s = re.sub(r"_+", "_", s)
    s = s.strip(" .-")
    if _RESERVED.match(s.split(".", 1)[0]):
        return f"attachment-{fallback_id}.bin"
    # Cap at 200 UTF-8 bytes; avoid mid-multibyte truncation.
    s = s.encode("utf-8")[:200].decode("utf-8", errors="ignore")
    return s or f"attachment-{fallback_id}.bin"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the MCP server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
