# MindSync AI

[![CI](https://github.com/adityarya24/mindsync-ai/actions/workflows/ci.yml/badge.svg)](https://github.com/adityarya24/mindsync-ai/actions/workflows/ci.yml)
[![PyPI version](https://img.shields.io/pypi/v/mindsync-ai.svg)](https://pypi.org/project/mindsync-ai/)
[![Python versions](https://img.shields.io/pypi/pyversions/mindsync-ai.svg)](https://pypi.org/project/mindsync-ai/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**[adityarya24.github.io/mindsync-ai](https://adityarya24.github.io/mindsync-ai/)**

Local-first MCP orchestration for coding agents. The CLI already talking to you
stays in charge: MindSync routes work by capability, blocks file collisions,
and keeps session memory across runs. No MindSync account. Remote sync is optional.

```text
You → human-facing CLI (orchestrator) → MindSync → Codex / Claude / Gemini / AGY / Grok / Cursor / OpenCode / Aider
```

Workers get bounded tasks. They cannot recursively delegate through MindSync.

## Quick start

```bash
pip install mindsync-ai
mindsync setup --mode auto
mindsync doctor
mindsync agents
```

Requires Python 3.10+. Restart the configured CLI sessions after setup.

`setup` registers known MCP hosts (Codex, Claude, Gemini, Grok, Cursor, OpenCode)
and recognised PATH agent CLIs. MCP is installed only when MindSync has a real
recipe — it will not guess `mcp add` flags. Unknown binaries are suggested, not
registered, and never executed. Use `mindsync register` for an unusual name.

```bash
mindsync setup --dry-run          # preview
mindsync setup --cli grok         # one known host, no PATH scan
mindsync setup --no-discover      # hosts only
mindsync setup --no-hooks         # skip Codex standalone hooks
```

Install from source: `python -m pip install -e ".[dev]"`

## Supported clients

A CLI may be an **MCP host**, a **worker**, or both.

| CLI | MCP host | Worker | Notes |
| --- | --- | --- | --- |
| OpenAI Codex | Native | Yes | Also gets standalone memory hooks |
| Anthropic Claude | Native | Yes | Architecture, review, large context |
| Google Gemini CLI | Native | Yes | Gemini/Antigravity family |
| Antigravity (`agy`) | Via Gemini | Yes | Preferred worker in that family |
| Grok CLI | Native | Yes | Research, review, security |
| Cursor Agent | JSON | Yes | `~/.cursor/mcp.json` |
| OpenCode | JSON | Yes | `~/.config/opencode/opencode.jsonc` |
| Aider | — | Yes | Focused editing |

Gemini CLI and `agy` are one family. When either is the human-facing orchestrator,
both are excluded from automatic worker selection.

## Orchestration

Policy lives in `~/.mindsync/orchestration.json`. Modes: `auto`, `suggest`, `off`.

```bash
mindsync config orchestration.mode auto
mindsync-dispatch run auto "implement and test the fix" --capability coding
mindsync-dispatch status
```

Custom worker:

```bash
mindsync register --name my-worker --bin my-cli --capability coding
```

Heavy tags (`security`, `large-context`, `multimodal`) need `--confirm`.
Roster and jobs: `~/.mindsync/dispatch/` (`AGENT_DISPATCH_HOME` overrides).

## Memory

Dispatch memory defaults to `auto` in git checkouts (opaque git identity, never
a path or repo name). Failures warn; they do not fail the job.

```bash
mindsync memory stats
mindsync memory list --project my-repo
mindsync memory recall --project my-repo --query "database decision"
```

Nothing is pruned without `--yes`. See MCP tools on the server (`get_sync_context`,
`delegate_task`, `job_wait`, …) once a host is configured.

## Optional remote sync

```bash
export MINDSYNC_SSH_HOST=my-server
export MINDSYNC_REMOTE_ROOT=/opt/mindsync
```

Worker loop and VPS scripts: [`examples/remote/`](examples/remote/).
Environment variables: [`.env.example`](.env.example).

## Safety

- The human-facing CLI owns authorization and the final answer.
- Setup never executes a binary it cannot name.
- Existing MCP registrations are preserved unless `--force`.
- Local state uses crash-safe locks and atomic writes.

Runs with the current user's privileges. See [`SECURITY.md`](SECURITY.md).

## Development

```bash
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
python -m pip install -e ".[dev]"
python -m ruff check .
python -m pytest -q
```

Use the venv. `sqlite-vec` is a package dependency; a bare system Python will
fail the Tier 2 tests.

## License

[MIT](LICENSE)
