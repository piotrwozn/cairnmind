"""An MCP tool call with an argument the tool does not declare is refused and names it (#169)."""
from __future__ import annotations

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from cairnmind.config import Settings
from cairnmind.mcp_server import create_server


class FakeService:
    def __init__(self):
        self.calls = []

    async def post(self, endpoint, payload):
        self.calls.append((endpoint, payload))
        return {"ok": True}

    async def get(self, endpoint, params=None):
        self.calls.append((endpoint, params))
        return {"checks": []}


def server_of(tmp_path):
    bridge = FakeService()
    return create_server(Settings(data_dir=tmp_path), bridge), bridge


@pytest.mark.asyncio
async def test_remember_with_state_beside_the_record_is_refused_and_nothing_is_saved(tmp_path):
    server, bridge = server_of(tmp_path)
    record = {"kind": "task", "content": "Do it"}
    with pytest.raises(ToolError) as refused:
        await server.call_tool("remember", {"project": "demo", "record": record, "sources": [], "state": "in_progress"})
    assert "'state'" in str(refused.value) and "record" in str(refused.value) and bridge.calls == []
    await server.call_tool("remember", {"project": "demo", "record": record, "sources": []})
    assert [endpoint for endpoint, _ in bridge.calls] == ["/remember"]


@pytest.mark.asyncio
async def test_every_tool_refuses_an_argument_it_does_not_declare(tmp_path):
    server, bridge = server_of(tmp_path)
    tools = await server.list_tools()
    assert len(tools) >= 8
    for tool in tools:
        with pytest.raises(ToolError, match="no argument named 'surprise'"):
            await server.call_tool(tool.name, {"surprise": 1})
    assert bridge.calls == []


@pytest.mark.asyncio
async def test_a_parameter_a_tool_declares_later_is_accepted_with_no_other_edit(tmp_path):
    # The refusal reads each tool's own schema, never a fixed list: a new parameter (such as full_packet) just works.
    server, _ = server_of(tmp_path)

    def probe(project: str, full_packet: bool = False) -> dict:
        return {"project": project, "full_packet": full_packet}

    server.add_tool(probe)
    result = await server.call_tool("probe", {"project": "demo", "full_packet": True})
    assert '"full_packet": true' in str(result)
    with pytest.raises(ToolError, match="no argument named 'surprise'.*full_packet, project"):
        await server.call_tool("probe", {"project": "demo", "surprise": 1})


@pytest.mark.asyncio
async def test_the_refusal_reaches_a_client_over_the_protocol_as_an_error_result(tmp_path):
    from mcp.shared.memory import create_connected_server_and_client_session
    server, bridge = server_of(tmp_path)
    async with create_connected_server_and_client_session(server._mcp_server) as client:
        result = await client.call_tool("resume_context", {"project": "demo"})
        assert result.isError and "no argument named 'project'" in result.content[0].text
        assert not (await client.call_tool("resume_context", {})).isError
    assert [endpoint for endpoint, _ in bridge.calls] == ["/sessions/context"]
