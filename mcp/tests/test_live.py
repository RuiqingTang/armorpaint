"""Destructive regression tests, enabled only for an explicitly disposable app."""
import asyncio
import concurrent.futures
import json
import os
import socket
import time

import pytest

from armorpaint_mcp import server
from armorpaint_mcp.bridge import Bridge, BridgeError

pytestmark = pytest.mark.skipif(
    not os.environ.get("ARMORPAINT_MCP_TEST_SOCKET"),
    reason="requires an explicitly opted-in disposable ArmorPaint instance",
)


def run(awaitable):
    return asyncio.run(awaitable)


@pytest.fixture
def app(monkeypatch):
    bridge = Bridge(os.environ["ARMORPAINT_MCP_TEST_SOCKET"])
    monkeypatch.setattr(server, "bridge", bridge)
    run(server.project_operation("new"))
    return bridge


@pytest.mark.parametrize("name", ['中文 "引号" \\ path\nline\tend', "prefix\bmiddle", "prefix\fmiddle", "prefix\x01middle", "prefix\x1fmiddle", "emoji \U0001f3a8"])
def test_layer_names_roundtrip_without_truncation(app, name):
    result = run(server.create_layer(name=name))
    state = result["state"]
    layer = next(l for l in state["layers"] if l["id"] == state["selected_layer_id"])
    assert layer["name"] == name


def test_bottom_layer_with_mask_cannot_merge_down(app):
    bottom_id = app.request("state")["layers"][0]["id"]
    run(server.create_layer(kind="mask", parent_id=bottom_id))
    before = app.request("state")
    with pytest.raises((ValueError, BridgeError), match="merge"):
        run(server.layer_operation(bottom_id, "merge_down"))
    after = app.request("state")
    assert after["layers"] == before["layers"]


@pytest.mark.parametrize("with_mask", [False, True])
def test_native_merge_of_bottom_layer_does_not_crash(app, with_mask):
    bottom_id = app.request("state")["layers"][0]["id"]
    if with_mask:
        run(server.create_layer(kind="mask", parent_id=bottom_id))
    before = app.request("state")
    run(server.execute_code(
        f"void main() {{ context_set_layer(mcp_get_layer({bottom_id})); layers_merge_down(); }}",
        retain_context=False,
    ))
    assert app.request("state")["layers"] == before["layers"]


def test_mask_cannot_merge_into_paint_layer(app):
    second = run(server.create_layer(name="Second"))["state"]["selected_layer_id"]
    mask = run(server.create_layer(kind="mask", parent_id=second))["state"]["selected_layer_id"]
    before = app.request("state")
    with pytest.raises((ValueError, BridgeError), match="merge"):
        run(server.layer_operation(mask, "merge_down"))
    assert app.request("state")["layers"] == before["layers"]


def test_merge_into_fill_layer_becomes_paint(app):
    fill = run(server.create_layer(kind="fill"))["state"]["selected_layer_id"]
    top = run(server.create_layer(name="Top"))["state"]["selected_layer_id"]
    result = run(server.layer_operation(top, "merge_down"))
    state = result["state"]
    assert state["selected_layer_id"] == fill
    assert next(l for l in state["layers"] if l["id"] == fill)["fill_material_id"] == -1


def test_layer_property_history_roundtrip(app):
    layer_id = run(server.create_layer(name="Original"))["state"]["selected_layer_id"]
    run(server.set_layer_properties(layer_id, name="Renamed", opacity=0.4, visible=False, blending=2))
    state = app.request("state")
    layer = next(l for l in state["layers"] if l["id"] == layer_id)
    assert (layer["name"], layer["visible"], layer["blending"]) == ("Renamed", False, 2)
    assert layer["opacity"] == pytest.approx(0.4)
    run(server.undo_redo("undo", steps=4))
    layer = next(l for l in app.request("state")["layers"] if l["id"] == layer_id)
    assert (layer["name"], layer["visible"], layer["blending"], layer["opacity"]) == ("Original", True, 0, 1)
    run(server.undo_redo("redo", steps=4))
    layer = next(l for l in app.request("state")["layers"] if l["id"] == layer_id)
    assert (layer["name"], layer["visible"], layer["blending"]) == ("Renamed", False, 2)
    assert layer["opacity"] == pytest.approx(0.4)


def test_node_create_connect_disconnect_and_undo(app):
    run(server.create_material("Node tests"))
    graph = run(server.get_node_graph())
    output_id = next(n["id"] for n in graph["nodes"] if n["type"] == "OUTPUT_MATERIAL_PBR")
    before_ids = {n["id"] for n in graph["nodes"]}
    run(server.material_node_operation("create", node_type="VALUE"))
    graph = run(server.get_node_graph())
    value_id = next(n["id"] for n in graph["nodes"] if n["id"] not in before_ids)
    run(server.material_node_operation("float", node_id=value_id, socket=0, is_input=False, values=[0.23]))
    run(server.material_node_operation("connect", node_id=output_id, socket=3, from_node_id=value_id))
    assert any(l["from_node"] == value_id and l["to_node"] == output_id and l["to_socket"] == 3 for l in run(server.get_node_graph())["links"])
    run(server.material_node_operation("disconnect", node_id=output_id, socket=3))
    assert not any(l["to_node"] == output_id and l["to_socket"] == 3 for l in run(server.get_node_graph())["links"])
    run(server.undo_redo("undo"))
    assert any(l["from_node"] == value_id and l["to_socket"] == 3 for l in run(server.get_node_graph())["links"])


