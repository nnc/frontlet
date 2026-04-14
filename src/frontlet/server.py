"""Frontlet: a minimal Front MCP server.

Exposes four tools over stdio so an AI agent can:
  - list conversations (with Front's search syntax for filtering)
  - fetch a conversation with its messages and internal comments
  - list all workspace tags (for query-building)
  - download an attachment to a local temp path

Auth: reads FRONT_API_TOKEN from the environment at startup.
"""

from __future__ import annotations

import asyncio
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

API_TOKEN = os.environ.get("FRONT_API_TOKEN")
if not API_TOKEN:
    print(
        "frontlet-mcp: FRONT_API_TOKEN environment variable is required.\n"
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
    """Fetch a conversation with all its messages and internal comments.

    Returns the conversation metadata, external messages (emails, chats,
    replies), and internal teammate comments in a single response. Messages
    and comments are separate arrays; use their `created_at` timestamps to
    interleave them chronologically if needed.

    Attachment metadata (id, filename, size, content_type) is available on
    each message under `attachments`. Use `download_attachment(attachment_id)`
    to fetch the file bytes to local disk.
    """
    _assert_id(conversation_id, "conversation_id")
    http = _http()
    conv_task = http.get(f"/conversations/{conversation_id}")
    msgs_task = http.get(f"/conversations/{conversation_id}/messages")
    cmts_task = http.get(f"/conversations/{conversation_id}/comments")

    conv_r, msgs_r, cmts_r = await asyncio.gather(conv_task, msgs_task, cmts_task)
    for r in (conv_r, msgs_r, cmts_r):
        r.raise_for_status()

    return {
        "conversation": conv_r.json(),
        "messages": msgs_r.json().get("_results", []),
        "comments": cmts_r.json().get("_results", []),
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
    `<tmpdir>/frontlet-mcp/<attachment_id>/<sanitized_filename>`. The path
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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
