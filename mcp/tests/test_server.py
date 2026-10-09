import asyncio
import copy
from unittest.mock import AsyncMock

import pytest

from armorpaint_mcp import server
from armorpaint_mcp.bridge import BridgeError


def run(awaitable):
    return asyncio.run(awaitable)


@pytest.fixture
def rpc(monkeypatch):
    state = {
        "project_path": "",
        "selected_layer_id": 1,
        "layers": [
            {"id": 1, "type": "layer", "parent_id": -1, "fill_material_id": -1},
            {"id": 2, "type": "mask", "parent_id": 1, "fill_material_id": -1},
            {"id": 3, "type": "group", "parent_id": -1, "fill_material_id": -1},
            {"id": 4, "type": "layer", "parent_id": -1, "fill_material_id": 0},
        ],
        "objects": [{"name": "Cube"}, {"name": "Sphere"}],
        "materials": [{"id": 0, "name": "Material"}],
        "brushes": [{"id": 0}],
    }
    graph = {
        "nodes": [
            {
                "id": 10,
                "type": "OUTPUT_MATERIAL_PBR",
                "inputs": [
                    {"type": "RGBA", "default_value": [0.5, 0.5, 0.5, 1]},
                    {"type": "VALUE", "default_value": [0.5]},
                    {"type": "VECTOR", "default_value": [0, 0, 0]},
                ],
                "outputs": [],
                "buttons": [],
            },
            {
                "id": 11,
                "type": "VALUE",
                "inputs": [],
                "outputs": [{"type": "VALUE", "default_value": [0.5]}],
                "buttons": [{"type": "VALUE", "default_value": [0.5]}],
            },
        ],
        "links": [],
    }
    replies = {
        "state": state,
        "material_graph": graph,
        "settings": {
            "settings": [
                {"scope": "context_t", "name": "xray", "type": "b", "value": False},
                {"scope": "context_t", "name": "tool", "type": "i", "value": 0},
                {"scope": "context_t", "name": "brush_radius", "type": "f", "value": 0.5},
                {"scope": "config_t", "name": "workspace", "type": "i", "value": 0},
            ],
        },
        "execute": {"status": "completed", "log": ""},
    }

    async def reply(method, **params):
        return copy.deepcopy(replies[method])

    mock = AsyncMock(side_effect=reply)
    mock.replies = replies
    monkeypatch.setattr(server, "rpc", mock)
    return mock


def executions(rpc):
    return [call.kwargs for call in rpc.call_args_list if call.args == ("execute",)]


@pytest.mark.parametrize("value", ["\0", "prefix\0suffix"])
def test_cstr_rejects_nul(value):
    with pytest.raises(ValueError, match="NUL"):
        server.cstr(value)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), "1", None])
def test_number_rejects_nonfinite_and_nonnumeric_values(value):
    with pytest.raises(ValueError, match="finite number"):
        server.number(value)


@pytest.mark.parametrize("value", [-0.1, 1.1, float("nan"), float("inf")])
def test_unit_rejects_out_of_range_values(value):
    with pytest.raises(ValueError, match="between 0 and 1"):
        server.unit(value, "opacity")


def test_path_validation(tmp_path):
    with pytest.raises(ValueError, match="absolute"):
        server.path_arg("relative.arm")
    with pytest.raises(ValueError, match="does not exist"):
        server.path_arg(str(tmp_path / "missing.arm"), exists=True)
    with pytest.raises(ValueError, match="Parent directory"):
        server.path_arg(str(tmp_path / "missing" / "project.arm"))
    with pytest.raises(ValueError, match="directory"):
        server.path_arg(str(tmp_path / "missing"), directory=True)
    assert server.path_arg(str(tmp_path / "project.arm")) == tmp_path / "project.arm"


def test_file_inputs_reject_directories_before_execution(tmp_path, rpc):
    directory = tmp_path / "directory.arm"
    directory.mkdir()
    with pytest.raises(ValueError, match="file"):
        run(server.project_operation("open", str(directory)))
    with pytest.raises(ValueError, match="file"):
        run(server.import_asset(str(directory)))
    with pytest.raises(ValueError, match="file"):
        run(server.project_operation("save", str(directory)))
    assert not executions(rpc)


