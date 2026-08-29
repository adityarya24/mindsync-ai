# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.7.0] - 2026-08-29

### Added

- Opt-in reactive provider-quota handoff for isolated dispatch jobs (#46). With
  `--on-limit handoff`, a narrowly classified exhaustion failure cools the
  configured provider/account, records a new attempt, and transfers the same
  worktree to the next available agent only after process-tree shutdown and an
  atomic worktree-lease handoff. The default remains `stop` and has no cooldown
  side effect. `mindsync-dispatch limits` shows or clears active cooldowns.
- Pluggable dispatch usage readers with a native Codex OAuth implementation
  (#47). Readers return a safe unavailable state for missing, malformed, or
  unauthenticated sources. Configure global thresholds in `agents.json`
  `usage` (disabled by default) and per-adapter `usageReader`.
- Opt-in pre-emptive usage polling and checkpoint-gated handoff (#47). When
  `usage.enabled` is true and `--on-limit handoff` is set on a worktree job,
  dispatch skips cooling or over-threshold accounts before spawn, polls during a
  running attempt, and transfers only when a privacy-safe MindSync checkpoint
  already exists; otherwise it records `preemptiveBlocked` and keeps the
  reactive quota floor. There is no generic CLI control channel and dispatch
  never asks agents to write `HANDOFF.md`. Job status exposes usage
  evaluation, skips, blocks, and handoffs without persisting raw usage, auth, or
  source payloads.

### Fixed

- Codex standalone hook bounds remaining seconds to the work budget so float
  rounding cannot overshoot the deadline (#48).
- Windows queue writers serialize under contention with a longer bounded lock
  deadline, exponential backoff, and per-name thread mutexes so burst enqueue
  workloads no longer time out on `msvcrt` locks (#48).
- Windows lock tuning env vars (`MINDSYNC_QUEUE_LOCK_TIMEOUT`,
  `MINDSYNC_LOCK_CONTENTION_BACKOFF_BASE`, `MINDSYNC_LOCK_CONTENTION_BACKOFF_MAX`)
  are validated at config load; the per-name thread-lock registry uses a
  weak-value dictionary so ephemeral job lock names do not pin mutexes for the
  process lifetime (#48).
- Usage-aware automatic routing evaluates thresholds during initial agent
  selection via the injectable evaluator seam (#48).

### Security

- Pull-request publication uses the separately stored operator task, never a
  successor prompt containing a private structured handoff checkpoint.

## [1.6.0] - 2026-08-28

### Added

- `onComplete: pr` in the orchestration policy (or `MINDSYNC_ON_COMPLETE=pr`
  for one run) pushes a finished job's branch and opens a pull request for it,
  so the path to review belongs to the orchestrator instead of being an
  instruction each agent may or may not carry. Nothing is ever merged.

  Only the operator's task is published. A stored job prompt is not the task:
  it carries the injected memory bootstrap — the project's decisions, blockers
  and durable facts — which is why it is written with mode 0o600. That framing
  is stripped, and publishing is abandoned rather than guessed at if it cannot
  be removed cleanly.

  A pull request also requires the mechanical checks to have passed; a zero
  exit code is not a passing build, and neither is a check that was requested
  and produced no result — that question is asked of the review gate itself,
  so the two cannot drift apart. Uncommitted work is committed onto the job's
  own branch first, with commit hooks left enabled, and a commit the
  repository refuses stops the publish rather than opening a pull request
  without the work. Every path the branch adds or changes is screened for
  things that look like secrets — case-insensitively, at any depth, including
  what the agent committed itself — and nothing is pushed when one matches.

  Set it per project with `mindsync config onComplete pr --project <path>`,
  which overrides the global default for that repository only. Project
  overrides live in MindSync's own policy file rather than in the repository,
  because a dispatched agent can write to the repository.

  Defaults to `branch`, the previous behaviour, reuses an existing open PR
  instead of failing, targets the branch the job was cut from, and reports on
  the job — and in `mindsync status` — whenever it declines.

### Changed

- GitHub Pages landing matches 1.6.0: OpenCode is listed, copy-paste commands
  are real (`mindsync-dispatch run auto`, `mindsync agents`), and setup no
  longer claims every MCP harness is auto-onboarded. The page now also carries
  branded social previews, live PyPI download stats, clearer CLI-versus-dispatch
  guidance, Windows MCP configuration notes, privacy-preserving analytics, and
  a simplified SVG mark across the navigation, hero, and favicon.
- README is a front door again: quick start, supported CLIs, and pointers.
  Tool catalogs, remote-worker internals, and memory-engine detail live in
  [`SECURITY.md`](SECURITY.md), [`examples/remote/`](examples/remote/), and
  the MCP server itself. It now documents the per-project pull-request
  completion policy and its refusal boundaries.
- GitHub Actions are pinned to Node 24-compatible action releases ahead of the
  hosted-runner migration.

### Fixed

- `list_agents` no longer runs every host CLI to answer a routing question.
  Filling the MCP columns means running each host's `mcp list`, which is not a
  read: the host boots its own configuration, plugins included. Where a plugin
  holds a single-instance lock — a chat channel bound to one session — the copy
  started by the probe takes the lock, and the session that had it goes silent.
  The MCP tool now asks for binary and capability information only; `mindsync
  agents` and `mindsync doctor` still probe and still report the full truth.

- `probe_mcp_capable` asks `mcp --help` before falling back to `mcp list` for
  CLIs that do not answer help. Discovery avoids starting known hosts while
  retaining compatibility with unusual MCP-capable CLIs.

## [1.5.3] - 2026-08-27

### Fixed

- Ended memory sessions stay terminal: a late dispatch checkpoint cannot
  resurrect them or write new facts.
- Dispatch memory finalization is retryable. A degraded first attempt no
  longer marks the job finalized forever; the terminal checkpoint id is
  stable across retries, and replayed session context is labeled untrusted.
- Replayed dispatch context ASCII-escapes Unicode line separators (`U+0085`,
  `U+2028`, and `U+2029`), so untrusted checkpoint text cannot forge framing
  delimiters.
- Legacy `~/.claude/agent-dispatch` migration runs once under a lock and
  writes a completion marker, so later files in the old folder cannot leak
  into `~/.mindsync/dispatch`.
- Release tagging now requires the tag to match the package version and the
  commit to be on `origin/master` with a successful **push** CI run on that
  SHA, not merely any completed CI check.

## [1.5.2] - 2026-08-27

### Added

- OpenCode is a first-party MCP host. `mindsync setup` writes MindSync into
  `~/.config/opencode/opencode.jsonc` (or `opencode.json`) the same way it
  writes Cursor's JSON config, because `opencode mcp add` is interactive and
  cannot be scripted.
- `mindsync setup` scans PATH for recognised coding-agent CLIs beyond the
  first-party host list and adds them to the user dispatch roster with
  `coding` capability. Discovery is on by default, off when `--cli` is passed,
  and skippable with `--no-discover`. System directories and a denylist of
  ordinary tools are ignored. Heavy capability tags are never auto-assigned.
  A binary MindSync cannot name is reported as a suggestion rather than
  registered, because confirming what it is would mean executing it.
- `mindsync register` lands an agent in the user dispatch roster and, when the
  CLI is a known MCP host, installs MindSync as its MCP server. It verifies
  `--version` / `mcp list` (or the host JSON file), is idempotent, and refuses
  heavy capability tags (`security`, `large-context`, `multimodal`) without
  `--confirm`.
- `mindsync agents` (and `mindsync-dispatch agents`) report binary present, MCP
  installed, and routable for every roster entry.

### Changed

- Dispatch roster and jobs now live under `~/.mindsync/dispatch/` instead of
  `~/.claude/agent-dispatch/`. On first use of the default home, existing
  files are copied from the old path and never overwritten.
  `AGENT_DISPATCH_HOME` still overrides the location.
- Codex and Cursor presets now advertise `refactoring`, so `--capability refactoring`
  no longer depends on Aider being installed.

### Fixed

- PATH discovery no longer executes unrecognised binaries. It decided a name
  looked agent-ish, then confirmed it by running `<bin> mcp list` — but on an
  ordinary Linux host those name shapes also match `ssh-agent`, `gpg-agent`,
  `pkttyagent`, `systemd-tty-ask-password-agent` and local `*-fleet-restart`
  scripts. Running them spawned background daemons, blocked on a TTY password
  prompt, and restarted live services; with a 30s timeout on two probes each,
  `setup` could also stall for many minutes. Only recognised agent CLIs are
  probed and registered now, unknown ones are reported for the operator to add,
  and the `amp`/`gpt`/`llm` hints are anchored to name segments so `uclampset`
  and `sg_timestamp` no longer match.

## [1.5.1] - 2026-08-27

### Added

- `mindsync setup` registers Codex standalone memory hooks at `~/.codex/hooks.json`
  when the Codex CLI is installed. Existing hook entries are merged and backed up;
  `--no-hooks` skips the step and `--force` rewrites the MindSync hook blocks.
- `mindsync doctor` reports session-memory database health, whether this directory
  has a git identity, and whether Codex standalone hooks are configured.

## [1.5.0] - 2026-08-27

### Added

- Phase 3B standalone session lifecycle with a first-party Codex native-hook adapter.
  `SessionStart` bootstraps bounded project context and starts or resumes a mapped
  memory episode; `Stop` records only a compact, deduplicated changed-file milestone;
  and `SessionEnd` finalizes idempotently. Per-session private state files isolate
  concurrent CLIs, interrupted `finalizing` states are retryable, and a bounded stale
  reaper conservatively closes abandoned episodes. Automatic adapter data never
  includes prompts, transcripts, assistant messages, stdout/stderr, check tails, raw
  workspace/branch values, or durable facts. `MINDSYNC_STANDALONE_MEMORY_MODE=off`
  disables the integration without affecting the CLI action.
- Tier 2 semantic recall and reversible fact consolidation (session-memory schema
  v3). `memory_recall` caches local embeddings by model/text hash and ranks active
  project facts with exact `sqlite-vec` cosine distance. Consolidation is preview-first:
  a generated fact is never applied without a separate explicit action, every cited
  source remains traceable through checkpoint provenance, and undo atomically restores
  the sources while retaining the generated fact ID in proposal history. CLI and MCP
  surfaces expose recall, preview, list, apply, and undo. Recall indexing is capped and
  independently committed in bounded batches so retries preserve forward progress;
  pending proposals are capped per project to keep the review queue bounded.
- Loopback-only Ollama-compatible model adapter for embeddings and strict structured
  consolidation output. Remote or credential-bearing model URLs are rejected, request
  failures are visible without response-body leakage, cues are redacted and never
  persisted, and consolidation receives only already-redacted durable-fact text.
- Phase 3A dispatch memory rollout modes: `auto` infers a privacy-safe opaque
  identity shared by a Git checkout and its linked worktrees, `explicit` requires a
  supplied project key, and `off` is an explicit opt-out. A supplied
  `memory_project` overrides inference; failed or untrustworthy inference disables
  memory with a visible non-fatal job warning instead of guessing from a path or
  repository name. CLI callers use `--memory-mode`; MCP callers use `memory_mode`.
  After a real-dispatch pilot, `auto` is the default.
- Project-scoped fact store (session-memory schema v2). Durable facts are still written
  to their checkpoint exactly as before, and are now also promoted into `facts` /
  `fact_sources` keyed by project, so the same lesson recorded in twenty sessions becomes
  one row instead of twenty unmerged payloads. Each fact carries `first_seen`,
  `last_recalled`, `recall_count`, and `source_count`; `source_count` rises only when a
  genuinely new checkpoint asserts the fact.
- `memory_bootstrap` now returns `project_facts` ahead of `bootstraps`, ordered by
  strength (recalls plus asserting checkpoints, recency breaking ties) and capped at a
  quarter of `budget_chars` so facts cannot starve the session history that gives them
  context. Serving a fact records the recall; a failed counter update degrades the
  strength signal rather than failing the read.
- `memory_stats` reports `total_facts` and a per-project `facts` count.

### Fixed

- A standalone `SessionEnd` that ran out of budget marked a healthy session
  `finalizing` before checking the deadline, leaving the state file finalizing while
  the session row stayed `active` — unresumable until the 24-hour stale reaper reached
  it. The budget is now checked before anything is mutated.
- The standalone entry points raised `TimeoutError` on an exhausted budget. Memory is
  optional in this module and every other failure degrades with a warning, so a spent
  budget now does too; only the Codex hook wrapped these calls, and anything else
  would have seen an exception where it previously saw a value.
- The stale-session reaper was handed the entire remaining budget, so a contended
  cleanup could consume it all and leave the new session with no context. It is capped
  at a share of the budget.
- Undoing a consolidation left the proposals that application had retired at
  `superseded`, even though their source facts were restored and they were applicable
  again. Only proposals whose sources are all active again are revived: matching on
  project alone also revived proposals retired by a *different* consolidation, which
  rejoined the queue, consumed the per-project cap, and failed on apply.
- Consolidation wedged shut once generated facts filled the candidate window.
  `is_generated` was filtered after the SQL `LIMIT`, and an applied proposal seeds its
  generated fact with the summed `source_count` of every fact it replaced — so
  generated facts outrank their own sources. After enough consolidations the whole
  window was generated rows and `memory_consolidate_preview` reported "not enough
  unconsolidated facts" with thousands sitting below the cut. The predicate now lives
  in the query; recall still sees generated facts, which are the better answer.
- Pending consolidation proposals had no exit. Applying one supersedes its source
  facts, which makes every other proposal citing them permanently unappliable — yet
  they stayed `pending` and counted against the per-project cap, eventually blocking
  consolidation with no supported way to clear the queue. Applying a proposal now
  retires the ones it invalidates and reports them as `superseded_proposals`.
- Codex lifecycle hooks now share one operation-wide deadline across Git, session
  locks, and SQLite waits, preserving headroom inside Codex's three-second hook limit.
- Superseded consolidation proposals can be audited explicitly through the Python API
  and `mindsync memory proposals --status superseded`.
- The Codex hook could outlive its own 3-second budget. Project inference ran two git
  probes at the dispatch default of 15 seconds each, so a slow checkout or a held
  `index.lock` got the hook killed — and a kill between `session_start()` and the state
  file being written stranded an active session row that the state-file-driven reaper
  can never close. `_git` now takes a timeout and the standalone caller passes one
  second.
- Stale-session recovery ran unguarded, so a `TimeoutError` from a concurrent finalize
  or an `OSError` from a full disk denied an unrelated healthy session its context and
  its memory episode. Reaping an abandoned session is now best-effort.
- `MINDSYNC_STANDALONE_MEMORY_MODE=off` was consulted only at `SessionStart`, so later
  turns still ran git and the core and wrote `no active session` to stderr every time.
  `off` now short-circuits `Stop` and `SessionEnd` and returns before touching the
  store, while still reporting an ignored `memory_project`.
- An unrecognized `SessionStart` source token raised inside the core instead of being
  dropped, costing that entire session its memory. Tokens are now checked against the
  set the core accepts.
- `explicit` was an accepted standalone mode that resolved to no project and no
  warning — quieter than a typo. Removed, matching the documented `auto | off`.
- The standalone bootstrap budget equalled the context cap, leaving no room for the
  delimiters and `current_session` envelope, so a full bootstrap was truncated
  mid-JSON. The budget reserves the framing, and the cap has one definition.
- A retried `memory_checkpoint` with an existing caller-supplied checkpoint ID skipped
  the session-status update, returning success while `sessions.status` never advanced.
- A background dispatch supervisor re-exec'd `sys.executable` without the parent
  process's import overlay, so `uv run --with` children crashed before finalize
  (`ModuleNotFoundError: pydantic`). The supervisor now inherits a `PYTHONPATH`
  built from the parent's `sys.path`.
- Reconciling a dead running job marked it `failed` without finalizing session
  memory, leaving the episode `active` until the 24-hour reaper. Dead-PID
  reconciliation now finalizes.

### Changed

- Dispatch `--memory-mode` / `memory_mode` now defaults to `auto`. A real-dispatch
  pilot confirmed Git identity is stable across a checkout and its subdirectory, and
  that a non-git working directory fail-closes with a visible warning instead of
  minting a key. `explicit` still requires `--memory-project` / `memory_project`;
  `off` is unchanged. The Codex standalone hook was already `auto`.
- `memory_checkpoint` accepts an optional caller-supplied checkpoint ID. Reusing that
  ID in the same session returns the existing checkpoint, while cross-session reuse is
  rejected; standalone terminal retries use this to avoid duplicate final markers.
- Session memory migrates additively from schema v2 to v3 on first open. Existing
  facts and call signatures remain valid; new embedding, proposal, generated-fact,
  and `superseded_by` metadata supports traceable and reversible Tier 2 operations.
- Session memory migrates from schema v1 to v2 automatically on first open, backfilling
  the fact store from durable facts already stored in checkpoints. The migration is
  idempotent and additive: no existing column or call signature changes. Facts
  outlive the sessions they came from, so pruning old episodes no longer discards
  what was learned in them.
- `memory_bootstrap` payload *membership* can shift even though its signature does
  not: `project_facts` claim up to a quarter of `budget_chars` before session
  entries are packed, so under a tight budget an entry that previously fit may now
  be dropped in favour of a fact. Intentional, but it will show up in any snapshot
  test asserting on a complete bootstrap response.

## [1.4.0] - 2026-08-21

### Added

- Local session memory backed by SQLite (`session_memory.db`): the `session_start`,
  `memory_checkpoint`, `memory_bootstrap`, and `session_end` MCP tools record structured
  session state per project, with deterministic project isolation, budget character
  limits, truncation, and conservative secret redaction on every write.
- Automatic session-memory lifecycle on dispatch, opt in with `--memory-project <key>`
  (CLI) or `memory_project` (`delegate_task` / `run_task`). Bounded project context is
  bootstrapped before the adapter runs, then a compact session is started and finalized
  exactly once across `done`, `failed`, `cancelled`, timeout, and early supervision
  failures. Prompts, transcripts, stdout, stderr, and check output are never persisted —
  only a bounded file list, reduced check summaries, and one durable fact per job. Any
  memory failure degrades to a job warning instead of failing the job.
- Priority-aware bounded bootstrap with strict per-class caps of 200: sessions carrying
  durable facts in any retained checkpoint pack first, then sessions whose latest
  checkpoint has unresolved blockers or pending items, then routine history. Durable
  facts are normalized across string/list/object payloads and merged across checkpoints,
  candidate metadata is streamed, grossly oversized base/history payloads are rejected
  before loading, durable merging stops once the response cannot fit, and up to three
  earlier failed or blocked checkpoints are surfaced as `earlier_checkpoints`.
- Human-facing `mindsync memory stats|list|show|prune` commands (all accept `--json`)
  for the local session-memory database, with dry-run-first retention that never deletes
  active or durable-fact sessions. Prune selects and deletes inside one transaction,
  `--keep-last` is applied before the age filter, and `memory list` orders by the
  greatest activity timestamp.

### Fixed

- Bootstrap no longer raises `JSONDecodeError` when a checkpoint stores an empty string
  for `durable_facts`. The bootstrap SQL already treats `''` as a valid "no facts"
  sentinel, so structured decoding now reports blank values as missing on both the
  latest-checkpoint and `earlier_checkpoints` paths.
- SSH and SCP helpers run with `stdin` detached, so a bridge subprocess can no longer
  consume the MCP server's stdio transport.

## [1.3.0] - 2026-08-17

### Added

- Generic remote execution modes: queue submissions now default to non-recursive `worker` jobs and may explicitly request a depth-0 local `orchestrator`; orchestrator execution requires a local opt-in, while all delegated children remain depth-1 workers with `MINDSYNC_WORKER=1`.
- Background job completion pings through the new `job_wait` MCP tool. The orchestrator now keeps its turn open after delegation and resumes automatically when a job completes, fails, or is cancelled; cancellation also emits a typed `job.cancelled` event.
- One-time `mindsync setup`, `mindsync doctor`, and `mindsync config` onboarding commands. Setup detects supported local CLIs, idempotently registers the MCP server, identifies the human-facing caller, and supports non-mutating dry runs.
- Antigravity (`agy`) and Gemini CLI are modeled as two backends in one `gemini-antigravity` worker family; family-wide caller exclusion prevents self-delegation and doctor groups their inventory.
- Persistent `auto` / `suggest` / `off` orchestration policy with announcement control, automatic caller exclusion, and a configurable parallel-job limit.
- Capability-based automatic worker routing (`agent="auto"`), live worker inventory, explainable route previews and stored routing metadata through the new `list_agents`, `route_task`, and `get_orchestration_policy` MCP tools.
- MCP handshake instructions establish the human-facing CLI as orchestrator while keeping authorization, verification, integration, and the final response with that agent.
- Support routing dispatch jobs by role instead of agent name (`--role` CLI flag, `role` parameter on `run_task` / `delegate_task` MCP tool, and new `roles` CLI command and `list_roles` MCP tool). Roles map to an agent, optional model, and optional effort triple in `agents.json`.
- Mechanical review gate for dispatch jobs (`--check "<command>"` on `run`, `review <job-id>` command, and `job_review` MCP tool). Runs cheap objective check commands and computes git diff metrics against the job's base commit prior to worktree cleanup, recording a `VERDICT: PASS` / `FAIL` summary so callers can skip broken outputs. Every job now records the base commit it started from, not only isolated ones.
- Opt-in git worktree isolation for dispatch jobs (`--worktree` / `--cwd` flags on the CLI, `worktree=True` / `cwd=...` kwargs on the `delegate_task` MCP tool). Each job runs in a dedicated sibling directory (`.mindsync-wt/<job-id>`) on its own branch. Unchanged worktrees are auto-cleaned; worktrees with commits or untracked files are kept for review.
- Dispatch layer supports discovering models, applying default models, and reasoning-effort options (`--effort`) natively, passed through to the underlying CLI adapters. Added `models` command to CLI and `list_models` MCP tool.
- Worktree jobs append a note to the task text telling the agent to stay inside its working directory. Isolation is advisory, and an absolute path in the task text is enough to send an agent back to the original checkout — a failure that is otherwise silent, since the job still succeeds.

### Fixed

- Replaced age-based lock stealing with kernel-managed cross-platform file locks, eliminating a race that could remove a newly acquired live lock.
- Made dispatch metadata updates locked and atomic, with conditional lifecycle transitions so cancellation cannot be overwritten by completion.
- Background dispatch now emits one correctly attributed `job.started` event and preserves the requesting agent for terminal events.
- Remote truth pulls use unique, always-cleaned staging directories so concurrent pulls cannot delete each other's data.
- Legacy SSH write errors now pass through the same path/key/host sanitizer as native batch writes.
- Dispatch job directories and artifacts now request private `0700`/`0600` permissions where supported.
- Event publishing uses a persistent sequence checkpoint instead of rescanning the entire JSONL log for every event.
- Automatic delegation now reserves capacity under one cross-process lock, so concurrent requests cannot exceed `maxParallel`; an active-job index avoids rescanning completed job history on every admission.
- Sensitive state, policy, dispatch, log, audit, queue, and Cursor onboarding files are created with private permissions before content is written; Cursor backups are private as well.
- Lock owner metadata is written only after acquisition, legacy `stale_after` use emits a deprecation warning, and client-name aliases prefer the most specific match.

## [1.2.0] - 2026-07-28

### Added

- `MINDSYNC_SSH_BIN` pins the ssh client the bridge shells out to, for setups
  where PATH resolution picks the wrong OpenSSH build.

### Changed

- `validate_entity` now accepts a single `namespace:` prefix (see below), so
  entity keys that were previously rejected are stored as given.

### Fixed

- **Failed dispatch jobs now explain themselves.** The result file only ever held
  stdout, so an agent that aborted early (bad auth, a trust prompt, a rejected
  model) produced an empty result and the CLI reported `(no output)` — the real
  error sat unread in `stderr.log`. Failed and timed-out jobs now append a
  `[dispatch] Agent failed (…)` block with the stderr tail (capped at 4 KB),
  keeping any partial stdout above it. Successful runs are unchanged.
- `job_result` no longer answers `(no result yet — job is failed)` for a finished
  job; empty results are now described per status (failed / cancelled / running).
- **`gemini` preset works headless.** Gemini CLI refuses to run in an untrusted
  directory, so every dispatched job died with exit 55 before starting. The preset
  now passes `--skip-trust`.
- **Remote no longer looks permanently offline on Windows.** The bridge shelled
  out to whatever `ssh` PATH resolved first; when Git for Windows' MSYS ssh won,
  it could not reach the Windows ssh-agent, so every agent-held key failed and
  the probe reported offline forever. ssh/scp now prefer the OS OpenSSH client on
  Windows, overridable with `MINDSYNC_SSH_BIN`.
- The remote probe's `reason` now carries ssh's own (sanitized) stderr, so an
  offline result says *why* — wrong key, unknown host alias, refused connection —
  instead of a bare `ssh_auth_or_timeout`.
- **Legacy write fallback degrades one step further.** The fallback for a remote
  with no `batch` subcommand still sent `--fact_id`, which older writers reject
  outright — argparse fails the whole call, so every queued fact stayed stuck.
  When the remote reports the flag as unrecognized, it is dropped and the write
  retried; the result is probed once per process, not once per fact.
- **Namespaced entity keys are valid again.** `validate_entity` rejected the
  `namespace:name` form (`person:alice`, `project:web-api`), so facts queued
  under the documented convention could never sync and were quarantined to the
  dead letter on flush. A single `namespace:` prefix is now accepted; the prefix
  may not contain a dot, which keeps the Windows alternate-data-stream shape
  (`file.txt:stream`) and reserved device names rejected as before.
- Docs, comments and test fixtures use neutral placeholders instead of the
  maintainer's own entity keys, remote script name and agent roster.

## [1.1.1] - 2026-07-26

### Fixed

- Republished so the PyPI project links point at `mindsync-ai`; the 1.1.0
  artifacts were built before the rename landed and still linked to the old
  `mindsync-mcp` repository.
- `update_focus` no longer swallows a failed `focus.changed` publish. The focus
  write still succeeds, but the dropped event is now reported in `warnings` and
  written to the audit log instead of returning a clean success.
- SSH sanitization tests use a neutral fixture path instead of a real home
  directory.

## [1.1.0] - 2026-07-21

### Added

- **MindSync AI** unified package: core memory/focus + in-process **Event Bus** +
  **Agent Dispatch** (Python port of agent-dispatch) in one MCP server.
- `mindsync.bus` — typed events (`job.started` / `completed` / `failed`,
  `focus.changed`, `memory.updated`, …), JSONL store, monotonic `seq`,
  publish / poll / subscribe tools.
- `mindsync.dispatch` — adapters, cross-platform process helpers, job store,
  runner, CLI (`mindsync-dispatch`), presets for codex/claude/gemini/cursor/aider/grok.
- MCP tools: `publish_event`, `poll_events`, `subscribe_events`,
  `delegate_task`, `job_status`, `job_result`, `job_cancel` (13 tools total).
- Auto event hooks from focus updates, durable facts, and job lifecycle.

### Changed

- **PyPI package renamed** to **`mindsync-ai`** (was `mindsync-mcp` through 1.0.1).
  Import path (`mindsync`) and CLI entry points (`mindsync`, `mindsync-dispatch`)
  are unchanged.
- GitHub repository renamed to **`adityarya24/mindsync-ai`** (old
  `mindsync-mcp` URL redirects).
- README rewritten for the unified product branding.

## [1.0.1] - 2026-07-18

### Added

- Public release packaging: `pyproject.toml` metadata (SPDX license expression,
  Python 3.10-3.13 classifiers), CI workflow, and release workflow.
- End-to-end MCP stdio handshake test that drives `initialize` ->
  `notifications/initialized` -> `tools/list` against the real server process
  and asserts every MindSync tool is exposed.

### Fixed

- `serverInfo.version` reported by the MCP server now reflects MindSync's own
  package version instead of the underlying `mcp` SDK version.
- Release workflow no longer bundles `checksums.txt` inside the artifact
  published to PyPI; checksums are generated and distributed separately.
- Release workflow now gates the release job on the tagged commit's CI run
  having succeeded.
- Lockfile lifecycle, offline queue flush, and conflict-token handling
  hardened against races.
- Data-integrity and false-offline sync issues resolved.
- Missing imports, concurrency safety, JSONL migration, and batch bounds
  issues resolved.
- Security: trust boundaries enforced, Windows-safe path validators added,
  atomic staging publish for local state.
- Windows SSH CRLF line endings no longer break remote bash scripts.

## [1.0.0] - Initial release

- Local-first multi-agent memory sync and focus conflict detection over MCP.
