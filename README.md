# MindSync MCP Server

MindSync is a lightweight, cross-environment durable memory and session synchronization server implementing the **Model Context Protocol (MCP)**. 

It is designed to bridge local developer agent sessions (e.g., Claude Code, Antigravity CLI, Codex CLI) with remote VPS-hosted agent databases, and coordinate multiple active terminal tabs on a single developer machine.

---

## Features

1. **One-Call Context Loading (`get_sync_context`)**
   - Fetches the entire local workspace parameters (active branch, project) and downloads/reads the compiled memory summaries from the remote server in a single call.
2. **Conflict Focus Detection (`update_focus`)**
   - Tracks the active files and focus areas of different local CLI agents running in separate terminal tabs in real-time. Automatically warns when two agents are claiming overlapping focuses in the same workspace.
3. **Offline-First Durable Queue (`queue_durable_fact`)**
   - Attempts to push high-confidence milestone facts to a remote database (e.g., PostgreSQL). If the network is down or the remote server is offline, it automatically queues the fact locally (`offline_queue.jsonl`) to ensure zero-disruption offline coding.
4. **Resync When Online (`sync_offline_facts`)**
   - Flushes all offline queued facts to the remote VPS database when the connection is restored, rebuilds the consolidated summaries, and pulls the fresh compiled memory files down to the laptop.

---

## File Structure

- `mindsync_server.py` — Python FastMCP server containing the tool definitions.
- `pyproject.toml` — Standard python dependency definition.
- `C:\Users\ADITYA\.local-gbrain\` — Shared local directory containing:
  - `local-state.json` — Active focuses, custom overrides, and active projects.
  - `local-audit.jsonl` — Append-only tab transaction log.
  - `offline_queue.jsonl` — Offline facts queue.
  - `compiled-truth/` — Consolidated markdown memory files synced from the remote server.

---

## Installation & Setup

### 1. Register the MCP Server
Add the server configuration to your MCP client (e.g., Claude Desktop, Antigravity config, or Claude Code):

```json
"mcpServers": {
  "mindsync": {
    "command": "python",
    "args": [
      "C:\\Users\\ADITYA\\mindsync-mcp\\mindsync_server.py"
    ]
  }
}
```

### 2. Configure SSH Access (Optional)
MindSync relies on SSH to communicate with the remote server database. Ensure you have your remote host configuration (e.g. `openclaw-vps`) set up in your SSH config file (`~/.ssh/config`) with public-key authentication so that connection checks and file transfers run without interactive passphrase prompts.
