# MindSync AI

[![CI](https://github.com/adityarya24/mindsync-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/adityarya24/mindsync-mcp/actions/workflows/ci.yml)
[![PyPI version](https://img.shields.io/pypi/v/mindsync-ai.svg)](https://pypi.org/project/mindsync-ai/)
[![Python versions](https://img.shields.io/pypi/pyversions/mindsync-ai.svg)](https://pypi.org/project/mindsync-ai/)

**One Python MCP server** for multi-agent teams: shared memory, focus conflict detection, an in-process event bus, and CLI agent dispatch (Codex, Claude, Gemini, Cursor, Aider, Grok).

| Layer | What it does |
| --- | --- |
| **Core** | Local-first focus registry + optional durable facts over SSH |
| **Bus** | Typed events (`job.*`, `focus.changed`, `memory.updated`, …) with monotonic `seq` |
| **Dispatch** | Spawn headless CLI agents, track jobs, cancel process trees |

No cloud account required. Remote sync is opt-in. Zero hard-coded hosts or personal paths.

> **Package rename (v1.1.0):** PyPI name is now **`mindsync-ai`** (was `mindsync-mcp` through 1.0.1).  
> Import path and CLI stay the same: `import mindsync` · `mindsync`.

## Install

```bash
pip install mindsync-ai
```

Requires Python 3.10+.

From source:

```bash
git clone https://github.com/adityarya24/mindsync-mcp.git
cd mindsync-mcp
python -m pip install -e ".[dev]"
```

## MCP client config

```json
{
  "mcpServers": {
    "mindsync": {
      "command": "mindsync"
    }
  }
}
```

Or:

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

(Windows: point at your venv’s `python.exe` if agents don’t share PATH.)

## Tools (13)

### Core memory / focus

| Tool | Purpose |
| --- | --- |
| `get_sync_context` | Local state + compiled truth (optional remote pull) |
| `update_focus` | Per-agent focus/project/branch/paths; conflict warnings → emits `focus.changed` |
| `queue_durable_fact` | Remote write or offline queue → emits `memory.updated` |
| `sync_offline_facts` | Flush offline queue; consolidate + pull truth |
| `pull_truth` | Windows-safe SCP pull of compiled-truth markdown |
| `health` | Paths, queue depth, remote reachability |

### Event bus

| Tool | Purpose |
| --- | --- |
| `publish_event` | Publish a typed event with payload |
| `poll_events` | Poll events since a sequence number |
| `subscribe_events` | Subscribe an agent to event types |

### Agent dispatch

| Tool | Purpose |
| --- | --- |
| `delegate_task` | Run a CLI agent (foreground or background) |
| `job_status` | Job status + PID reconciliation |
| `job_result` | Read job result file |
| `job_cancel` | Cancel running job and kill its process tree |

Dispatch also auto-emits `job.started`, `job.completed`, and `job.failed` on the bus.

### CLI (dispatch)

```bash
mindsync-dispatch agents
mindsync-dispatch run codex "summarize README" --background
mindsync-dispatch status
mindsync-dispatch result <job-id>
mindsync-dispatch cancel <job-id>
```

Jobs live under `~/.claude/agent-dispatch/jobs/` (override with `AGENT_DISPATCH_HOME`).  
Custom agents: `~/.claude/agent-dispatch/agents.json`.

Built-in presets: `codex`, `claude`, `gemini`, `cursor`, `aider`, `grok`.

## Quick start (local only)

No env vars required for core + bus + dispatch. State lives under `~/.mindsync`.

1. **Start:** `get_sync_context(agent_name)`  
2. **Work:** `update_focus(agent_name, project, branch, focus, paths=[...])`  
3. **Milestone:** `queue_durable_fact(agent_name, entity, attribute, text)`  
4. **Delegate:** `delegate_task(agent="codex", prompt="...", background=True)`  
5. **Reconnect:** `sync_offline_facts(agent_name)` when remote is configured  

## Optional remote sync

Remote stays **disabled** until both are set:

```bash
export MINDSYNC_SSH_HOST=my-server          # SSH config Host or user@host
export MINDSYNC_REMOTE_ROOT=/opt/mindsync   # directory on that host
```

See [`.env.example`](.env.example) and [`examples/remote/`](examples/remote/).

| Env var | Default | Meaning |
| --- | --- | --- |
| `MINDSYNC_HOME` | `~/.mindsync` | Local data root |
| `MINDSYNC_SSH_HOST` | *(empty)* | SSH host; empty disables remote |
| `MINDSYNC_REMOTE_ROOT` | *(empty)* | Remote project root |
| `MINDSYNC_REMOTE_ENV_FILE` | `config/mindsync.env` | Sourced on remote before commands |
| `MINDSYNC_REMOTE_WRITE_SCRIPT` | `tools/mindsync_fact.py` | Relative to remote root |
| `MINDSYNC_REMOTE_CONSOLIDATE_SCRIPT` | `tools/mindsync_consolidate.py` | Relative to remote root |
| `MINDSYNC_REMOTE_TRUTH_SUBDIR` | `compiled-truth` | Markdown summaries directory |
| `MINDSYNC_SSH_TIMEOUT` | `3` | SSH connect timeout (seconds) |
| `MINDSYNC_FOCUS_STALE_SECS` | `7200` | Ignore older focus entries |
| `MINDSYNC_REMOTE_CACHE_TTL` | `30` | Cache TTL for online probe |
| `MINDSYNC_LOCK_TIMEOUT` | `5` | Local lock wait (seconds) |
| `MINDSYNC_LOCK_STALE_SECS` | `60` | Steal lock after holder stops renewing |

SSH must be key-based / `BatchMode`-friendly.

### Two machines (VPS + laptop)

Run MindSync AI on **each** machine for local focus/state. Share **durable facts** via one always-on host:

1. **VPS:** deploy [`examples/remote/`](examples/remote/) scripts under e.g. `/opt/mindsync`.  
2. **Laptop:** set `MINDSYNC_SSH_HOST` + `MINDSYNC_REMOTE_ROOT` to that VPS.  
3. **VPS itself:** leave remote vars empty — it *is* the store.

## Local data layout

Under `MINDSYNC_HOME` (default `~/.mindsync`):

- `local-state.json` — active project + per-agent focus map  
- `local-audit.jsonl` — append-only action log  
- `offline_queue.jsonl` — durable facts waiting for remote  
- `events.jsonl` — event bus log  
- `subscriptions.json` — event subscriptions  
- `compiled-truth/*.md` — pulled remote summaries  
- `.locks/` — exclusive lockfiles  

## Layout

```
mindsync-mcp/                 # GitHub repo
├── mindsync/
│   ├── server.py             # FastMCP tools (core + bus + dispatch)
│   ├── storage.py            # JSON/JSONL + locks
│   ├── bridge.py             # optional SSH/SCP
│   ├── conflict.py           # focus overlap
│   ├── config.py             # env-based settings
│   ├── bus/                  # Event bus engine
│   └── dispatch/             # Agent dispatch (presets, runner, CLI)
├── examples/remote/
├── tests/
└── pyproject.toml            # PyPI: mindsync-ai
```

## Develop / test

```bash
python -m pip install -e ".[dev]"
python -m pytest -q
python scripts/smoke_test.py
```

CI runs on every push/PR to `master` (Python 3.10 / 3.12 / 3.13 × Ubuntu + Windows).

## Design principles

1. **Offline-first** — local tools always work; remote is opt-in.  
2. **Locked local state** — exclusive locks around state/queue/events.  
3. **Safe remote writes** — identifier allowlists + base64 text over SSH.  
4. **No false-positive conflicts** — same project alone is not a conflict.  
5. **Generic by default** — zero personal paths in code.  
6. **Safe dispatch** — model tokens validated; Windows `.cmd`/`.bat` arg-mode prompts blocked.  

## Security notes

- Runs with the privileges of the executing user. Wire only into trusted local agents.  
- Pulled remote truth is treated as untrusted (filename/UTF-8 validation, atomic swap).  
- Local store defaults to Unix `0700` dirs / `0600` files where the OS allows.  
- SSH errors are scrubbed before return to clients.  

Full details: [`SECURITY.md`](SECURITY.md).

## License

MIT
