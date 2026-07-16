# MindSync MCP

Local-first **Model Context Protocol** server for multi-agent memory sync and focus conflict detection.

MindSync bridges local CLI agents (Claude Code, Codex, Grok, Antigravity, etc.) through a shared laptop state store, with best-effort sync to a remote durable gbrain/Postgres backend over SSH.

## Features

| Tool | Purpose |
| --- | --- |
| `get_sync_context` | One-call load of local state + compiled truth (optional remote pull) |
| `update_focus` | Per-agent focus/project/branch (+ optional paths); conflict warnings |
| `queue_durable_fact` | Write fact to VPS, or queue offline if unreachable |
| `sync_offline_facts` | Flush offline queue; consolidate + pull truth |
| `pull_truth` | Windows-safe SCP pull of compiled-truth markdown |
| `health` | Paths, queue depth, VPS reachability |

Design principles:

1. **Offline-first** — local tools always work; remote is best-effort.
2. **Locked local state** — exclusive lockfiles around state/queue writes.
3. **Safe remote writes** — identifier allowlists + base64 text transport over SSH.
4. **No false-positive conflicts** — same project alone is not a conflict; token/path overlap is.

## Layout

```
mindsync-mcp/
├── mindsync/
│   ├── server.py      # FastMCP tools
│   ├── storage.py     # JSON/JSONL + locks
│   ├── bridge.py      # SSH/SCP
│   ├── conflict.py    # focus overlap
│   └── config.py      # env-based settings
├── mindsync_server.py # thin entry shim
├── tests/
└── pyproject.toml
```

Local data directory (default `~/.local-gbrain` or `%USERPROFILE%\.local-gbrain`):

- `local-state.json` — active project + per-agent focus map
- `local-audit.jsonl` — append-only action log
- `offline_queue.jsonl` — durable facts waiting for VPS
- `compiled-truth/*.md` — pulled remote summaries

## Configuration

| Env var | Default | Meaning |
| --- | --- | --- |
| `MINDSYNC_HOME` | `~/.local-gbrain` | Local data root |
| `MINDSYNC_SSH_HOST` | `openclaw-vps` | SSH host alias |
| `MINDSYNC_REMOTE_ROOT` | `/home/aditya/.openclaw/workspace/gbrain` | Remote gbrain root |
| `MINDSYNC_SSH_TIMEOUT` | `3` | SSH connect timeout (seconds) |
| `MINDSYNC_FOCUS_STALE_SECS` | `7200` | Focus entries older than this are ignored |
| `MINDSYNC_VPS_CACHE_TTL` | `30` | Cache TTL for VPS online probe |

SSH must be key-based / `BatchMode` friendly (no interactive password prompts).

## Install

```bash
cd mindsync-mcp
python -m pip install -e ".[dev]"
```

## MCP client config

```json
{
  "mcpServers": {
    "mindsync": {
      "command": "python",
      "args": ["C:\\Users\\ADITYA\\mindsync-mcp\\mindsync_server.py"]
    }
  }
}
```

Or after install:

```json
{
  "mcpServers": {
    "mindsync": {
      "command": "mindsync"
    }
  }
}
```

## Agent usage pattern

1. **Start:** `get_sync_context(agent_name, refresh_remote=true)` when online.
2. **Work:** `update_focus(agent_name, project, branch, focus, paths=[...])`.
3. **Milestone:** `queue_durable_fact(agent_name, entity, attribute, text)`.
4. **Reconnect:** `sync_offline_facts(agent_name)`.

## Develop / test

```bash
python -m pytest -q
```

## Security notes

- Treat tool arguments as untrusted: entity/attribute/agent must match a strict allowlist.
- Fact text is base64-transported to the remote host (no raw shell interpolation).
- This server runs with your user privileges and can SSH to the configured host — only wire it into trusted local agent clients.

## License

MIT
