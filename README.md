# MindSync AI

[![CI](https://github.com/adityarya24/mindsync-ai/actions/workflows/ci.yml/badge.svg)](https://github.com/adityarya24/mindsync-ai/actions/workflows/ci.yml)
[![PyPI version](https://img.shields.io/pypi/v/mindsync-ai.svg)](https://pypi.org/project/mindsync-ai/)
[![Python versions](https://img.shields.io/pypi/pyversions/mindsync-ai.svg)](https://pypi.org/project/mindsync-ai/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

### One orchestrator. Your coding agents. Shared context.

MindSync AI is a local-first MCP orchestration layer for coding agents. It turns the
CLI already working with you into the lead orchestrator: MindSync discovers available
workers, routes tasks by capability, supervises execution, and keeps every session
aligned through shared focus, events, and durable memory.

Use Codex, Claude, Antigravity/Gemini, Grok, Cursor, and Aider as one coordinated
system—without introducing another hosted control plane. MindSync works locally by
default, requires no MindSync account, and makes remote synchronization entirely
optional.

## Why MindSync?

Running several capable agents is easy. Keeping them coordinated is the hard part.
Without a shared layer, agents duplicate work, overwrite files, lose decisions between
sessions, and force the user to manually choose a worker for every task.

MindSync provides:

- **Automatic orchestration** — the human-facing CLI decides when delegation is useful.
- **Capability-based routing** — workers are ranked by task fit, availability, and priority.
- **Conflict prevention** — active file and project focus is visible before work begins.
- **Shared local memory** — session state, events, and queued facts survive restarts.
- **Safe process control** — tracked jobs, timeouts, cancellation, and process-tree cleanup.
- **Optional durable sync** — important facts can be shared through your own SSH host.
- **Explainable decisions** — every automatic route includes the reason and candidate scores.

## How it works

```text
You
 │
 ▼
Human-facing CLI (orchestrator)
 │  MCP
 ▼
MindSync AI
 ├── capability router ──────► Codex / Claude / AGY / Gemini / Grok / Cursor / Aider
 ├── focus + conflict map
 ├── event bus + job records
 └── local durable state ────► optional SSH/VPS truth store
```

The orchestrator remains responsible for planning, authorization, integration, and the
final answer. Delegated workers receive bounded tasks and cannot recursively delegate
through MindSync.

## Quick start

Install MindSync:

```bash
pip install mindsync-ai
```

Run one-time onboarding:

```bash
mindsync setup --mode auto
mindsync doctor
```

Restart the configured CLI sessions. From then on, the CLI can use MindSync
automatically; the user does not need to name a worker for every task.

`setup` is idempotent. Existing MCP registrations are preserved unless `--force` is
explicitly supplied, and a non-mutating preview is available:

```bash
mindsync setup --dry-run
```

Requires Python 3.10 or newer.

### Install from source

```bash
git clone https://github.com/adityarya24/mindsync-ai.git
cd mindsync-ai
python -m pip install -e ".[dev]"
```

## Supported clients and workers

MindSync distinguishes an **MCP host** from a **worker backend**. A CLI may support
one or both roles.

| CLI | MCP host setup | Worker preset | Notes |
| --- | --- | --- | --- |
| OpenAI Codex | Native | Built in | General coding, debugging, testing, and DevOps |
| Anthropic Claude | Native | Built in | Architecture, reasoning, review, and large-context work |
| Google Gemini CLI | Native | Built in | Alternate backend in the Gemini/Antigravity family |
| Antigravity (`agy`) | Via Gemini CLI host | Built in | Preferred worker backend in the Gemini/Antigravity family |
| Grok CLI | Native | Built in | Research, reasoning, review, and security-oriented work |
| Cursor Agent | JSON setup | Built in | Coding and repository work |
| Aider | — | Built in | Focused code editing worker |

Antigravity and Gemini CLI are two execution backends in one logical
`gemini-antigravity` family—not two separate logical agents. When either backend is
the human-facing orchestrator, MindSync excludes both from automatic worker selection
to prevent self-delegation.

Detected clients without a supported registration surface are reported but never
modified through guessed or undocumented configuration.

## Automatic orchestration

Static roles remain supported, but they are optional. Omitting `agent` from
`delegate_task` is equivalent to `agent="auto"`.

```text
delegate_task(
  prompt="Audit authentication and report concrete vulnerabilities",
  required_capabilities=["security", "review"]
)
```

The router:

1. infers capabilities when none are supplied;
2. filters out missing CLIs and explicit exclusions;
3. excludes the human-facing agent family;
4. ranks eligible workers using capability weights and routing priority;
5. stores and returns the complete routing explanation.

Use `route_task` to preview a decision and `list_agents` to inspect the live worker
inventory.

### Orchestration modes

Policy is stored in `~/.mindsync/orchestration.json`.

| Mode | Behaviour |
| --- | --- |
| `auto` | Delegates useful work automatically and briefly announces it |
| `suggest` | Returns the recommended worker without launching a job |
| `off` | Disables automatic delegation; explicitly selected agents and roles still work |

Manage policy from the CLI:

```bash
mindsync config
mindsync config orchestration.mode suggest
mindsync config orchestration.announce false
mindsync config orchestration.maxParallel 4
```

The default parallel limit is three automatically routed pending or running jobs.
MindSync never retries a failed write-capable task on another worker automatically,
preventing duplicate edits.

### Custom workers

Add custom adapters to `~/.claude/agent-dispatch/agents.json`:

```json
{
  "agents": [
    {
      "name": "my-worker",
      "family": "my-provider-family",
      "bin": "my-cli",
      "input": "stdin",
      "capabilities": ["general", "coding", "testing"],
      "capabilityWeights": {"coding": 100, "testing": 90},
      "routingPriority": 75
    }
  ]
}
```

Authentication remains the responsibility of each worker CLI.

## Shared context and coordination

MindSync combines three coordination layers:

| Layer | Responsibility |
| --- | --- |
| **Core** | Local-first focus registry, conflict detection, durable facts, optional SSH sync |
| **Event bus** | Typed `job.*`, `focus.changed`, and `memory.updated` events with monotonic sequence IDs |
| **Dispatch** | Worker discovery, routing, execution, job review, cancellation, and cleanup |

A typical session uses:

1. `get_sync_context(agent_name)` to load current state and compiled truth.
2. `update_focus(...)` before editing to detect overlapping work.
3. `delegate_task(...)` for bounded work that benefits from another agent.
4. `queue_durable_fact(...)` for high-confidence decisions worth retaining.
5. `sync_offline_facts(...)` when an optional remote store comes back online.

## MCP tools

MindSync exposes 20 tools.

### Memory and focus

| Tool | Purpose |
| --- | --- |
| `get_sync_context` | Load local state and optionally refreshed remote truth |
| `update_focus` | Claim project/file focus and receive overlap warnings |
| `queue_durable_fact` | Write remotely or queue locally when offline |
| `sync_offline_facts` | Flush queued facts and refresh compiled truth |
| `pull_truth` | Safely pull compiled-truth Markdown |
| `health` | Inspect paths, queue depth, policy, and remote reachability |

### Event bus

| Tool | Purpose |
| --- | --- |
| `publish_event` | Publish a typed event |
| `poll_events` | Read events after a sequence number |
| `subscribe_events` | Subscribe an agent to selected event types |

### Dispatch and orchestration

| Tool | Purpose |
| --- | --- |
| `delegate_task` | Run an explicit or automatically selected worker |
| `route_task` | Preview automatic worker selection |
| `list_agents` | Inspect availability, capabilities, families, and defaults |
| `get_orchestration_policy` | Read active delegation policy |
| `list_models` | Discover models exposed by worker CLIs |
| `list_roles` | Inspect configured static roles |
| `job_status` | Reconcile and report job state |
| `job_wait` | Hold the orchestration turn open until a background job finishes, then return its review |
| `job_result` | Read captured worker output |
| `job_review` | Read checks and Git-diff review results |
| `job_cancel` | Cancel a job and terminate its process tree |

### Background completion ping

After `delegate_task(..., background=True)` returns a job ID, call
`job_wait(job_id)` immediately. The MCP call remains pending while the worker runs
and returns a completion ping with the mechanical review when the job reaches
`done`, `failed`, or `cancelled`. This keeps the orchestrator's turn alive and removes
the need for the user to ask for repeated status checks. If a job exceeds the wait
timeout, call `job_wait` again to continue watching it.

MCP servers cannot reopen a chat turn after the client has closed it, so the
orchestrator must start `job_wait` before ending its response.

## Dispatch CLI

```bash
mindsync-dispatch agents
mindsync-dispatch models <agent>
mindsync-dispatch roles
mindsync-dispatch run auto "implement and test the fix" \
  --capability coding --capability testing
mindsync-dispatch run codex "summarize README" \
  --worktree --effort high --check "pytest -q"
mindsync-dispatch status
mindsync-dispatch review <job-id>
mindsync-dispatch result <job-id>
mindsync-dispatch cancel <job-id>
```

Jobs live under `~/.claude/agent-dispatch/jobs/`; override this with
`AGENT_DISPATCH_HOME`.

`--worktree` provides advisory isolation. Agents still run with the permissions of
the current user, so task wording and working-directory boundaries must agree.

## Manual MCP configuration

If native setup is unavailable, register the server manually:

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

On Windows, use the full path to the appropriate `python.exe` when client processes
do not share the same `PATH`.

## Optional remote synchronization

Core coordination works without a network connection. To share durable facts through
an always-on host, configure:

```bash
export MINDSYNC_SSH_HOST=my-server
export MINDSYNC_REMOTE_ROOT=/opt/mindsync
```

SSH must support non-interactive key authentication. See [`.env.example`](.env.example)
and [`examples/remote/`](examples/remote/).

For a VPS + laptop setup:

1. deploy the scripts from [`examples/remote/`](examples/remote/) on the VPS;
2. point the laptop at that host with the two variables above;
3. for sync-only use, leave remote variables empty on the VPS itself; remote dispatch submitters
   set only `MINDSYNC_REMOTE_ROOT` so they write into that local durable store.

### Remote Dispatch Queue & Worker

MindSync enables a remote orchestrator (e.g., running on a VPS) to submit work into a queue on the remote store, which a worker running on the local machine claims and executes within its own interactive session.

#### Submitting a job (remote side)

On the VPS, point only `MINDSYNC_REMOTE_ROOT` at the existing local durable-store root; no SSH
host is needed because the queue is local there.

```bash
export MINDSYNC_REMOTE_ROOT=/opt/mindsync
mindsync submit --repo /path/to/repo --prompt "implement feature" --agent codex
mindsync status <job-id>
```

#### Running the worker (local side)

> [!IMPORTANT]
> The worker **must** run in the user's interactive desktop session (for example, a normal
> PowerShell window). Do not launch it through SSH or as a Windows service in session 0, because
> tool sandboxes such as Codex's runner pipe require that interactive session.

Configure worker environment:

```powershell
$env:MINDSYNC_SSH_HOST = "mindsync-vps"
$env:MINDSYNC_REMOTE_ROOT = "/opt/mindsync"
$env:MINDSYNC_WORKER_ALLOWED_ROOTS = "C:\work\project1;C:\work\project2"
```

Keep a non-default SSH port in the selected host's `~/.ssh/config` entry (this setup uses port
2422); MindSync intentionally has no separate port setting.

Start the worker loop:

```bash
mindsync worker
```

Or process at most one job and exit:

```bash
mindsync worker --once
```

### Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `MINDSYNC_HOME` | `~/.mindsync` | Local data root |
| `MINDSYNC_SSH_HOST` | empty | SSH host; empty disables remote sync |
| `MINDSYNC_REMOTE_ROOT` | empty | Remote MindSync root |
| `MINDSYNC_REMOTE_ENV_FILE` | `config/mindsync.env` | Remote environment file |
| `MINDSYNC_REMOTE_WRITE_SCRIPT` | `tools/mindsync_fact.py` | Remote fact writer |
| `MINDSYNC_REMOTE_CONSOLIDATE_SCRIPT` | `tools/mindsync_consolidate.py` | Remote consolidation command |
| `MINDSYNC_REMOTE_TRUTH_SUBDIR` | `compiled-truth` | Compiled truth directory |
| `MINDSYNC_SSH_TIMEOUT` | `3` | SSH connection timeout in seconds |
| `MINDSYNC_FOCUS_STALE_SECS` | `7200` | Age after which focus is ignored |
| `MINDSYNC_REMOTE_CACHE_TTL` | `30` | Remote probe cache lifetime |
| `MINDSYNC_LOCK_TIMEOUT` | `5` | Local lock wait in seconds |
| `MINDSYNC_WORKER_ID` | `laptop-worker` | Worker identifier string |
| `MINDSYNC_WORKER_POLL_SECS` | `30` | Worker poll interval in seconds |
| `MINDSYNC_WORKER_CLAIM_STALE_SECS` | `300` | Stale claim threshold in seconds |
| `MINDSYNC_WORKER_ALLOWED_ROOTS` | empty | Semicolon- or comma-separated allow-list of repository roots the worker may execute in |

## Local data

By default, state is stored under `~/.mindsync`:

```text
~/.mindsync/
├── local-state.json       active project and per-agent focus
├── local-audit.jsonl      append-only action audit
├── offline_queue.jsonl    durable facts waiting for remote sync
├── events.jsonl           event bus
├── events.jsonl.seq       monotonic sequence checkpoint
├── subscriptions.json     event subscriptions
├── orchestration.json     automatic delegation policy
├── compiled-truth/        pulled durable summaries
└── .locks/                kernel-managed lock files
```

## Safety model

- The human-facing CLI owns authorization, integration, and the final answer.
- Delegated workers cannot recursively delegate through MindSync.
- Automatic routing never expands the permissions granted by the user.
- Setup preserves existing registrations and supports a non-mutating dry run.
- Cursor configuration is merged atomically and backed up before forced replacement.
- Local state uses crash-safe OS locks and atomic file replacement.
- Remote identifiers are allowlisted; text is encoded safely before SSH transfer.
- Pulled truth is treated as untrusted and validated before replacing local files.
- Job cancellation terminates the spawned process tree.

MindSync runs with the privileges of the current user. Connect only trusted local
agents. See [`SECURITY.md`](SECURITY.md) for the complete security policy.

## Development

```bash
python -m pip install -e ".[dev]"
python -m ruff check .
python -m pytest -q
python scripts/smoke_test.py
```

CI covers Python 3.10, 3.12, and 3.13 on Ubuntu and Windows.

## Project structure

```text
mindsync-ai/
├── mindsync/
│   ├── server.py           FastMCP tools
│   ├── onboarding.py       CLI discovery and safe registration
│   ├── orchestration.py    persistent delegation policy
│   ├── storage.py          atomic JSON/JSONL storage and locks
│   ├── bridge.py           optional SSH/SCP transport
│   ├── bus/                typed local event bus
│   └── dispatch/           adapters, router, runner, jobs, and CLI
├── examples/remote/        optional remote-store scripts
├── tests/
└── pyproject.toml
```

> Upgrading from the old `mindsync-mcp` package name? The PyPI package and repository
> are now `mindsync-ai`; the Python import and CLI remain `mindsync`.

## License

[MIT](LICENSE)
