"""A hung MCP tool must not stall a run forever: _execute_tool bounds it."""

import asyncio
import json

from ops_copilot.graph.workers import _execute_tool


class HangingTool:
    name = "hanging_tool"

    async def ainvoke(self, args):
        await asyncio.sleep(3600)
        return "never"


class FailingTool:
    name = "failing_tool"

    async def ainvoke(self, args):
        raise RuntimeError("boom")


class OkTool:
    name = "ok_tool"

    async def ainvoke(self, args):
        return "done"


class TestExecuteToolTimeout:
    async def test_hanging_tool_returns_timeout_error_fast(self):
        result = await _execute_tool(HangingTool(), {}, timeout_s=0.2)
        assert "timed out" in json.loads(result)["error"]

    async def test_failing_tool_still_returns_error_json(self):
        result = await _execute_tool(FailingTool(), {})
        assert json.loads(result) == {"error": "boom"}

    async def test_ok_tool_still_returns_result(self):
        assert await _execute_tool(OkTool(), {}) == "done"
