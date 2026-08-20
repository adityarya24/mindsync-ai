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
    # Core memory / focus
    "get_sync_context",
    "update_focus",
    "queue_durable_fact",
    "sync_offline_facts",
    "pull_truth",
    "health",
    "session_start",
    "memory_checkpoint",
    "memory_bootstrap",
    "session_end",
    # Event bus
    "publish_event",
    "poll_events",
    "subscribe_events",
    # Agent dispatch
    "delegate_task",
    "route_task",
    "list_agents",
    "get_orchestration_policy",
    "job_status",
    "job_wait",
    "job_result",
    "job_review",
    "job_cancel",
    "list_models",
    "list_roles",
}


async def _run_handshake(home: str):
    env = get_default_environment()
    env["MINDSYNC_HOME"] = home

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mindsync.server"],
        env=env,
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            # 1. `initialize` request, followed internally by the
            #    `notifications/initialized` notification.
            init_result = await session.initialize()
            # 2. `tools/list` request.
            tools_result = await session.list_tools()
            return init_result, tools_result


def test_stdio_handshake_reports_server_identity_and_version(tmp_path):
    init_result, _ = asyncio.run(_run_handshake(str(tmp_path / "home")))

    assert init_result.serverInfo.name == "MindSync"
    # Regression guard: the server must report MindSync's own package
    # version, not the underlying `mcp` SDK version it happens to be
    # built against.
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
