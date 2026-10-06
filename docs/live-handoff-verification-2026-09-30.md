# Live usage handoff verification — 2026-09-30

## Result

The candidate code on `fix/live-preemptive-handoff` completed one real
Claude-to-Codex-family dispatch transfer. This was not a quota fixture.

- Job: `20260930152250-fcfe6e`
- Live Claude OAuth preflight: weekly 92%, default threshold 90%
- Attempt 1: `claude`, `usage_threshold`, exit 0
- Handoff: `claude` to `codex-orchestrator`, reason `usage_threshold`
- Attempt 2: `codex-orchestrator` (the Codex CLI), `done`, exit 0
- Both attempts used the job's preserved Git worktree.
- `organizer.py` was edited at 20:53:22 IST, before the 20:53:29 handoff;
  `tests/test_organizer.py` was created at 20:57:23 IST during the successor
  attempt. This is file-level evidence that Codex continued Claude's work.
- Dispatched `unittest discover -s tests -v` check: passed, exit 0
- Job metadata (local to the test machine):
  `$MINDSYNC_HOME/dispatch/jobs/20260930152250-fcfe6e/meta.json`

The first usage observation came from the live `claude-oauth-usage` reader.
During Claude's run the endpoint intermittently failed; the threshold poll used
that same real observation from the bounded recent cache. The cache expires
after 120 seconds or any reported usage-window reset. An actual worktree edit
was checkpointed before the process transfer.

## Scope and remaining gate

The live dispatch used this branch through a process-local `PYTHONPATH` and a
temporary 10-second polling interval. The user settings file was restored
byte-for-byte afterward; its effective threshold is again 90% and its normal
polling interval is 60 seconds. The branch is not merged or released.

The running Agent Notch Pro instance used an older alias mapper when this
handoff occurred. Its source now maps `codex-orchestrator` to the Codex ring,
and the Pro app has been restarted, but a new live event has not yet verified
the on-screen banner. Treat the visual gate as pending.