@pytest.mark.parametrize("timeout,frames", [(0, 4), (301, 4), (float("nan"), 4), (60, 0), (60, 121)])
def test_execute_bounds_checked_without_rpc(rpc, timeout, frames):
    with pytest.raises(ValueError):
        run(server.execute_code("void main() {}", timeout=timeout, wait_frames=frames))
    rpc.assert_not_called()


def test_empty_code_rejected_without_rpc(rpc):
    with pytest.raises(ValueError, match="empty"):
        run(server.execute_code(" \n\t"))
    rpc.assert_not_called()


@pytest.mark.parametrize(
    "declaration,arguments,expected",
    [
        ("int answer(void);", [], "int main() { return answer(); }"),
        ("float answer();", [], "float main() { return answer(); }"),
        ("void answer(int count, char *name);", [2, '中文 "quoted"'], 'answer(2, "中文 \\"quoted\\"")'),
        ("void answer(void *value);", [None], "answer(NULL)"),
        ("void answer(slot_layer_t *layer);", [{"expression": "mcp_get_layer(1)"}], "answer(mcp_get_layer(1))"),
        ("void answer(bool enabled);", [True], "answer(true)"),
        ("char *answer(void);", [], "console_log(result == NULL"),
        ("string_array_t *answer(void);", [], "result->buffer[i]"),
    ],
)
def test_call_function_generates_correct_script(rpc, declaration, arguments, expected):
    rpc.replies["api"] = declaration
    run(server.call_function("answer", arguments))
    assert expected in executions(rpc)[0]["code"]


@pytest.mark.parametrize("name", ["bad();", "bad-name", "évil"])
def test_invalid_function_name_rejected_without_rpc(rpc, name):
    with pytest.raises(ValueError, match="function name"):
        run(server.call_function(name, []))
    rpc.assert_not_called()


def test_call_function_rejects_unknown_name_and_wrong_arity(rpc):
    rpc.replies["api"] = "void answer(int count);"
    with pytest.raises(ValueError, match="not registered"):
        run(server.call_function("missing", []))
    with pytest.raises(ValueError, match="Expected 1 arguments"):
        run(server.call_function("answer", []))
    assert not executions(rpc)


def test_api_search_and_pagination(rpc):
    rpc.replies["api"] = "void Foo(void);\nint food(void);\nvoid bar(void);"
    result = run(server.get_api_reference("FOO", limit=1))
    assert result == {"total_lines": 2, "offset": 0, "lines": ["void Foo(void);"], "next_offset": 1}
    assert run(server.get_api_reference("foo", offset=1, limit=1))["next_offset"] is None


def test_settings_scope_filter(rpc):
    result = run(server.get_settings("context_t", "BRUSH"))
    assert [s["name"] for s in result] == ["brush_radius"]


@pytest.mark.parametrize("values", [{"missing": 1}, {"xray": 1}, {"tool": True}, {"tool": 2**31}, {"tool": 0.5}, {"brush_radius": float("inf")}])
def test_invalid_settings_do_not_partially_execute(rpc, values):
    with pytest.raises(ValueError):
        run(server.set_settings("context_t", {"xray": True, **values}))
    assert not executions(rpc)


def test_float_settings_reject_overflow_before_native_cast(rpc):
    with pytest.raises(ValueError, match="32-bit"):
        run(server.set_settings("context_t", {"brush_radius": 1e100}))
    assert not executions(rpc)


def test_missing_layer_is_actionable(rpc):
    with pytest.raises(ValueError, match="current IDs"):
        run(server.layer_operation(999, "select"))
    assert not executions(rpc)


def test_mask_cannot_parent_another_mask(rpc):
    with pytest.raises(ValueError, match="parent.*mask|mask.*parent"):
        run(server.create_layer(kind="mask", parent_id=2))
    assert not executions(rpc)


def test_merge_uses_native_eligibility_guard_before_history(rpc):
    run(server.layer_operation(4, "merge_down"))
    code = executions(rpc)[0]["code"]
    assert "mcp_can_merge_layer(l)" in code
    assert code.index("mcp_can_merge_layer(l)") < code.index("history_merge_layers()")
    assert "slot_layer_to_paint_layer" in code


