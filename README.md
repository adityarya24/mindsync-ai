# MindSync AI

[![CI](https://github.com/adityarya24/mindsync-ai/actions/workflows/ci.yml/badge.svg)](https://github.com/adityarya24/mindsync-ai/actions/workflows/ci.yml)
[![PyPI version](https://img.shields.io/pypi/v/mindsync-ai.svg)](https://pypi.org/project/mindsync-ai/)
[![Python versions](https://img.shields.io/pypi/pyversions/mindsync-ai.svg)](https://pypi.org/project/mindsync-ai/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**[adityarya24.github.io/mindsync-ai](https://adityarya24.github.io/mindsync-ai/)**

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
- **Durable local state** — focus, events, and queued facts survive restarts.
- **Session memory** — decisions, blockers, and durable facts persist per project, and
  bounded prior context is replayed into the next session instead of being re-explained.
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
 ├── session memory (SQLite) ──► bounded context replayed on the next run
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

MindSync exposes 29 tools.

### Memory and focus

| Tool | Purpose |
| --- | --- |
| `get_sync_context` | Load local state and optionally refreshed remote truth |
| `update_focus` | Claim project/file focus and receive overlap warnings |
| `queue_durable_fact` | Write remotely or queue locally when offline |
| `sync_offline_facts` | Flush queued facts and refresh compiled truth |
| `pull_truth` | Safely pull compiled-truth Markdown |
| `health` | Inspect paths, queue depth, policy, and remote reachability |
| `session_start` | Start a tracked local memory session |
| `memory_checkpoint` | Save structured session state locally |
| `memory_bootstrap` | Retrieve bounded relevant context for a project |
| `memory_recall` | Recall related project facts with local embeddings |
| `memory_consolidate_preview` | Create a review-only consolidation proposal |
| `memory_consolidation_apply` | Explicitly apply a reviewed proposal |
| `memory_consolidation_undo` | Restore sources and remove a generated fact |
| `memory_consolidation_list` | List pending and historical proposals |
| `session_end` | Mark a session completed or failed |

Note: dispatch can also drive session memory automatically. `memory_mode` (MCP) or
`--memory-mode` (CLI) accepts `explicit`, `auto`, or `off`. The compatibility default
is `explicit`, where `memory_project` / `--memory-project <key>` opts in exactly as
before. `auto` uses an explicit key when supplied; otherwise it hashes Git's resolved
common directory into an opaque key shared by the checkout and its linked worktrees.
It never uses a raw path, repository name, or remote URL as the key. If Git identity
cannot be trusted, memory stays off and the job carries a non-fatal warning. `off`
is an explicit opt-out. When enabled, MindSync bootstraps bounded project context before
spawn, prepends a delimited compact prefix to the worker prompt, starts a local session,
and finalizes once on every terminal job outcome. Raw user prompts, injected prompts,
and full stdout/stderr are never written to session memory.
Memory failures surface as job warnings and do not fail otherwise successful jobs.
Job metadata includes `memoryMode`, `memoryProjectSource`, `memorySessionId`,
`memoryProject`, `memoryFinalized`, and `memoryFinalizeState` when applicable. Explicit
`session_*` / `memory_*` MCP tools remain available.
Session data is scoped to the specified project key and stored locally. Checkpoint text
is treated as untrusted and undergoes conservative secret redaction (for common
tokens/passwords/private keys) before persistence, though perfection is not guaranteed.

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
mindsync-dispatch run codex "continue the refactor" \
  --memory-project my-repo-key
mindsync-dispatch run codex "pilot inferred project memory" \
  --memory-mode auto
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

Remote jobs default to the safe `worker` execution mode. To run a configured
human-facing CLI as an orchestrator, opt in explicitly and name the agent (or
role) in the payload:

```bash
mindsync submit --repo /path/to/repo --prompt "plan and implement feature" \
  --execution-mode orchestrator --agent <configured-orchestrator-agent>
```

Or use a configured role instead: `--execution-mode orchestrator --role <configured-role>`.
Orchestrator submissions without an explicit `--agent` or `--role` are rejected.

Two further submit options control how long a job may run and what the worker
does with the result:

```bash
mindsync submit --repo /path/to/repo --prompt "implement feature" --agent codex \
  --timeout-seconds 1800 --commit
```

`--timeout-seconds` bounds a single agent run. It accepts `0 < t <= 3600` and
defaults to `900`; the value is carried through to the worker, so a job that
overruns is stopped on the machine that is executing it.

`--commit` is opt-in. After a **successful** run only, the worker stages and
commits its checkout and records the resulting SHA in the job result. It never
pushes — the worker holds no non-interactive git credentials — it refuses a
tree that was already dirty before the run, and it never commits a run that
failed or timed out. Without the flag the worker leaves the checkout untouched
for you to review.

An orchestrator job is accepted only when the local worker owner also enables
the boundary with `MINDSYNC_WORKER_ALLOW_ORCHESTRATOR=true` (or the one-shot
`mindsync worker --once --allow-orchestrator` / loop `--allow-orchestrator`
flag). The remote repository allow-list, branch check, write sandbox, and
result lifecycle apply in both modes. The orchestrator process is allowed to
use MindSync delegation; every child dispatch remains a depth-1 worker with
`MINDSYNC_WORKER=1` and cannot delegate recursively. Legacy payloads without
the mode/depth fields remain worker jobs.

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
# Optional, privileged local opt-in for explicit orchestrator payloads:
$env:MINDSYNC_WORKER_ALLOW_ORCHESTRATOR = "true"
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
| `MINDSYNC_WORKER_ALLOW_ORCHESTRATOR` | `false` | Local opt-in required before an explicit remote orchestrator job can run |
| `MINDSYNC_MEMORY_MODEL_URL` | `http://127.0.0.1:11434` | Loopback-only Ollama-compatible API base URL |
| `MINDSYNC_MEMORY_EMBEDDING_MODEL` | empty | Local model used by semantic recall |
| `MINDSYNC_MEMORY_CONSOLIDATION_MODEL` | empty | Local model used for consolidation proposals |
| `MINDSYNC_MEMORY_MODEL_TIMEOUT` | `60` | Local model request timeout (greater than 0, at most 300 seconds) |
| `MINDSYNC_STANDALONE_MEMORY_MODE` | `auto` | Standalone adapter memory mode: `auto` or `off` |

## Session memory

MindSync provides local, structured session memory via SQLite (`session_memory.db`).

- **Budget and priority semantics**: `memory_bootstrap` bounds its serialized envelope
  to `budget_chars` and scans at most 200 sessions per priority class. Classes are
  strict: sessions with durable facts in any retained checkpoint come first, then
  sessions whose latest checkpoint has unresolved blockers or pending items, then
  routine history — so routine floods can never crowd out important sessions. Durable
  facts are merged from every retained checkpoint of an included session, and up to
  three earlier failed or blocked checkpoints are attached as `earlier_checkpoints`.
  Records that do not fit are dropped.
- **Redaction**: Memory writes apply conservative masking for common token, password,
  and private-key patterns. This is best-effort protection, not a substitute for keeping
  credentials out of checkpoints. Lists and objects remain structured after redaction.
- **Lifecycle**: agents drive memory explicitly with `session_start`,
  `memory_checkpoint`, `memory_bootstrap`, and `session_end`. Dispatch can run the
  same lifecycle on the shared runner path (no per-vendor adapter hooks). The
  `explicit` mode requires `memory_project` / `--memory-project`, `auto` can infer
  an opaque Git checkout/worktree identity, and `off` disables dispatch memory.
- **Coverage limits**: Automatic dispatch memory records compact job status,
  bounded changed-file paths, and check pass/fail summaries—not agent transcripts,
  raw prompts, or check output tails. Use explicit `memory_checkpoint` when agents
  need richer handoff detail.
- **Semantic recall**: `memory_recall` embeds the redacted cue and active facts with
  the configured local model, then performs exact cosine ranking through `sqlite-vec`.
  The cue is never persisted. Embeddings are cached by model, dimension, and fact-text
  hash. Indexing is capped at the 2,000 strongest active facts and commits bounded
  batches independently, so a later provider failure retains completed progress.
- **Reversible consolidation**: consolidation is deliberately two-step. Preview asks
  a loopback-only local model to generalize a related fact cluster and stores only the
  redacted proposal plus cited fact IDs. Apply must be requested explicitly; it links
  every source to the generated fact and copies checkpoint provenance. Undo restores
  the individual sources and removes the generated replacement atomically. Pending
  proposals are capped at 100 per project and remain recoverable through
  `memory_consolidation_list` / `mindsync memory proposals`.
- **Local-model boundary**: model URLs must resolve explicitly to loopback; remote
  hosts and credential-bearing URLs are rejected. MindSync never downloads a model.
  Only already-redacted durable-fact text is sent for consolidation—never prompts,
  transcripts, stdout, stderr, or check-output tails.

### Standalone CLI lifecycle (Codex pilot)

Phase 3B lets a CLI session use the same memory lifecycle without going through a
MindSync dispatch job. The first adapter is `mindsync-codex-hook`, wired to Codex's
native `SessionStart`, `Stop`, and `SessionEnd` hooks. It infers the same opaque Git
project identity used by dispatch, injects bounded prior context at start/resume,
checkpoints only a compact changed-file list, and finalizes the mapped memory session.
Repeated terminal events and unchanged `Stop` events are idempotent. A later start
conservatively finalizes stale or interrupted mappings before creating a new episode.

The repository includes [`.codex/hooks.json`](.codex/hooks.json) as a working project
configuration. After installing MindSync, copy that file into a trusted repository's
`.codex` directory, review/enable it through Codex's hook trust flow, then restart the
Codex session. Project-local hooks do not replace user-level Codex configuration.

```bash
python -m pip install mindsync-ai
mindsync-codex-hook < codex-hook-event.json  # adapter smoke test
```

Set `MINDSYNC_STANDALONE_MEMORY_MODE=off` for an explicit opt-out. Hook and memory
failures are reported as non-fatal warnings; they do not fail the Codex action.
The adapter reads only the event type, external session ID, workspace, and start
source. It deliberately ignores prompt, transcript, assistant-message, stdout,
stderr, and check-output fields, and never promotes automatic lifecycle data into
durable facts.

## Inspecting memory

Human-facing commands for the local session-memory database:

```bash
mindsync memory stats                          # totals, per-project counts, db size
mindsync memory list --project my-repo         # sessions, most recently active first
mindsync memory show <session-id>              # one session with every checkpoint
mindsync memory prune --older-than-days 30     # dry run: what would be deleted?
mindsync memory recall --project my-repo --query "database decision"
mindsync memory consolidate --project my-repo  # preview only; sources unchanged
mindsync memory apply <proposal-id>             # explicit reviewed mutation
mindsync memory undo <generated-fact-id>        # restore individual sources
mindsync memory proposals --project my-repo     # recover review/audit IDs
```

`prune` only considers ended sessions, always protects active sessions and any
session carrying durable facts in any retained checkpoint, and supports `--keep-last N`
to preserve the most recent N ended sessions per project (keep-last is applied before
the age filter, so fresher sessions already satisfy it). Candidate selection and
deletion run in one transaction, so a concurrently written durable checkpoint is never
deleted. Nothing is deleted unless `--yes` is passed.
The inspection and prune commands accept `--json`; Tier 2 commands always emit JSON.

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
├── session_memory.db      local SQLite session memory
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
│   ├── memory.py           local SQLite session memory
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
