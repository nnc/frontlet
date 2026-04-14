# frontlet

A minimal [Model Context Protocol](https://modelcontextprotocol.io) server for [Front](https://front.com), designed for AI agents that need to read conversations and pull down attachments. No Docker, no build step, no clone — install with a single `uvx` command.

Four tools. ~1,250 tokens in your context window.

## Tools

| Tool | Purpose |
|---|---|
| `list_conversations` | Most recent conversations, or filter by Front search syntax (tags, status, assignee, dates, etc.) |
| `get_conversation` | One conversation with all its external messages and internal team comments |
| `list_tags` | Discover which tags exist before building queries |
| `download_attachment` | Save an attachment to `$TMPDIR/frontlet/<id>/<filename>`; up to 10 MB |

## Install

You need [`uv`](https://docs.astral.sh/uv/) on your machine.

### Claude Code

```bash
claude mcp add frontlet -e FRONT_API_TOKEN=eyJ... -- uvx --from git+https://github.com/nnc/frontlet frontlet
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

Front Settings → Company → Developers → API tokens → create a token with the scopes you need (at a minimum `shared:*` with `Read` permissions for attachments, comments, conversations, messages and tags). Front tokens are JWTs — they start with `eyJ` and are quite long.

## Usage notes

### Search syntax

`list_conversations` accepts Front's native search query in the `query` parameter. A few common patterns:

- `tag:urgent status:open` — open conversations tagged urgent
- `from:customer@acme.com after:2026-01-01` — from a specific sender this year
- `has:attachment tag:invoice` — invoice conversations that include a file
- `-assignee:me status:open` — open conversations NOT assigned to me
- `(tag:billing OR tag:refund) status:open` — grouped filter

Full reference: [dev.frontapp.com/reference/search](https://dev.frontapp.com/reference/search). The tool description also embeds a large cheat-sheet so the LLM can build queries without external lookups.

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

- **Attachment upload / message sending** — read-only by design.
- **Webhooks** — separate runtime model.
- **Streaming large files** — 10 MB cap keeps the implementation simple.
- **Caching** — the agent manages its own context; the server is stateless per call.

## License

MIT — see [LICENSE](./LICENSE).