@pytest.mark.parametrize("action", ["invert_mask", "apply_mask"])
def test_mask_only_operations_rejected_for_layers(rpc, action):
    with pytest.raises(ValueError, match="requires a mask"):
        run(server.layer_operation(1, action))
    assert not executions(rpc)


@pytest.mark.parametrize("layer_id", [3, 4])
def test_strokes_reject_groups_and_fill_layers(rpc, layer_id):
    with pytest.raises(ValueError, match="paint layer or mask"):
        run(server.paint_stroke([[0.5, 0.5]], layer_id=layer_id))
    assert not executions(rpc)


@pytest.mark.parametrize("points", [[], [[0.5]], [[-0.1, 0.5]], [[0.5, float("nan")]], [[0.5, 0.5]] * 2001])
def test_invalid_stroke_coordinates_do_not_execute(rpc, points):
    with pytest.raises(ValueError):
        run(server.paint_stroke(points))
    assert not executions(rpc)


def test_world_stroke_keeps_negative_coordinates(rpc):
    run(server.paint_stroke([[-1, 2, -3]], space="world"))
    code = executions(rpc)[0]["code"]
    assert "{-1, 2, -3}" in code
    assert "script_paint_world" in code
    assert "mcp_task_begin()" in code and "mcp_task_end(NULL)" in code


def test_fill_reports_missing_selection(rpc):
    rpc.replies["state"]["selected_layer_id"] = -1
    with pytest.raises(ValueError, match="layer"):
        run(server.fill_layer())
    assert not executions(rpc)


def test_remove_output_node_rejected_before_history(rpc):
    with pytest.raises(ValueError, match="PBR output"):
        run(server.material_node_operation("remove", node_id=10))
    assert not executions(rpc)


@pytest.mark.parametrize("action,socket,values", [("float", 0, [0.2]), ("color", 2, [1, 0, 0, 1]), ("vector", 0, [1, 2, 3])])
def test_node_values_require_matching_socket_type(rpc, action, socket, values):
    with pytest.raises(ValueError, match="value type"):
        run(server.material_node_operation(action, node_id=10, socket=socket, values=values))
    assert not executions(rpc)


def test_node_button_rejects_non_numeric_button(rpc):
    rpc.replies["material_graph"]["nodes"][1]["buttons"] = [{"type": "TEXT", "data": "text"}]
    with pytest.raises(ValueError, match="numeric"):
        run(server.material_node_operation("button", node_id=11, values=[1]))
    assert not executions(rpc)


def test_node_text_rejects_numeric_button(rpc):
    with pytest.raises(ValueError, match="text"):
        run(server.material_node_operation("text", node_id=11, text="invalid"))
    assert not executions(rpc)


@pytest.mark.parametrize("values", [[2], [-1], [0.5], [float("nan")]])
def test_camera_projection_rejects_invalid_enum(rpc, values):
    with pytest.raises(ValueError, match="projection"):
        run(server.camera_operation("projection", values))
    assert not executions(rpc)


def test_multistep_undo_waits_between_steps(rpc):
    run(server.undo_redo("undo", steps=3))
    calls = executions(rpc)
    assert len(calls) == 3
    assert all("history_undo();" in c["code"] and c["wait_frames"] == 6 for c in calls)


def test_save_checks_native_output(tmp_path, rpc):
    with pytest.raises(BridgeError, match="nonempty project"):
        run(server.project_operation("save", str(tmp_path / "project.arm")))


def test_empty_project_rejected_before_native_decode(tmp_path, rpc):
    path = tmp_path / "empty.arm"
    path.write_bytes(b"")
    with pytest.raises(ValueError, match="empty"):
        run(server.project_operation("open", str(path)))
    rpc.assert_not_called()


@pytest.mark.parametrize("data", [b"x", b"\xdf\0\0\0", b"not a project file"])
def test_invalid_project_header_rejected_before_native_decode(tmp_path, rpc, data):
    path = tmp_path / "broken.arm"
    path.write_bytes(data)
    with pytest.raises(ValueError, match="Invalid or truncated"):
        run(server.project_operation("open", str(path)))
    rpc.assert_not_called()


def test_screenshot_requires_file(rpc):
    rpc.replies["screenshot"] = {"status": "completed"}
    with pytest.raises(BridgeError, match="without an output file"):
        run(server.get_screenshot())
