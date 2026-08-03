# MindSync AI

[![CI](https://github.com/adityarya24/mindsync-ai/actions/workflows/ci.yml/badge.svg)](https://github.com/adityarya24/mindsync-ai/actions/workflows/ci.yml)
[![PyPI version](https://img.shields.io/pypi/v/mindsync-ai.svg)](https://pypi.org/project/mindsync-ai/)
[![Python versions](https://img.shields.io/pypi/pyversions/mindsync-ai.svg)](https://pypi.org/project/mindsync-ai/)

**One Python MCP server** for multi-agent teams: shared memory, focus conflict detection, an in-process event bus, and CLI agent dispatch (Codex, Claude, Gemini/Antigravity, Cursor, Aider, Grok).

| Layer | What it does |
| --- | --- |
| **Core** | Local-first focus registry + optional durable facts over SSH |
| **Bus** | Typed events (`job.*`, `focus.changed`, `memory.updated`, …) with monotonic `seq` |
| **Dispatch** | Spawn headless CLI agents, track jobs, cancel process trees |

No cloud account required. Remote sync is opt-in. Zero hard-coded hosts or personal paths.

> **Rename (v1.1.0):** PyPI package and GitHub repo are now **`mindsync-ai`**  
> (older installs used `mindsync-mcp`). Import path and CLI stay the same:  
> `import mindsync` · `mindsync`.

## Install

```bash
pip install mindsync-ai
```

Requires Python 3.10+.

Run one-time onboarding after installation:

```bash
mindsync setup --mode auto
mindsync doctor
```

`setup` detects installed CLIs, registers MindSync where their supported MCP
surface allows it, and stores the shared orchestration policy. It is idempotent:
existing `mindsync` registrations are preserved unless `--force` is explicitly used.
Preview without changing anything with `mindsync setup --dry-run`.

From source:

```bash
git clone https://github.com/adityarya24/mindsync-ai.git
cd mindsync-ai
python -m pip install -e ".[dev]"
```

## MCP client config

Automatic setup currently supports Codex, Claude, Gemini CLI, Grok, and Cursor.
Detected CLIs without a documented MCP registration surface are reported but not
modified. Antigravity (`agy`) and Gemini CLI belong to one logical worker family:
Antigravity is the preferred worker backend, while Gemini CLI can host MindSync through
its native MCP commands and remains an alternate worker backend. Manual registration remains available:

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

## Tools (19)

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
| `delegate_task` | Run a CLI agent or role (foreground/background), optionally in an isolated `--worktree` |
| `route_task` | Preview capability-based automatic worker selection without launching anything |
| `list_agents` | List worker availability, capabilities, defaults, and routing priority |
| `get_orchestration_policy` | Read the active `auto` / `suggest` / `off` delegation policy |
| `list_models` | Discover available models for agents |
| `list_roles` | List configured roles and their agent, model, and effort mappings |
| `job_status` | Job status + PID reconciliation |
| `job_result` | Read job result file |
| `job_review` | Read mechanical review verdict (check results + git diff summary) |
| `job_cancel` | Cancel running job and kill its process tree |

Dispatch also auto-emits `job.started`, `job.completed`, and `job.failed` on the bus.

### CLI (dispatch)

```bash
mindsync-dispatch agents
mindsync-dispatch models <agent>
mindsync-dispatch roles
mindsync-dispatch run --role bulk "summarize README" --background --worktree
mindsync-dispatch run codex "summarize README" --worktree --effort high --check "pytest -q"
mindsync-dispatch run auto "implement and test the fix" --capability coding --capability testing --exclude-agent codex
mindsync-dispatch status
mindsync-dispatch review <job-id>
mindsync-dispatch result <job-id>
mindsync-dispatch cancel <job-id>
```

Jobs live under `~/.claude/agent-dispatch/jobs/` (override with `AGENT_DISPATCH_HOME`).  
Custom agents: `~/.claude/agent-dispatch/agents.json`.

`--worktree` is advisory isolation: nothing prevents an agent from writing outside its
working directory. Write the task text in terms of the current directory — a single absolute
path to another checkout will send the agent straight back to it, the job will still succeed,
and only the isolation will be lost. Dispatch appends a warning to the task text for you, but
the wording of your own task has to agree with it.

Built-in presets: `codex`, `claude`, `agy`, `gemini`, `cursor`, `aider`, `grok`.

The `agy` and `gemini` presets are two execution backends in the same
`gemini-antigravity` family, not two logical agents. If either one is the human-facing
orchestrator, automatic routing excludes both family members to prevent self-delegation.

### Automatic orchestration

Static roles are optional. The human-facing CLI can remain the orchestrator and
delegate to an automatically selected installed worker:

```text
delegate_task(
  prompt="Audit authentication and report concrete vulnerabilities",
  required_capabilities=["security", "review"],
  exclude_agents=["codex"]
)
```

Omitting `agent` in the MCP tool is equivalent to `agent="auto"`. The router ranks
installed agents using their declared `capabilities`, per-capability weights, and
`routingPriority`; the selection reason is returned and stored with the job. When
`required_capabilities` is omitted, MindSync infers broad needs from the task text.
Use `route_task` to preview a decision and `list_agents` to inspect the worker pool.

Custom agents can extend `agents.json` with routing metadata:

```json
{
  "name": "my-worker",
  "family": "my-provider-family",
  "bin": "my-cli",
  "capabilities": ["general", "coding", "testing"],
  "capabilityWeights": {"coding": 100, "testing": 90},
  "routingPriority": 75
}
```

Automatic routing only chooses a worker that is installed and on `PATH`. Authentication
is still verified by the selected CLI when the job starts. MindSync does not retry a
failed write-capable job on another worker automatically, avoiding duplicate edits.

The MCP handshake tells the human-facing CLI to act as orchestrator: keep tiny or
blocking work local, delegate useful bounded work, respect the original authorization,
verify worker output, and own final integration. MindSync identifies the caller from MCP
`clientInfo`; setup also tags new registrations with `MINDSYNC_CALLER_CLI` as a fallback,
allowing automatic routing to exclude the CLI talking to the human.

Policy is stored in `~/.mindsync/orchestration.json`:

```json
{
  "mode": "auto",
  "announce": true,
  "maxParallel": 3,
  "avoidHumanFacingAgent": true
}
```

Manage it without editing JSON:

```bash
mindsync config
mindsync config orchestration.mode suggest
mindsync config orchestration.announce false
mindsync config orchestration.maxParallel 4
```

- `auto` delegates useful work without blocking confirmation and briefly announces it.
- `suggest` previews a worker but launches nothing until the human approves.
- `off` keeps automatic work local; an explicit agent or role remains available when requested.

The parallel limit applies to automatically routed pending/running jobs. Existing CLI MCP
registrations are never silently replaced; use native CLI management or the explicit
`mindsync setup --force` path when replacement is intended.

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
| `MINDSYNC_LOCK_STALE_SECS` | `60` | Legacy compatibility setting; OS locks now recover automatically on process exit |

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
- `events.jsonl.seq` — constant-time monotonic sequence checkpoint
- `subscriptions.json` — event subscriptions  
- `orchestration.json` — persistent automatic delegation policy
- `compiled-truth/*.md` — pulled remote summaries  
- `.locks/` — persistent files holding kernel-managed exclusive locks

## Layout

```
mindsync-ai/                  # GitHub repo
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
2. **Locked local state** — crash-safe OS locks around state/queue/events.
3. **Safe remote writes** — identifier allowlists + base64 text over SSH.  
4. **No false-positive conflicts** — same project alone is not a conflict.  
5. **Generic by default** — zero personal paths in code.  
6. **Safe dispatch** — model tokens validated; Windows `.cmd`/`.bat` arg-mode prompts blocked.  
7. **Human-owned orchestration** — workers never expand authority; the user-facing CLI integrates and answers.

## Security notes

- Runs with the privileges of the executing user. Wire only into trusted local agents.  
- `mindsync setup --dry-run` is non-mutating. Setup preserves existing MCP registrations by default; Cursor JSON is merged atomically and backed up before forced replacement.
- Pulled remote truth is treated as untrusted (filename/UTF-8 validation, atomic swap).  
- Local store defaults to Unix `0700` dirs / `0600` files where the OS allows.  
- SSH errors are scrubbed before return to clients.  

Full details: [`SECURITY.md`](SECURITY.md).

## License

MIT
