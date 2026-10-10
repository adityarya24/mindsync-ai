# Open-core removal map (before removal)

- `dispatch/usage/`: six reader implementations feed `registry` and `evaluate`; `config` and `preemptive` are imported by `dispatch/routing.py` and `dispatch/runner.py`. `adapters.py` validates reader names; preset JSON assigns readers. `onboarding.py` loads usage configuration for doctor. Matching usage reader, ranking and preemptive tests cover these paths.
- `codex_standalone_usage.py`: imports usage configuration, threshold evaluation and Codex reader. `codex_hook.py` prefetches during session setup and appends reserve warnings at stop. Standalone usage tests, hook reserve tests and test isolation helper reference it.
- Worker handoff: `dispatch/cli.py` parses `--on-limit`; MCP `server.delegate_task` forwards `on_limit` to `runner.run_task`. Runner performs preflight/periodic usage evaluation, checkpoints via `memory_lifecycle.py`, builds successor prompts, routes successors and transfers worktree leases. `store.py` stores attempts/handoffs; CLI/server render handoff blocks. `limits.py` reactive classification/cooldown remains free.
- `dispatch/publish.py`: runner calls automatic PR publication after completion; orchestration models and manage config accept `onComplete=pr`. The completion sink imports `public_task` solely for privacy filtering; that filter must remain available independently of PR creation. Matching publication tests move out.
- `remote_queue.py`: manage `submit`, `worker` and remote `status` import queue submission, worker loops and status lookup. Queue tests and remote orchestrator tests reference these paths.
- README and GitHub Pages advertise handoff; README also describes usage readers and automatic PRs. Historical live-handoff evidence is paid-feature documentation.

Existing SSH fact/truth synchronization in `bridge.py` is a separate scope question: the brief specifically names remote_queue and its CLI commands. No changes to the private platform repository are authorized.
