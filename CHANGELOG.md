# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