def test_settings_roundtrip_and_context_lifetime(app):
    before = app.request("state")["retained_contexts"]
    run(server.set_settings("context_t", {"brush_radius": 0.37, "xray": True}))
    settings = {s["name"]: s["value"] for s in run(server.get_settings("context_t"))}
    assert settings["brush_radius"] == pytest.approx(0.37)
    assert settings["xray"] is True
    for _ in range(3):
        run(server.execute_code("int main() { return 42; }", retain_context=False))
    assert app.request("state")["retained_contexts"] == before


def test_object_duplicate_rename_visibility_delete(app):
    name = app.request("state")["objects"][0]["name"]
    result = run(server.object_operation(name, "duplicate"))
    assert len(result["state"]["objects"]) == 2
    duplicate = next(o["name"] for o in result["state"]["objects"] if o["name"] != name)
    run(server.object_operation(duplicate, "rename", new_name='中文 "copy"'))
    run(server.object_operation('中文 "copy"', "visibility", visible=False))
    state = app.request("state")
    assert next(o for o in state["objects"] if o["name"] == '中文 "copy"')["visible"] is False
    result = run(server.object_operation('中文 "copy"', "delete"))
    assert len(result["state"]["objects"]) == 1
    with pytest.raises(ValueError, match="last mesh"):
        run(server.object_operation(name, "delete"))


def raw_request(path, request):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
        peer.settimeout(5)
        peer.connect(str(path))
        peer.sendall(request)
        data = bytearray()
        while b"\n" not in data:
            chunk = peer.recv(4096)
            assert chunk, "Application disconnected without replying"
            data.extend(chunk)
        return json.loads(data.split(b"\n", 1)[0])


@pytest.mark.parametrize(
    "wire_request",
    [
        b'{"id":"raw","method":"ping"} {}\n',
        b'{"id":"raw","method":"execute","code":"void main() {}","retain_context":"false"}\n',
        b'{"id":"raw","method":"execute","code":"void main() {}","wait_frames":1.5}\n',
        b'{"id":"raw","method":"execute","code":"void main() {}","wait_frames":1e100}\n',
        b'{"id":"raw","method":"execute","code":"void main() {}","wait_frames":"4"}\n',
        b'{"id":"raw","method":"execute","code":"void main() {}","retain_context":null}\n',
        b'{"id":"raw","method":"execute","code":"void main() {}","timeout":0}\n',
    ],
)
def test_native_bridge_rejects_invalid_request_before_execution(app, wire_request):
    before = app.request("state")
    result = raw_request(app.path, wire_request)
    assert result["ok"] is False
    assert "Invalid" in result["error"]
    after = app.request("state")
    assert after["retained_contexts"] == before["retained_contexts"]
    assert after["layers"] == before["layers"]


@pytest.mark.parametrize(
    "wire_request",
    [
        b'{"id":"raw","method":"unknown"}\n',
        b'{"id":"raw","method":"execute","code":""}\n',
        b'{"id":"raw","method":"execute","code":"\\u0000"}\n',
        b'{"id":"raw","method":"execute","code":"\\ud800"}\n',
        b'{"id":"raw","method":"screenshot","path":"relative.png"}\n',
        b'{"id":"raw","method":null}\n',
        b'[]\n',
    ],
)
def test_native_bridge_errors_leave_application_usable(app, wire_request):
    assert raw_request(app.path, wire_request)["ok"] is False
    assert app.request("ping") == "ArmorPaint MCP bridge v1"


def test_read_queries_remain_available_during_mutation(app):
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        mutation = executor.submit(
            app.request, "execute", code="void main() {}", wait_frames=120, retain_context=False
        )
        observer = Bridge(app.path)
        deadline = time.monotonic() + 3
        while not observer.request("state")["busy"]:
            assert time.monotonic() < deadline and not mutation.done()
            time.sleep(0.01)
        assert app.request("state")["busy"] is True
        with pytest.raises(BridgeError, match="busy"):
            observer.request("execute", code="void main() {}", retain_context=False)
        assert mutation.result(timeout=10)["status"] == "completed"
    assert app.request("state")["busy"] is False


def test_explicit_async_task_error_preserves_log_and_partial_changes(app):
    layer_id = app.request("state")["selected_layer_id"]
    with pytest.raises(BridgeError, match="intentional failure") as exc:
        run(server.execute_code(
            f'void done() {{ console_log("completed callback"); mcp_task_end("intentional failure"); }} '
            f'void main() {{ mcp_get_layer({layer_id})->name = string_copy("Partial change"); '
            'mcp_task_begin(); script_notify_on_next_frame(done); }'
        ))
    assert exc.value.result["changes_may_have_been_applied"] is True
    assert "completed callback" in exc.value.result["log"]
    assert next(l for l in app.request("state")["layers"] if l["id"] == layer_id)["name"] == "Partial change"
    assert run(server.execute_code("int main() { return 7; }", retain_context=False))["return_value"] == 7


def test_multiple_async_tasks_wait_for_all_callbacks(app):
    result = run(server.execute_code(
        'void second() { console_log("second"); mcp_task_end(NULL); } '
        'void first() { console_log("first"); mcp_task_end(NULL); script_notify_on_next_frame(second); } '
        'void main() { mcp_task_begin(); mcp_task_begin(); script_notify_on_next_frame(first); }'
    ))
    assert "first" in result["log"] and "second" in result["log"]
    assert result["status"] == "completed"
