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

Provider quota handoff is opt-in and requires an isolated worktree:

```bash
mindsync-dispatch run auto "implement and test the fix" --write --worktree --on-limit handoff
mindsync-dispatch limits                 # inspect provider/account cooldowns
mindsync-dispatch limits clear           # clear cooldowns after operator verification
```

Pre-emptive usage readers are pluggable per provider. The Codex adapter can
read primary and weekly OAuth usage windows from the local `~/.codex/auth.json`
source when a reader is configured. Pre-emptive polling and threshold handoff are
opt-in: they require both `usage.enabled: true` in `agents.json` and
`--on-limit handoff` on an isolated worktree job. Global usage settings default
to disabled:

```json
{
  "usage": {
    "enabled": false,
    "defaultThresholdPercent": 90,
    "pollingIntervalSeconds": 60
  }
}
```

When enabled, dispatch polls at `pollingIntervalSeconds` during a running
attempt, skips cooling or over-threshold provider accounts before spawn, and may
transfer only when a privacy-safe MindSync checkpoint already exists for that
attempt (plus the worktree diff and original task). There is no generic CLI
control channel: dispatch does not ask arbitrary agents to write `HANDOFF.md`.
Without a checkpoint, threshold hits are recorded as `preemptiveBlocked` and
the attempt keeps running so reactive quota handoff remains the floor. Job
status shows usage evaluation, skips, blocks, and handoffs; only percentages,
window labels, reset times, scope, and reasons are persisted — never raw usage,
auth, or source payloads.

Per-adapter overrides use `usageReader` and optional `usageThresholdPercent`.
The bundled Codex preset declares `usageReader: "codex-oauth"`.

Only configured provider-specific exhaustion messages rotate. Timeouts, auth
errors, generic rate limits, failing tests, and ordinary agent failures stop the
job. A successor receives the same worktree, the original task, and the latest
structured MindSync checkpoint; because routing may select another provider,
enable handoff only when that cross-provider context transfer is acceptable.

Completed jobs keep their branch by default. To push a successful isolated
job and open a pull request for review, enable it for that repository:

```bash
mindsync config onComplete pr --project .
```

MindSync never merges the pull request. It also declines to publish when a
requested check failed or did not report, private prompt framing cannot be
separated safely, commit hooks refuse the work, or changed paths look like
secrets. Use `MINDSYNC_ON_COMPLETE=pr` for a one-run override.

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
- Local state uses crash-safe locks and atomic writes. On Windows, tune queue
  lock deadlines and OS-lock contention backoff via
  `MINDSYNC_QUEUE_LOCK_TIMEOUT`, `MINDSYNC_LOCK_CONTENTION_BACKOFF_BASE`, and
  `MINDSYNC_LOCK_CONTENTION_BACKOFF_MAX` (see [`.env.example`](.env.example)).

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
