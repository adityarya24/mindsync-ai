# Wave 2 design: issue #53 tool bundling (not implemented)

Status: **design only**. Do not merge bundling in the same PR as #52 unless
Aditya explicitly wants a versioned public MCP break.

## Current surface (Wave 1, PR #55)

- Orchestrator MCP: **29** tools
- Worker MCP (`MINDSYNC_WORKER=1`): **23** tools (six orchestration tools omitted
  at registration: `delegate_task`, `route_task`, `get_orchestration_policy`,
  `list_agents`, `list_models`, `list_roles`)

## Proposed bundles (subject-sharing only)

| Bundle | Actions | Today |
|---|---|---|
| `job` | status, wait, result, review, cancel | `job_status`, `job_wait`, `job_result`, `job_review`, `job_cancel` |
| `memory_consolidation` | preview, apply, undo, list | four `memory_consolidation_*` tools |
| `events` | publish, poll, subscribe | `publish_event`, `poll_events`, `subscribe_events` |
| `list` | agents, models, roles | already worker-gated; orchestrator-only |
| `session` | start, checkpoint, end | `session_start`, `memory_checkpoint`, `session_end` |

Do **not** bundle `health`, `update_focus`, `get_sync_context`,
`memory_bootstrap`, or `memory_recall`.

## Compatibility / stale-client policy

Immediate deletion of old names is an unannounced break for any MCP client that
cached tool names. Required policy if bundling ships:

1. **v1.8 (this would be a minor with aliases):** register both bundle tools and
   the old names. Old names are thin wrappers. Worker gating still applies to
   `list_*` / `delegate_task` family. Docs + changelog list every alias.
2. **Later minor:** drop aliases after a documented window.

If the only acceptable design is “remove old names in the same release with no
aliases”, that is a **separately versioned PR**, not a silent add-on to #52.

Discriminated validation: each bundle `action` has its own required fields;
unknown extras for that action are rejected, not ignored.

Recursive `delegate_task` refusal stays a runtime check even if the tool is
ungated for stale workers.

## This wave

No bundling code. #52 + evidence docs only. Implement #53 after Aditya picks
aliases-in-1.8 vs separate breaking PR.
