# Issue #53 MCP tool bundling (Option 2)

Status: **implemented** as a separately versioned breaking change. No aliases.

## Advertised surface

| Process | Count | Names |
|---|---|---|
| Orchestrator | **16** | `get_sync_context`, `update_focus`, `queue_durable_fact`, `sync_offline_facts`, `pull_truth`, `health`, `session`, `memory_bootstrap`, `memory_recall`, `memory_consolidation`, `events`, `delegate_task`, `route_task`, `get_orchestration_policy`, `job`, `list` |
| Worker (`MINDSYNC_WORKER=1`) | **12** | orchestrator set minus `delegate_task`, `route_task`, `get_orchestration_policy`, `list` |

Not bundled: `health`, `update_focus`, `get_sync_context`, `memory_bootstrap`, `memory_recall`.

## Old → new

| Removed MCP name | Bundle |
|---|---|
| `job_status`, `job_wait`, `job_result`, `job_review`, `job_cancel` | `job(action=…)` |
| `publish_event`, `poll_events`, `subscribe_events` | `events(action=…)` |
| `session_start`, `memory_checkpoint`, `session_end` | `session(action=start\|checkpoint\|end)` |
| `memory_consolidate_preview`, `memory_consolidation_apply`, `memory_consolidation_undo`, `memory_consolidation_list` | `memory_consolidation(action=preview\|apply\|undo\|list)` |
| `list_agents`, `list_models`, `list_roles` | `list(kind=agents\|models\|roles)` (orchestrator only) |

## Stale-client policy

Connected clients that cached old tool names cannot call them: they are not
registered. Reconnect (or restart the host) and use the bundle names. In-process
Python helpers still exist for tests; they are not an MCP compatibility layer.

## Validation

Each bundle keeps its own action table. Fields that are valid for a different
action of the same tool are rejected, not ignored. Missing required fields for
that action fail closed.

Recursive `delegate_task` refusal remains a runtime check for worker processes
even though the tool is unregistered there.
