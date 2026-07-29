# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Mechanical review gate for dispatch jobs (`--check "<command>"` on `run`, `review <job-id>` command, and `job_review` MCP tool). Runs cheap objective check commands and computes git diff metrics against base_commit prior to worktree cleanup, recording a `VERDICT: PASS` / `FAIL` summary so callers can skip broken outputs.
- Opt-in git worktree isolation for dispatch jobs (`--worktree` / `--cwd` flags on the CLI, `worktree=True` / `cwd=...` kwargs on the `delegate_task` MCP tool). Each job runs in a dedicated sibling directory (`.mindsync-wt/<job-id>`) on its own branch. Unchanged worktrees are auto-cleaned; worktrees with commits or untracked files are kept for review.
- Dispatch layer supports discovering models, applying default models, and reasoning-effort options (`--effort`) natively, passed through to the underlying CLI adapters. Added `models` command to CLI and `list_models` MCP tool.
- Worktree jobs append a note to the task text telling the agent to stay inside its working directory. Isolation is advisory, and an absolute path in the task text is enough to send an agent back to the original checkout — a failure that is otherwise silent, since the job still succeeds.

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
