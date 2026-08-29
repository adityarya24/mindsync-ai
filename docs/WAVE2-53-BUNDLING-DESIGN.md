# Wave 2 design: issue #53 tool bundling (implemented, #57)

Status: **implemented** in PR #57. The open question this doc used to hold —
aliases-in-a-minor vs. a separate breaking PR — was decided in favour of the
clean break. This file now records the decision and its rationale.

## Decision

**Remove the old tool names in the same release, with no aliases.** Shipped as a
versioned breaking change (its own `### Changed` / **Breaking (#53)** entry),
not folded into #52.

Why the break rather than an alias window: the compatibility cost that aliases
buy is a real cost only when unknown external clients have cached the old tool
names. MindSync's MCP surface has effectively one operator, moves fast, and the
whole point of #53 was to *shrink* the surface — permanent alias wrappers would
have re-grown it and left dead names to carry forever. A single announced break
with a loud changelog entry is cheaper than a deprecation window nobody needed.

## Bundles (subject-sharing only)

| Bundle | Actions | Replaced |
|---|---|---|
| `job` | status, wait, result, review, cancel | `job_status`, `job_wait`, `job_result`, `job_review`, `job_cancel` |
| `memory_consolidation` | preview, apply, undo, list | four `memory_consolidation_*` tools |
| `events` | publish, poll, subscribe | `publish_event`, `poll_events`, `subscribe_events` |
| `list` | agents, models, roles | `list_agents`, `list_models`, `list_roles` (orchestrator-only) |
| `session` | start, checkpoint, end | `session_start`, `memory_checkpoint`, `session_end` |

Deliberately **not** bundled: `health`, `update_focus`, `get_sync_context`,
`memory_bootstrap`, `memory_recall` — these do not share a subject with each
other or with the groups above, so merging them would trade one slot for a
blurred tool.

## Surface

- Orchestrator MCP: **29 → 16** tools.
- Worker MCP (`MINDSYNC_WORKER=1`): **12** tools — the orchestration tools
  (`delegate_task`, `route_task`, `get_orchestration_policy`, and the `list`
  bundle) are omitted at registration, not merely refused at call time.

## Guarantees preserved

- **Discriminated validation:** each bundle `action` has its own required
  fields; fields belonging to a different action are rejected, not ignored.
- **Recursive `delegate_task` refusal** stays a runtime check even where a stale
  worker still holds the tool.
- **Python helpers keep their old function names** for in-process tests, so only
  the MCP-advertised surface changed, not the importable API.

## Migration

Stale MCP clients must reconnect and call the bundle tools: `job`, `events`,
`session`, `memory_consolidation`, and `list(kind=agents|models|roles)`. The
release changelog lists every removed name.
