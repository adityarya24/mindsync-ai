# MindSync MCP

[![CI](https://github.com/adityarya24/mindsync-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/adityarya24/mindsync-mcp/actions/workflows/ci.yml)

Local-first **Model Context Protocol** server for multi-agent memory sync and focus conflict detection.

Use it when several coding agents (Claude Code, Codex, Cursor, Gemini CLI, Grok, custom agents, …) share one developer machine and need:

1. A shared session/focus registry (who is editing what)
2. Conflict warnings when focuses overlap
3. Optional durable fact sync to **your** remote host over SSH (offline queue when remote is down)

No cloud account. No hard-coded hosts or home directories.

## Features

| Tool | Purpose |
| --- | --- |
| `get_sync_context` | Load local state + compiled truth (optional remote pull) |
| `update_focus` | Per-agent focus/project/branch (+ optional paths); conflict warnings |
| `queue_durable_fact` | Write fact remotely, or queue offline if unreachable / unconfigured |
| `sync_offline_facts` | Flush offline queue; consolidate + pull truth |
| `pull_truth` | Windows-safe SCP pull of compiled-truth markdown |
| `health` | Paths, queue depth, remote reachability |

Design principles:

1. **Offline-first** — local tools always work; remote is opt-in and best-effort.
2. **Locked local state** — exclusive lockfiles around state/queue writes.
3. **Safe remote writes** — identifier allowlists + base64 text transport over SSH.
4. **No false-positive conflicts** — same project alone is not a conflict; token/path overlap is.
5. **Generic by default** — zero personal paths in code; configure via env.

## Install

```bash
git clone https://github.com/adityarya24/mindsync-mcp.git
cd mindsync-mcp
python -m pip install -e ".[dev]"
```

## Quick start (local only)

No env vars required. State lives under `~/.mindsync`.

### MCP client config

After `pip install -e .`:

```json
{
  "mcpServers": {
    "mindsync": {
      "command": "mindsync"
    }
  }
}
```

Or run the module path explicitly:

```json
{
  "mcpServers": {
    "mindsync": {
      "command": "python",
      "args": ["-m", "mindsync.server"]
    }
  }
}
```

(Windows: use your venv’s `python.exe` if agents don’t see the same PATH.)

## Optional remote sync

Remote features stay **disabled** until both are set:

```bash
export MINDSYNC_SSH_HOST=my-server          # SSH config Host or user@host
export MINDSYNC_REMOTE_ROOT=/opt/mindsync   # directory on that host
```

See [`.env.example`](.env.example) for the full list and [`examples/remote/`](examples/remote/) for sample server-side scripts (`mindsync_fact.py`, `mindsync_consolidate.py`).

| Env var | Default | Meaning |
| --- | --- | --- |
| `MINDSYNC_HOME` | `~/.mindsync` | Local data root |
| `MINDSYNC_SSH_HOST` | *(empty)* | SSH host; empty disables remote |
| `MINDSYNC_REMOTE_ROOT` | *(empty)* | Remote project root; empty disables remote |
| `MINDSYNC_REMOTE_ENV_FILE` | `config/mindsync.env` | Optional file sourced on remote before commands |
| `MINDSYNC_REMOTE_WRITE_SCRIPT` | `tools/mindsync_fact.py` | Relative to remote root |
| `MINDSYNC_REMOTE_CONSOLIDATE_SCRIPT` | `tools/mindsync_consolidate.py` | Relative to remote root |
| `MINDSYNC_REMOTE_TRUTH_SUBDIR` | `compiled-truth` | Markdown summaries directory |
| `MINDSYNC_SSH_TIMEOUT` | `3` | SSH connect timeout (seconds) |
| `MINDSYNC_FOCUS_STALE_SECS` | `7200` | Ignore older focus entries |
| `MINDSYNC_REMOTE_CACHE_TTL` | `30` | Cache TTL for online probe |
| `MINDSYNC_LOCK_TIMEOUT` | `5` | Local lock wait (seconds) |

SSH must be key-based / `BatchMode`-friendly (no interactive password prompts).

## Local data layout

Under `MINDSYNC_HOME` (default `~/.mindsync`):

- `local-state.json` — active project + per-agent focus map
- `local-audit.jsonl` — append-only action log
- `offline_queue.jsonl` — durable facts waiting for remote
- `compiled-truth/*.md` — pulled remote summaries
- `.locks/` — exclusive lockfiles

## Agent usage pattern

1. **Start:** `get_sync_context(agent_name)` (set `refresh_remote=true` when remote is configured).
2. **Work:** `update_focus(agent_name, project, branch, focus, paths=[...])`.
3. **Milestone:** `queue_durable_fact(agent_name, entity, attribute, text)`.
4. **Reconnect:** `sync_offline_facts(agent_name)`.

## Layout

```
mindsync-mcp/
├── mindsync/
│   ├── server.py      # FastMCP tools
│   ├── storage.py     # JSON/JSONL + locks
│   ├── bridge.py      # optional SSH/SCP
│   ├── conflict.py    # focus overlap
│   └── config.py      # env-based settings
├── examples/remote/   # sample remote hooks
├── tests/
├── .env.example
└── pyproject.toml
```

## Develop / test

```bash
python -m pip install -e ".[dev]"
python -m pytest -q
python scripts/smoke_test.py
```

GitHub Actions runs the same suite on every push/PR to `master`
(Python 3.10 + 3.12, Ubuntu + Windows).

## Security notes

- Treat tool arguments as untrusted: entity/attribute/agent must match a strict allowlist.
- Fact text is base64-transported to the remote host (no raw shell interpolation of free text).
- This server runs with your user privileges and can SSH only when you configure a host — wire it into trusted local agent clients only.
- Do not commit real `.env` files or server secrets.

## License

MIT
