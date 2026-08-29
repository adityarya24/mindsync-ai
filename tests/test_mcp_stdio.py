"""End-to-end MCP stdio handshake test.

Spawns the real `mindsync` server as a subprocess and drives the actual
protocol handshake using the `mcp` client SDK: `initialize` ->
`notifications/initialized` -> `tools/list`. This exercises the real wire
protocol, not a mocked/simulated one, and asserts every MindSync tool is
exposed with the correct server identity.
"""

from __future__ import annotations

import asyncio
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import get_default_environment, stdio_client

from mindsync import __version__

EXPECTED_TOOLS = {
    "get_sync_context",
    "update_focus",
    "queue_durable_fact",
    "sync_offline_facts",
    "pull_truth",
    "health",
    "session",
    "memory_bootstrap",
    "memory_recall",
    "memory_consolidation",
    "events",
    "delegate_task",
    "route_task",
    "get_orchestration_policy",
    "job",
    "list",
}

WORKER_EXCLUDED_TOOLS = frozenset(
    {
        "delegate_task",
        "route_task",
        "get_orchestration_policy",
        "list",
    }
)
EXPECTED_WORKER_TOOLS = EXPECTED_TOOLS - WORKER_EXCLUDED_TOOLS
REMOVED_ALIASES = {
    "job_status",
    "job_wait",
    "job_result",
    "job_review",
    "job_cancel",
    "publish_event",
    "poll_events",
    "subscribe_events",
    "session_start",
    "memory_checkpoint",
    "session_end",
    "memory_consolidate_preview",
    "memory_consolidation_apply",
    "memory_consolidation_undo",
    "memory_consolidation_list",
    "list_agents",
    "list_models",
    "list_roles",
}


async def _run_handshake(home: str, *, worker: bool = False):
    env = get_default_environment()
    env["MINDSYNC_HOME"] = home
    if worker:
        env["MINDSYNC_WORKER"] = "1"

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mindsync.server"],
        env=env,
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init_result = await session.initialize()
            tools_result = await session.list_tools()
            return init_result, tools_result


def test_stdio_handshake_reports_server_identity_and_version(tmp_path):
    init_result, _ = asyncio.run(_run_handshake(str(tmp_path / "home")))

    assert init_result.serverInfo.name == "MindSync"
    assert init_result.serverInfo.version == __version__
    assert "human" in (init_result.instructions or "").lower()
    assert "orchestration mode" in (init_result.instructions or "").lower()


def test_stdio_handshake_lists_all_mindsync_tools(tmp_path):
    _, tools_result = asyncio.run(_run_handshake(str(tmp_path / "home")))

    tool_names = {tool.name for tool in tools_result.tools}
    assert EXPECTED_TOOLS.issubset(tool_names), (
        f"missing tools: {EXPECTED_TOOLS - tool_names}"
    )
    assert tool_names == EXPECTED_TOOLS, (
        f"unexpected extra tools exposed: {tool_names - EXPECTED_TOOLS}"
    )
    assert REMOVED_ALIASES.isdisjoint(tool_names)
    assert len(tool_names) == 16


def test_stdio_worker_handshake_excludes_orchestrator_tools(tmp_path):
    _, tools_result = asyncio.run(
        _run_handshake(str(tmp_path / "worker-home"), worker=True)
    )

    tool_names = {tool.name for tool in tools_result.tools}
    assert tool_names == EXPECTED_WORKER_TOOLS
    assert WORKER_EXCLUDED_TOOLS.isdisjoint(tool_names)
    assert REMOVED_ALIASES.isdisjoint(tool_names)
    assert len(tool_names) == 12


def test_stdio_job_bundle_dispatch_and_stale_name_absent(tmp_path):
    async def _run():
        env = get_default_environment()
        env["MINDSYNC_HOME"] = str(tmp_path / "home")
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "mindsync.server"],
            env=env,
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                names = {tool.name for tool in (await session.list_tools()).tools}
                assert "job_status" not in names
                result = await session.call_tool(
                    "job",
                    {
                        "action": "status",
                        "job_id": "no-such-job",
                        "timeout_seconds": 1.0,
                    },
                )
                text = "".join(
                    block.text for block in result.content if hasattr(block, "text")
                )
                assert "timeout_seconds" in text
                ok = await session.call_tool(
                    "job",
                    {"action": "status", "job_id": "no-such-job"},
                )
                ok_text = "".join(
                    block.text for block in ok.content if hasattr(block, "text")
                )
                assert "No such job" in ok_text

    asyncio.run(_run())
