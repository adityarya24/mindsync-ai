# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
