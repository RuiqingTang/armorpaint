import asyncio
import os
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def with_client(socket_path, check):
    params = StdioServerParameters(command=sys.executable, args=["-m", "armorpaint_mcp.server", "--socket", str(socket_path)])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as client:
            init = await client.initialize()
            assert init.serverInfo.name == "ArmorPaint"
            await check(client)


def test_stdio_mcp_discovery_without_running_application(tmp_path):
    async def check(client):
        tools = await client.list_tools()
        names = {t.name for t in tools.tools}
        assert {"execute_code", "call_function", "get_application_state", "get_screenshot", "paint_stroke", "bake_lightmap", "get_node_graph"} <= names
        for tool in tools.tools:
            assert tool.inputSchema["type"] == "object"
        resources = await client.list_resources()
        assert len(resources.resources) == 4
        result = await client.call_tool("get_application_state", {})
        assert result.isError
        assert "Start MCP Server" in result.content[0].text
    asyncio.run(with_client(tmp_path / "missing.sock", check))


@pytest.mark.skipif(not os.environ.get("ARMORPAINT_MCP_TEST_SOCKET"), reason="requires an explicitly opted-in disposable ArmorPaint instance")
def test_live_workflow_through_mcp(tmp_path):
    """This test REPLACES the test instance's project. Never point it at user work."""
    async def check(client):
        async def call(tool_name, **args):
            result = await client.call_tool(tool_name, args)
            assert not result.isError, result.content
            return result.structuredContent

        await call("project_operation", action="new")
        state = await call("get_application_state")
        assert state["registered_functions"] > 1400
        assert state["registered_structs"] > 50
        api = await call("get_api_reference", query="layers_create_fill_layer")
        assert api["lines"]
        scalar = await call("call_function", name="config_get_texture_res_x", arguments=[])
        assert scalar["return_value"] == state["texture_width"]
        material = await call("create_material", name="MCP blue metal", base_color=[0.1, 0.3, 0.7, 1], roughness=0.3, metallic=0.9)
        assert material["state"]["materials"][-1]["name"] == "MCP blue metal"
        graph = await call("get_node_graph")
        output = next(n for n in graph["nodes"] if n["type"] == "OUTPUT_MATERIAL_PBR")
        assert output["inputs"][0]["default_value"][:3] == pytest.approx([0.1, 0.3, 0.7])
        assert not any(l["to_node"] == output["id"] and l["to_socket"] == 0 for l in graph["links"])
        fill = await call("create_layer", name='中文 "fill"', kind="fill")
        fill_id = fill["state"]["selected_layer_id"]
        assert next(l for l in fill["state"]["layers"] if l["id"] == fill_id)["name"] == '中文 "fill"'
        rejected = await client.call_tool("paint_stroke", {"points": [[0.5, 0.5]]})
        assert rejected.isError
        paint = await call("create_layer", name="Stroke")
        paint_id = paint["state"]["selected_layer_id"]
        await call("set_brush", radius=0.1)
        await call("paint_stroke", points=[[0.4, 0.5], [0.5, 0.5], [0.6, 0.5]], layer_id=paint_id)
        await call("set_layer_properties", layer_id=paint_id, opacity=0.75)
        state = await call("get_application_state")
        assert next(l for l in state["layers"] if l["id"] == paint_id)["opacity"] == pytest.approx(0.75)
        await call("undo_redo", action="undo")
        state = await call("get_application_state")
        assert next(l for l in state["layers"] if l["id"] == paint_id)["opacity"] == pytest.approx(1)
        await call("undo_redo", action="redo")
        obj = state["objects"][0]["name"]
        transformed = await call("object_operation", name=obj, action="transform", location=[0.1, 0, 0])
        assert transformed["state"]["objects"][0]["location"][0] == pytest.approx(0.1)
        project = tmp_path / "roundtrip.arm"
        await call("project_operation", action="save", path=str(project))
        assert project.stat().st_size > 100
        for fmt in ("png", "exr16", "exr32", "jpg"):
            directory = tmp_path / fmt
            directory.mkdir()
            await call("export_asset", kind="textures", path=str(directory), format=fmt)
            files = list(directory.iterdir())
            assert len(files) == 5
            assert all(p.stat().st_size > 100 for p in files)
            if fmt.startswith("exr"):
                assert all(p.read_bytes()[:4] == b"\x76\x2f\x31\x01" for p in files)
        await call("project_operation", action="new")
        await call("project_operation", action="open", path=str(project))
        state = await call("get_application_state")
        assert any(l["name"] == '中文 "fill"' for l in state["layers"])
        screenshot = await client.call_tool("get_screenshot", {})
        assert not screenshot.isError
        assert screenshot.content[0].type == "image"
        # This adapter passes a value struct into a native app function.
        await call("execute_code", code="void main() { object_t *o = script_get_object(\"" + obj + "\"); point_in_aabb(o, o->transform->loc); }", retain_context=False)
        # A native callback must hold the RPC open until its completion hook.
        await call("execute_code", code="void done() { mcp_task_end(NULL); } void main() { mcp_task_begin(); script_screenshot_queue(\"" + str(tmp_path / "callback.png") + "\", 2, done); }")
        assert (tmp_path / "callback.png").is_file()
        error = await client.call_tool("execute_code", {"code": "void main() { nonexistent_operator(); }", "retain_context": False})
        assert error.isError
        assert "nonexistent_operator" in error.content[0].text
        runaway = await client.call_tool("execute_code", {"code": "void main() { while (1) {} }", "retain_context": False})
        assert runaway.isError
        assert "instruction limit" in runaway.content[0].text
        started = await call("player_operation", action="start")
        assert started["state"]["player_running"]
        playing_shot = await client.call_tool("get_screenshot", {})
        assert not playing_shot.isError
        stopped = await call("player_operation", action="stop")
        assert not stopped["state"]["player_running"]
        await call("get_application_state")
    asyncio.run(with_client(os.environ["ARMORPAINT_MCP_TEST_SOCKET"], check))
