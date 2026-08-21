# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
