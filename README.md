# frontlet

A minimal [Model Context Protocol](https://modelcontextprotocol.io) server for [Front](https://front.com), designed for AI agents that need to read conversations, compose draft replies, and pull down attachments. No Docker, no build step, no clone — install with a single `uvx` command.

Eleven tools. Small context footprint.

## Tools

| Tool | Purpose |
|---|---|
| `list_conversations` | Most recent conversations, or filter by Front search syntax (tags, status, assignee, dates, etc.) |
| `get_conversation` | One conversation's metadata (subject, status, tags, last-message preview) — no message bodies |
| `list_conversation_messages` | Page through messages on a conversation (defaults to 5 most recent, newest-first) |
| `list_conversation_comments` | Page through internal teammate comments on a conversation (defaults to 20) |
| `list_tags` | Discover which tags exist before building queries |
| `download_attachment` | Save an attachment to `$TMPDIR/frontlet/<id>/<filename>`; up to 10 MB |
| `create_draft` | Create a new draft message (starts a new conversation) for human review |
| `create_draft_reply` | Create a reply draft on an existing conversation — Reply All with quoted message |
| `edit_draft` | Update an existing draft's body, recipients, or subject (optimistic locking via `version`) |
| `tag_conversation` | Add one or more tags to a conversation |
| `untag_conversation` | Remove one or more tags from a conversation |

## Install

You need [`uv`](https://docs.astral.sh/uv/) on your machine.

### Claude Code

```bash
claude mcp add frontlet \
  -e FRONT_API_TOKEN=eyJ... \
  -- uvx --from git+https://github.com/nnc/frontlet frontlet
```

Verify it was added:

```bash
claude mcp list
```

### Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json` on macOS:

```json
{
  "mcpServers": {
    "frontlet": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/nnc/frontlet", "frontlet"],
      "env": {
        "FRONT_API_TOKEN": "eyJ..."
      }
    }
  }
}
```

### Getting your Front API token

Front Settings → Company → Developers → API tokens → create a token with these scopes:

| Scope | What it enables |
|---|---|
| `conversations:read` | List and fetch conversations |
| `messages:read` | Read message bodies and recipients |
| `comments:read` | Read internal teammate comments |
| `attachments:read` | Download attachments |
| `tags:read` | List workspace tags |
| `channels:read` | Auto-detect sending channel for drafts |
| `teammates:read` | Auto-detect draft author |
| `drafts:write` | Create and edit draft messages |
| `conversations:write` | Add and remove tags on conversations |

**Do NOT grant Send permission** — this keeps agents from sending messages directly. Agents create drafts; humans review and send.

Front tokens are JWTs — they start with `eyJ` and are quite long.

## Usage notes

### Search syntax

`list_conversations` accepts Front's native search query in the `query` parameter. A few common patterns:

- `tag:urgent status:open` — open conversations tagged urgent
- `from:customer@acme.com after:2026-01-01` — from a specific sender this year
- `has:attachment tag:invoice` — invoice conversations that include a file
- `-assignee:me status:open` — open conversations NOT assigned to me
- `(tag:billing OR tag:refund) status:open` — grouped filter

Full reference: [dev.frontapp.com/reference/search](https://dev.frontapp.com/reference/search). The tool description also embeds a large cheat-sheet so the LLM can build queries without external lookups.

### Drafts

Agents create drafts for human review — they never send messages directly. A teammate must open the draft in Front and click Send.

#### Sending channel

The sending channel (the address drafts are sent **from**) is auto-detected from the workspace. If the workspace has exactly one channel, it's used automatically. If there are multiple channels, the agent must pass `channel_id` to specify which one — the error message lists all available channels with their addresses and IDs.

Any draft tool can override auto-detection via the `channel_id` parameter (e.g. `alt:address:billing@yourcompany.com` or a raw channel ID like `cha_abc123`).

#### Draft author

The draft author is auto-detected from the workspace's human teammates. If there's exactly one human teammate, they're set as the author automatically. If there are multiple, pass `author_id` (a teammate ID like `tea_abc` or the teammate's email address) to specify.

For **private** drafts, `author_id` is required when multiple teammates exist — private drafts are only visible to their author, so the API bot identity won't work.

For **shared** drafts, the author defaults to the API bot if multiple teammates exist and none is specified. The draft is still visible to everyone, but shows the bot as author rather than a human name.

#### Reply All

`create_draft_reply` automatically populates recipients from the conversation's last message (or from a specific message if `message_id` is provided), emulating the Reply All button. All original TO and FROM addresses go into `to`, all original CC addresses go into `cc`, and the sending channel's own address is excluded. Pass `to` or `cc` explicitly to override the auto-detected recipients.

#### Quoted reply

Reply drafts automatically include the original message body as a quoted reply, like a normal email client. Front wraps the quoted content as a blockquote. There is no opt-out — this matches standard email behavior.

#### Replying to a specific message

By default, `create_draft_reply` replies to the most recent message in the conversation. Pass `message_id` to reply to a specific message instead — the quoted reply and auto-detected recipients will come from that message.

#### Signature

All new drafts (`create_draft`, `create_draft_reply`) automatically include the account's default email signature. This is always on.

#### Rich formatting

The `body` parameter on all draft tools accepts HTML:

```html
<strong>bold</strong>, <em>italic</em>
<a href="https://example.com">links</a>
<ul><li>unordered list</li></ul>
<ol><li>ordered list</li></ol>
<p>paragraphs</p>, <br> line breaks
<blockquote>quoted text</blockquote>
```

#### Editing drafts

`edit_draft` requires the `version` string returned by `create_draft`, `create_draft_reply`, or a previous `edit_draft` call. This prevents accidental overwrites when the draft was modified elsewhere — if the version is stale, Front rejects the edit.

### Tags

Use `list_tags` to discover available tags, then `tag_conversation` and `untag_conversation` to modify them. Both tag tools accept a list of tag IDs, so multiple tags can be added or removed in one call.

Workflow: `list_tags` → pick the IDs you need → `tag_conversation(conversation_id, tag_ids)`.

### Attachment storage

Files are saved under the system temp directory: `$TMPDIR/frontlet/<attachment_id>/<filename>` on macOS/Linux, `%TEMP%\frontlet\<id>\<filename>` on Windows. Re-downloading the same attachment overwrites in place (idempotent).

Temp directories get cleaned by the OS — if you need a file long-term, move it elsewhere.

### Size limits

Attachments are capped at 10 MB. Larger files return a clear error. Front itself caps at 25-100 MB depending on channel; raise the constant in `server.py` if you hit the limit often.

## Development

```bash
git clone https://github.com/nnc/frontlet
cd frontlet
uv sync
FRONT_API_TOKEN=eyJ... uv run frontlet
```

The server speaks MCP over stdio. For interactive testing use the [MCP Inspector](https://github.com/modelcontextprotocol/inspector):

```bash
FRONT_API_TOKEN=eyJ... npx @modelcontextprotocol/inspector uv run frontlet
```

Point Claude Code at a local checkout while iterating (before pushing to GitHub):

```bash
claude mcp add frontlet -e FRONT_API_TOKEN=eyJ... -- uv run --directory /absolute/path/to/frontlet frontlet
```

## Non-goals

Intentionally left out:

- **Message sending** — agents create drafts for human review, never send directly.
- **Attachment upload** — drafts are text-only for now.
- **Webhooks** — separate runtime model.
- **Streaming large files** — 10 MB cap keeps the implementation simple.
- **Caching** — the agent manages its own context; the server is stateless per call.

## License

MIT — see [LICENSE](./LICENSE).
