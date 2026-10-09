"""Stdio MCP server for the native ArmorPaint bridge."""
import argparse
import asyncio
import json
import math
import re
import tempfile
from pathlib import Path
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP, Image
from mcp.types import ToolAnnotations

from .bridge import Bridge, BridgeError

mcp = FastMCP(
    "ArmorPaint",
    instructions=(
        "Control a running ArmorPaint application. First get_application_state, then search "
        "get_api_reference/get_node_reference before writing scripts. Prefer dedicated tools; "
        "execute_code and call_function expose the registered native API for advanced operations. "
        "Scripts are interpreted C with void main(), no preprocessor. They run on the app's main "
        "thread and have native application access. Get a screenshot to check visual results. "
        "Never automatically retry a failed mutation: partial changes can remain. "
        "Use mcp_task_begin()/mcp_task_end(error) for asynchronous native callbacks."
    ),
)
bridge = Bridge()
READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True)


def cstr(value: str) -> str:
    if "\0" in value:
        raise ValueError("Strings cannot contain NUL")
    # minic handles these C escapes but not JSON's \b, \f or \uXXXX.
    escapes = {'"': '\\"', "\\": "\\\\", "\n": "\\n", "\r": "\\r", "\t": "\\t"}
    return '"' + "".join(escapes.get(c, c) for c in value) + '"'


def number(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("Expected a finite number")
    return repr(value)


def unit(value, name):
    if not 0 <= value <= 1 or not math.isfinite(value):
        raise ValueError(f"{name} must be between 0 and 1")
    return number(value)


def path_arg(path, *, exists=False, directory=False):
    p = Path(path).expanduser()
    if not p.is_absolute():
        raise ValueError("Use an absolute filesystem path")
    if exists and not p.exists():
        raise ValueError(f"Path does not exist: {p}")
    if directory and not p.is_dir():
        raise ValueError(f"Expected an existing directory: {p}")
    if not directory and p.exists() and not p.is_file():
        raise ValueError(f"Expected a file path: {p}")
    if not exists and not p.parent.is_dir():
        raise ValueError(f"Parent directory does not exist: {p.parent}")
    return p


async def rpc(method, **params):
    return await asyncio.to_thread(bridge.request, method, **params)


async def run_body(body, *, wait_frames=4, timeout=60, retain_context=False):
    return await rpc("execute", code="void main() {\n" + body + "\n}", wait_frames=wait_frames, timeout=timeout, retain_context=retain_context)


async def layer_prefix(layer_id):
    state = await rpc("state")
    if layer_id not in [l["id"] for l in state["layers"]]:
        raise ValueError(f"Layer {layer_id} does not exist; query state for current IDs")
    return f"slot_layer_t *l = mcp_get_layer({int(layer_id)}); if (l == NULL) {{ console_error(\"Layer no longer exists\"); mcp_task_end(\"Layer no longer exists\"); return; }} context_set_layer(l);\n"


async def object_prefix(name):
    state = await rpc("state")
    if name not in [o["name"] for o in state["objects"]]:
        raise ValueError(f"Object {name!r} does not exist")
    return f"object_t *o = script_get_object({cstr(name)}); if (o == NULL) {{ mcp_task_end(\"Object no longer exists\"); return; }}\n"


@mcp.tool(annotations=READ)
async def get_application_state() -> dict[str, Any]:
    """Read live project, object transforms, layer/material/brush IDs, assets and selection."""
    return await rpc("state")


@mcp.tool(annotations=READ)
async def get_api_reference(query: str = "", offset: int = 0, limit: int = 100) -> dict[str, Any]:
    """Search the running app's actual C API, enum constants and registered struct fields.

    Empty query returns paginated full reference. Search e.g. history_, layers_,
    script_material_, viewport_, bake_, context_t, WORKFLOW_. Use armorpaint://api
    to read the complete reference. Declaration availability is build dependent.
    """
    if offset < 0 or not 1 <= limit <= 500:
        raise ValueError("offset >= 0 and 1 <= limit <= 500 are required")
    lines = (await rpc("api")).splitlines()
    matches = [line for line in lines if query.lower() in line.lower()]
    return {"total_lines": len(matches), "offset": offset, "lines": matches[offset:offset + limit], "next_offset": offset + limit if offset + limit < len(matches) else None}


@mcp.tool(annotations=READ)
async def get_node_reference(query: str = "") -> str:
    """Get actual material node types, input/output socket indices and button options."""
    text = await rpc("nodes")
    if not query:
        return text
    groups = re.split(r"(?=// [A-Z][A-Z_0-9]*(?: \||\n))", text)
    return "\n".join(g for g in groups if query.lower() in g.lower()) or "No matching node types"


@mcp.tool(annotations=READ)
async def get_settings(scope: Literal["context_t", "config_t", "all"] = "all", query: str = "") -> list[dict[str, Any]]:
    """Read scalar application/brush/baking/workspace/config settings with exact field names."""
    result = await rpc("settings")
    return [s for s in result["settings"] if (scope == "all" or s["scope"] == scope) and query.lower() in s["name"].lower()]


@mcp.tool(annotations=READ)
async def get_node_graph(kind: Literal["material", "brush"] = "material") -> dict[str, Any]:
    """Read the selected material/brush's actual nodes, socket values, buttons and links."""
    return await rpc(kind + "_graph")


@mcp.tool(annotations=DESTRUCTIVE)
async def execute_code(code: str, timeout: float = 60, wait_frames: int = 4, retain_context: bool = True) -> dict[str, Any]:
    """Execute interpreted C in the live project, analogous to Blender MCP execute_code.

    Supply a full main() function, without #include/preprocessor directives.
    void main() is usual; int/float/double main() can return a scalar result.
    Query the API first; this is C, not Python. console_log/printf output is returned.
    For async work call mcp_task_begin() before scheduling, then mcp_task_end(NULL)
    in its completion callback (pass an error string on failure). wait_frames only
    allows frame callbacks to settle; it cannot detect arbitrary native async work.
    A VM instruction limit stops runaway script loops, not blocking native calls.
    Failed scripts are not transactions and may leave partial changes.
    Keep retain_context=true if callbacks or script-owned pointers escape main().
    Retained contexts are capped at 256 per app session; restart at that limit.
    """
    if not 1 <= timeout <= 300 or not 1 <= wait_frames <= 120:
        raise ValueError("timeout must be 1–300 seconds; wait_frames must be 1–120")
    if not code.strip():
        raise ValueError("Code cannot be empty")
    return await rpc("execute", code=code, timeout=timeout, wait_frames=wait_frames, retain_context=retain_context)


@mcp.tool(annotations=DESTRUCTIVE)
async def call_function(name: str, arguments: list, timeout: float = 60, wait_frames: int = 4) -> dict[str, Any]:
    """Call any registered native operation without writing an entire C script.

    Primitive arguments become C literals. For pointers/enums/struct expressions,
    use {"expression": "mcp_get_layer(2)"} or {"expression": "UV_TYPE_UVMAP"}.
    Use execute_code for local variables, multi-step logic and async callbacks.
    Query the API and state first; internal functions can require GPU/UI setup.
    """
    if not re.fullmatch(r"[A-Za-z_]\w*", name):
        raise ValueError("Invalid function name")
    api = await rpc("api")
    declaration = re.search(r"^.+?\b" + re.escape(name) + r"\((.*?)\);$", api, re.M)
    if declaration is None:
        raise ValueError(f"{name} is not registered in the running build")
    params = declaration[1]
    if "..." not in params:
        count = 0 if params.strip() in ("", "void") else len(params.split(","))
        if len(arguments) != count:
            raise ValueError(f"Expected {count} arguments for {declaration[0]}")
    literals = []
    for arg in arguments:
        if isinstance(arg, str):
            literals.append(cstr(arg))
        elif arg is None:
            literals.append("NULL")
        elif isinstance(arg, (bool, int, float)):
            literals.append(number(arg))
        elif isinstance(arg, dict) and set(arg) == {"expression"} and isinstance(arg["expression"], str):
            literals.append(arg["expression"])
        else:
            raise ValueError("Arguments must be primitives or {expression: C expression}")
    invocation = f"{name}({', '.join(literals)})"
    returns = declaration[0].split(name + "(", 1)[0].strip()
    if returns in ("int", "bool", "float", "double", "char"):
        code = f"{returns} main() {{ return {invocation}; }}"
    elif returns.replace(" ", "") == "char*":
        code = f"void main() {{ char *result = {invocation}; console_log(result == NULL ? \"NULL\" : result); }}"
    elif returns.replace(" ", "") == "string_array_t*":
        code = f"void main() {{ string_array_t *result = {invocation}; if (result != NULL) for (int i = 0; i < result->length; i++) console_log(result->buffer[i]); }}"
    else:
        code = f"void main() {{ {invocation}; }}"
    return await execute_code(code, timeout, wait_frames)


@mcp.tool(annotations=WRITE)
async def set_settings(scope: Literal["context_t", "config_t"], values: dict) -> dict[str, Any]:
    """Set named scalar settings after querying get_settings. Raw settings require caller validation.

    For workspace/tool changes prefer call_function on context_select_tool,
    base_update_workspace etc.; raw assignment alone may require an update hook.
    """
    settings = {s["name"]: s for s in await get_settings(scope)}
    body = []
    for name, value in values.items():
        if name not in settings:
            raise ValueError(f"Unknown {scope} field: {name}")
        kind = settings[name]["type"]
        if kind == "b" and not isinstance(value, bool):
            raise ValueError(f"{name} requires a boolean")
        if kind == "i" and (isinstance(value, bool) or not isinstance(value, int) or not -(2**31) <= value < 2**31):
            raise ValueError(f"{name} requires a signed 32-bit integer")
        if kind == "f" and isinstance(value, (int, float)) and abs(value) > 3.4028234663852886e38:
            raise ValueError(f"{name} requires a finite 32-bit float")
        body.append(f"if (!mcp_set_setting({cstr(scope)}, {cstr(name)}, {number(value)})) mcp_task_end(\"Setting failed\");")
    return await run_body("\n".join(body))


@mcp.tool(annotations=DESTRUCTIVE)
async def project_operation(action: Literal["new", "open", "save"], path: str = "") -> dict[str, Any]:
    """Create/open/save an .arm project. new/open replace the live project; save first if needed.

    Supply an absolute .arm path for open or first save. Subsequent saves may omit it.
    """
    if action == "new":
        return await run_body("script_project_new();", wait_frames=8)
    if path:
        p = path_arg(path, exists=action == "open")
        if p.suffix.lower() != ".arm":
            raise ValueError("Project path must end in .arm")
        if action == "open":
            with p.open("rb") as file:
                header = file.read(11)
            if not header:
                raise ValueError("Cannot open an empty project file")
            if len(header) < 11 or header[0] != 0xdf:
                raise ValueError("Invalid or truncated .arm project header")
    else:
        if action == "open":
            raise ValueError("Opening a project requires a path")
        state = await rpc("state")
        if not state["project_path"]:
            raise ValueError("First save requires an absolute .arm path")
        p = path_arg(state["project_path"])
    if action == "open":
        return await run_body(f"script_project_open({cstr(str(p))});", wait_frames=8)
    result = await run_body(f"project_filepath_set({cstr(str(p))}); project_save(false);", wait_frames=8)
    if not p.is_file() or p.stat().st_size == 0:
        raise BridgeError("Save did not produce a nonempty project file", result)
    return {**result, "path": str(p)}


@mcp.tool(annotations=WRITE)
async def import_asset(path: str, append_mesh: bool = False, hdr_as_envmap: bool = True) -> dict[str, Any]:
    """Import a model, texture, HDR environment, material, brush or other supported asset.

    append_mesh keeps existing objects; normal mesh import uses the app's import behavior.
    """
    p = path_arg(path, exists=True)
    if append_mesh:
        body = f"script_append_mesh({cstr(str(p))});"
    else:
        body = f"script_import_asset({cstr(str(p))}, {number(hdr_as_envmap)});"
    return await run_body(body, wait_frames=10)


@mcp.tool(annotations=WRITE)
async def export_asset(kind: Literal["mesh", "material", "textures"], path: str, preset: str = "generic", format: Literal["png", "jpg", "exr16", "exr32"] = "png") -> dict[str, Any]:
    """Export the current mesh/material or PBR textures to an absolute path.

    Textures require an existing output directory. preset is a bundled export
    preset such as generic, unreal or unity. Uses the project's current resolution.
    """
    p = path_arg(path, directory=kind == "textures")
    if kind == "textures":
        if not re.fullmatch(r"[A-Za-z0-9_]+", preset):
            raise ValueError("Invalid preset name")
        bits = {"png": "TEXTURE_BITS_BITS8", "jpg": "TEXTURE_BITS_BITS8", "exr16": "TEXTURE_BITS_BITS16", "exr32": "TEXTURE_BITS_BITS32"}[format]
        fmt = "TEXTURE_LDR_FORMAT_JPG" if format == "jpg" else "TEXTURE_LDR_FORMAT_PNG"
        body = (
            f"buffer_t *b = data_get_blob({cstr('export_presets/' + preset + '.json')}); "
            "if (b == NULL) { mcp_task_end(\"Export preset not found\"); return; } "
            f"export_preset_t *ep = json_parse(sys_buffer_to_string(b)); "
            f"mcp_export_textures({cstr(str(p))}, ep, {bits}, {fmt});"
        )
    else:
        body = f"script_export_{kind}({cstr(str(p))});"
    result = await run_body(body, wait_frames=8)
    return {**result, "path": str(p)}


@mcp.tool(annotations=WRITE)
async def create_layer(name: str = "", kind: Literal["paint", "fill", "group", "mask", "path", "curve", "text", "decal"] = "paint", parent_id: int = -1) -> dict[str, Any]:
    """Create a paint/fill/group/mask/path/curve/text/decal layer with undo support.

    A mask requires an existing parent layer ID; fill uses the selected material.
    """
    if kind == "mask":
        prefix = await layer_prefix(parent_id)
        state = await rpc("state")
        if next((l for l in state["layers"] if l["id"] == parent_id), {}).get("type") == "mask":
            raise ValueError("A mask parent must be a layer or group, not another mask")
        prefix += 'if (slot_layer_is_mask(l)) { mcp_task_end("A mask cannot parent another mask"); return; }\n'
        body = prefix + "slot_layer_t *created = layers_new_mask(true, l, -1); if (created == NULL) { mcp_task_end(\"Layer limit reached\"); return; } history_new_black_mask();"
    elif kind in ("fill", "decal"):
        body = f"mcp_create_fill_layer({'UV_TYPE_PROJECT' if kind == 'decal' else 'UV_TYPE_UVMAP'}, -1);"
        if name:
            # Functions must be outside main; use a full script with one frame callback.
            code = f"void rename_layer() {{ slot_layer_t *l = script_get_context()->layer; tab_stages_rename_layer(l->name, {cstr(name)}); l->name = string_copy({cstr(name)}); }}\nvoid main() {{ mcp_create_fill_layer({'UV_TYPE_PROJECT' if kind == 'decal' else 'UV_TYPE_UVMAP'}, -1); script_notify_on_next_frame(rename_layer); }}"
            result = await execute_code(code)
        else:
            result = await run_body(body)
        return {**result, "state": await rpc("state")}
    else:
        expr = {"paint": "layers_new_layer(true, -1, NULL)", "group": "layers_new_group()", "path": "layers_new_path_layer(false)", "curve": "layers_new_path_layer(true)", "text": "layers_new_text_layer()"}[kind]
        hist = "history_new_group" if kind == "group" else "history_new_layer"
        body = f"slot_layer_t *created = {expr}; if (created == NULL) {{ mcp_task_end(\"Layer limit reached\"); return; }} {hist}();"
    if name:
        body += f" tab_stages_rename_layer(created->name, {cstr(name)}); created->name = string_copy({cstr(name)});"
    result = await run_body(body)
    return {**result, "state": await rpc("state")}


@mcp.tool(annotations=DESTRUCTIVE)
async def layer_operation(layer_id: int, action: Literal["select", "delete", "duplicate", "clear", "merge_down", "to_fill", "to_paint", "invert_mask", "apply_mask"]) -> dict[str, Any]:
    """Operate on a layer by its live ID, using application history where supported."""
    prefix = await layer_prefix(layer_id)
    bodies = {
        "select": "",
        "delete": "if (!mcp_can_delete_layer(l)) { mcp_task_end(\"Cannot delete the last paint layer\"); return; } tab_layers_delete_layer(l);",
        "duplicate": "history_duplicate_layer(); layers_duplicate_layer(l);",
        "clear": "history_clear_layer(); slot_layer_clear(l, 0x00000000, NULL, 1.0, 0.0, 0.0);",
        "merge_down": 'if (!mcp_can_merge_layer(l)) { mcp_task_end("Layer cannot merge down"); return; } history_merge_layers(); layers_merge_down(); if (script_get_context()->layer->fill_material != NULL) slot_layer_to_paint_layer(script_get_context()->layer);',
        "to_fill": "history_to_fill_layer(); slot_layer_to_fill_layer(l);",
        "to_paint": "history_to_paint_layer(); slot_layer_to_paint_layer(l);",
        "invert_mask": "history_invert_mask(); slot_layer_invert_mask(l);",
        "apply_mask": "history_apply_mask(); slot_layer_apply_mask(l);",
    }
    state = await rpc("state")
    current = next((l for l in state["layers"] if l["id"] == layer_id), None)
    if current is None:
        raise ValueError("Layer no longer exists; query state for current IDs")
    if action in ("invert_mask", "apply_mask") and current["type"] != "mask":
        raise ValueError("This action requires a mask")
    if action in ("clear", "to_fill", "to_paint", "merge_down") and current["type"] == "group":
        raise ValueError("This action requires a paint layer or mask")
    if action == "merge_down" and state["layers"][0]["id"] == layer_id:
        raise ValueError("Bottom layer cannot be merged down")
    result = await run_body(prefix + bodies[action], wait_frames=6)
    return {**result, "state": await rpc("state")}


@mcp.tool(annotations=WRITE)
async def set_layer_properties(layer_id: int, name: str | None = None, opacity: float | None = None, visible: bool | None = None, blending: int | None = None) -> dict[str, Any]:
    """Rename a layer or change its opacity, visibility and blend mode with history."""
    body = await layer_prefix(layer_id)
    if name is not None:
        body += f"history_layer_name(l, l->name); tab_stages_rename_layer(l->name, {cstr(name)}); l->name = string_copy({cstr(name)});\n"
    if opacity is not None:
        body += f"history_layer_opacity(); l->mask_opacity = {unit(opacity, 'opacity')};\n"
    if visible is not None:
        body += f"history_layer_visible(l); l->visible = {number(visible)};\n"
    if blending is not None:
        if not 0 <= blending <= 17:
            raise ValueError("Query BLEND_TYPE_ constants for supported blend modes")
        body += f"history_layer_blending(); l->blending = {int(blending)};\n"
    body += "script_get_context()->ddirty = 2; base_redraw_ui();"
    return await run_body(body)


@mcp.tool(annotations=WRITE)
async def create_material(name: str, base_color: list[float] = [0.5, 0.5, 0.5, 1], roughness: float = 0.5, metallic: float = 0) -> dict[str, Any]:
    """Create and select a PBR material with color, roughness and metallic parameters."""
    if len(base_color) != 4:
        raise ValueError("base_color requires RGBA with four values")
    rgba = ", ".join(unit(v, "base_color") for v in base_color)
    body = (
        f"slot_material_t *m = script_material_create({cstr(name)}); if (m == NULL) {{ mcp_task_end(\"Material creation failed\"); return; }} "
        "ui_node_t *n = script_material_get_node(\"OUTPUT_MATERIAL_PBR\"); "
        "if (n == NULL) { mcp_task_end(\"PBR output node missing\"); return; } "
        "script_material_disconnect(n, 0); "
        f"script_material_set_color(n, true, 0, {rgba}); "
        f"script_material_set_float(n, true, 3, {unit(roughness, 'roughness')}); "
        f"script_material_set_float(n, true, 4, {unit(metallic, 'metallic')}); script_material_update();"
    )
    result = await run_body(body, wait_frames=6)
    return {**result, "state": await rpc("state")}


@mcp.tool(annotations=WRITE)
async def material_node_operation(action: Literal["create", "remove", "connect", "disconnect", "float", "color", "vector", "button", "text"], node_id: int = -1, node_type: str = "", socket: int = 0, is_input: bool = True, values: list[float] = [], text: str = "", from_node_id: int = -1, from_socket: int = 0) -> dict[str, Any]:
    """Edit the selected material node graph. Query get_node_reference for socket/button indices.

    create returns the new node ID in the console log. float/button use one value,
    color uses RGBA, vector uses XYZ; connect uses from_node_id/from_socket → node_id/socket.
    """
    if socket < 0 or from_socket < 0:
        raise ValueError("Socket indices must be nonnegative")
    graph = await get_node_graph()
    nodes = {n["id"]: n for n in graph["nodes"]}
    if action != "create":
        if node_id not in nodes:
            raise ValueError("Node not found")
        node = nodes[node_id]
        if action == "remove" and node["type"] == "OUTPUT_MATERIAL_PBR":
            raise ValueError("Cannot remove PBR output")
        if action in ("button", "text") and socket >= len(node["buttons"]):
            raise ValueError("Button index out of range")
        if action == "button":
            button = node["buttons"][socket]
            if button["type"] not in ("VALUE", "BOOL", "ENUM") or not button.get("default_value"):
                raise ValueError("Button does not hold a numeric value")
        if action == "text":
            button = node["buttons"][socket]
            if button["type"] != "STRING" and node["type"] != "SHADER_GPU":
                raise ValueError("Button does not hold editable text")
        if action in ("float", "color", "vector", "disconnect", "connect"):
            sockets = node["inputs"] if is_input or action in ("connect", "disconnect") else node["outputs"]
            if socket >= len(sockets):
                raise ValueError("Socket index out of range")
            if action in ("float", "color", "vector"):
                target = sockets[socket]
                if target["type"] != {"float": "VALUE", "color": "RGBA", "vector": "VECTOR"}[action] or len(target.get("default_value", [])) < {"float": 1, "color": 3, "vector": 3}[action]:
                    raise ValueError("Socket does not hold the requested value type")
        if action == "connect" and (from_node_id not in nodes or from_socket >= len(nodes[from_node_id]["outputs"])):
            raise ValueError("Source node/socket not found")
    if action == "create":
        if not node_type:
            raise ValueError("node_type is required")
        body = f"ui_node_t *n = script_material_create_node({cstr(node_type)}); if (n == NULL) {{ mcp_task_end(\"Unknown node type\"); return; }} console_log(string(\"node_id=%d\", n->id));"
    else:
        body = f"ui_node_t *n = script_material_get_node_id({int(node_id)}); if (n == NULL) {{ mcp_task_end(\"Node not found\"); return; }} "
        expected = {"float": 1, "button": 1, "color": 4, "vector": 3}
        if action in expected and len(values) != expected[action]:
            raise ValueError(f"{action} needs {expected[action]} values")
        val = ", ".join(number(v) for v in values)
        inp = number(is_input)
        if action == "connect":
            body += f"ui_node_t *from = script_material_get_node_id({int(from_node_id)}); if (from == NULL) {{ mcp_task_end(\"Source node not found\"); return; }} script_material_connect(from, {from_socket}, n, {socket});"
        elif action == "disconnect":
            body += f"script_material_disconnect(n, {socket});"
        elif action == "remove":
            body += "if (string_equals(n->type, \"OUTPUT_MATERIAL_PBR\")) { mcp_task_end(\"Cannot remove PBR output\"); return; } script_material_remove_node(n);"
        elif action in ("float", "color", "vector"):
            body += f"script_material_set_{action}(n, {inp}, {socket}, {val});"
        elif action == "button":
            body += f"script_material_set_button(n, {socket}, {val});"
        else:
            body += f"script_material_set_text(n, {socket}, {cstr(text)});"
    return await run_body("history_edit_nodes(script_get_context()->material->canvas, 0, -1);\n" + body + " script_material_update();", wait_frames=6)


@mcp.tool(annotations=DESTRUCTIVE)
async def object_operation(name: str, action: Literal["select", "duplicate", "delete", "rename", "transform", "material", "visibility"], new_name: str = "", location: list[float] | None = None, rotation: list[float] | None = None, scale: list[float] | None = None, material_name: str = "", visible: bool = True) -> dict[str, Any]:
    """Select/duplicate/remove/rename/transform objects or assign an existing material.

    rotation is Euler XYZ in radians; location/scale are XYZ vectors. Selection
    accepts mesh objects from application state. Transform updates geometry state.
    """
    body = await object_prefix(name)
    if action == "select":
        body += "context_select_paint_object(o->ext);"
    elif action == "duplicate":
        body += "script_object_duplicate(o);"
    elif action == "delete":
        state = await rpc("state")
        if len(state["objects"]) <= 1:
            raise ValueError("Cannot remove the last mesh object")
        body += "script_object_remove(o);"
    elif action == "rename":
        if not new_name:
            raise ValueError("new_name is required")
        body += f"script_object_set_name(o, {cstr(new_name)});"
    elif action == "visibility":
        body += f"o->visible = {number(visible)};"
    elif action == "material":
        state = await rpc("state")
        if material_name not in [m["name"] for m in state["materials"]]:
            raise ValueError("Material not found")
        body += f"script_object_set_material(o, script_get_material({cstr(material_name)}));"
    else:
        for label, vec in (("location", location), ("rotation", rotation), ("scale", scale)):
            if vec is not None and len(vec) != 3:
                raise ValueError(f"{label} requires XYZ")
        body += "transform_t *t = o->transform;"
        if location is not None:
            body += " " + " ".join(f"t->loc.{axis} = {number(v)};" for axis, v in zip("xyz", location))
        if scale is not None:
            body += " " + " ".join(f"t->scale.{axis} = {number(v)};" for axis, v in zip("xyz", scale))
        if rotation is not None:
            body += f" t->rot = quat_from_euler({', '.join(number(v) for v in rotation)});"
        body += " transform_build_matrix(t); util_mesh_transform_changed();"
    result = await run_body(body, wait_frames=6)
    return {**result, "state": await rpc("state")}


@mcp.tool(annotations=WRITE)
async def add_shape(name: str) -> dict[str, Any]:
    """Add a bundled shape. Query script_shape_list() or API reference for available shapes."""
    return await run_body(f"if (script_shape_add({cstr(name)}) == NULL) mcp_task_end(\"Shape not found\");", wait_frames=8)


@mcp.tool(annotations=WRITE)
async def set_brush(tool: int = 0, radius: float = 0.5, opacity: float = 1, hardness: float = 1, brush_id: int = -1, xray: bool = False, symmetry_x: bool = False, symmetry_y: bool = False, symmetry_z: bool = False) -> dict[str, Any]:
    """Select a tool/brush and configure painting. Query TOOL_TYPE_ enums for numeric tool IDs."""
    if not 0 <= tool <= 12 or not 0 < radius <= 10:
        raise ValueError("tool must be 0–12 and radius must be >0 and <=10")
    body = f"context_select_tool({tool});"
    if brush_id >= 0:
        state = await rpc("state")
        if brush_id not in [b["id"] for b in state["brushes"]]:
            raise ValueError("Brush not found")
        body += f" context_set_brush(mcp_get_brush({brush_id}));"
    for name, value in {"brush_radius": radius, "brush_opacity": float(unit(opacity, "opacity")), "brush_hardness": float(unit(hardness, "hardness")), "xray": xray, "sym_x": symmetry_x, "sym_y": symmetry_y, "sym_z": symmetry_z}.items():
        body += f" mcp_set_setting(\"context_t\", {cstr(name)}, {number(value)});"
    return await run_body(body)


@mcp.tool(annotations=WRITE)
async def paint_stroke(points: list[list[float]], space: Literal["viewport", "world"] = "viewport", layer_id: int = -1) -> dict[str, Any]:
    """Paint a stroke, one point per frame, on the selected/explicit paint layer.

    viewport points are normalized XY (0–1) in the 3D viewport; world points are
    XYZ. Configure brush/material first. Returns only after the stroke finishes.
    """
    if not points or len(points) > 2000:
        raise ValueError("Provide 1–2000 points")
    dim = 2 if space == "viewport" else 3
    for p in points:
        if len(p) != dim:
            raise ValueError(f"Points in {space} space need {dim} coordinates")
        for v in p:
            number(v)
            if space == "viewport":
                unit(v, "viewport coordinate")
    prefix = await layer_prefix(layer_id) if layer_id >= 0 else ""
    state = await rpc("state")
    active_id = layer_id if layer_id >= 0 else state["selected_layer_id"]
    active = next((l for l in state["layers"] if l["id"] == active_id), None)
    if active is None or active["type"] == "group" or active["fill_material_id"] >= 0:
        raise ValueError("Painting requires a paint layer or mask; convert the fill layer to paint or create a paint layer first")
    flat = ", ".join(number(v) for p in points for v in p)
    fn = "script_paint" if dim == 2 else "script_paint_world"
    coords = ", ".join(f"pts[index * {dim} + {i}]" for i in range(dim))
    code = f"float pts[{len(points) * dim}] = {{{flat}}}; int index = 0;\nvoid tick() {{ if (index >= {len(points)}) {{ script_paint_end(); script_stop(); mcp_task_end(NULL); return; }} {fn}({coords}); index++; }}\nvoid main() {{ {prefix} mcp_task_begin(); script_notify_on_update(tick); }}"
    return await execute_code(code, timeout=min(300, max(60, len(points) / 10)), retain_context=True)


@mcp.tool(annotations=WRITE)
async def fill_layer(layer_id: int = -1) -> dict[str, Any]:
    """Fill a paint layer with the selected material, using the application's fill operation."""
    prefix = await layer_prefix(layer_id) if layer_id >= 0 else ""
    state = await rpc("state")
    active_id = layer_id if layer_id >= 0 else state["selected_layer_id"]
    active = next((l for l in state["layers"] if l["id"] == active_id), None)
    if active is None:
        raise ValueError("No selected layer; query state for current IDs")
    if active["type"] == "group":
        raise ValueError("Cannot fill a layer group")
    return await run_body(prefix + "script_fill_layer();", wait_frames=8)


@mcp.tool(annotations=WRITE)
async def bake_lightmap(object_name: str, path: str, resolution: int = 1024, samples: int = 64, range: float = 1, timeout: float = 300) -> dict[str, Any]:
    """Bake an object's lightmap and wait for the native completion callback.

    Requires the app's raytracing backend. Writes to an absolute output path;
    timeout cannot interrupt native GPU baking. Check the result before retrying.
    Other bake types are available through Bake Texture nodes and execute_code.
    """
    p = path_arg(path)
    if resolution not in (256, 512, 1024, 2048, 4096) or not 1 <= samples <= 4096 or range <= 0:
        raise ValueError("Invalid baking resolution, sample count or range")
    prefix = await object_prefix(object_name)
    code = f"void done() {{ mcp_task_end(NULL); }}\nvoid main() {{ {prefix} mcp_task_begin(); script_bake_lightmap(o, {resolution}, {samples}, {number(range)}, {cstr(str(p))}, done); }}"
    result = await execute_code(code, timeout=timeout)
    if not p.is_file() or p.stat().st_size == 0:
        raise BridgeError("Bake completed without an output file", result)
    return {**result, "path": str(p)}


@mcp.tool(annotations=WRITE)
async def camera_operation(action: Literal["orbit", "zoom", "view", "reset", "projection"], values: list[float] = []) -> dict[str, Any]:
    """Change viewport camera. orbit takes XY radians; zoom one amount; view six values
    x,y,z,rx,ry,rz; projection one CAMERA_TYPE_ enum; reset takes no values.
    """
    arity = {"orbit": 2, "zoom": 1, "view": 6, "reset": 0, "projection": 1}[action]
    if len(values) != arity:
        raise ValueError(f"{action} needs {arity} values")
    if action == "projection" and (isinstance(values[0], bool) or values[0] not in (0, 1)):
        raise ValueError("projection requires CAMERA_TYPE_PERSPECTIVE (0) or CAMERA_TYPE_ORTHOGRAPHIC (1)")
    func = {"orbit": "viewport_orbit", "zoom": "viewport_zoom", "view": "viewport_set_view", "reset": "viewport_reset", "projection": "viewport_update_camera_type"}[action]
    return await run_body(f"{func}({', '.join(number(v) for v in values)});")


@mcp.tool(annotations=WRITE)
async def undo_redo(action: Literal["undo", "redo"], steps: int = 1) -> dict[str, Any]:
    """Use the application's undo/redo history. Raw scripts only record history if they request it."""
    if not 1 <= steps <= 32:
        raise ValueError("steps must be 1–32")
    # Each history step may schedule its own frame callback. Do not issue all at once.
    result = {}
    for _ in range(steps):
        result = await run_body(f"history_{action}();", wait_frames=6)
    return {**result, "state": await rpc("state")}


@mcp.tool(annotations=READ)
async def get_screenshot() -> Image:
    """Capture the actual ArmorPaint window and return a PNG for visual inspection."""
    with tempfile.TemporaryDirectory(prefix="armorpaint-mcp-") as directory:
        p = Path(directory) / "screenshot.png"
        await rpc("screenshot", path=str(p), timeout=30)
        if not p.is_file():
            raise BridgeError("Screenshot callback completed without an output file")
        return Image(data=p.read_bytes(), format="png")


@mcp.tool(annotations=WRITE)
async def player_operation(action: Literal["start", "stop"]) -> dict[str, Any]:
    """Start project playback or stop it and restore the editor's captured project.

    start returns when playback has started, rather than waiting for game scripts
    to finish. Use stop to restore editing before sending further mutations.
    """
    result = await rpc("player_" + action)
    return {**result, "state": await rpc("state")}


@mcp.resource("armorpaint://state")
async def state_resource() -> str:
    """Live project state."""
    return json.dumps(await rpc("state"), ensure_ascii=False)


@mcp.resource("armorpaint://api")
async def api_resource() -> str:
    """Complete API reference from the running build."""
    return await rpc("api")


@mcp.resource("armorpaint://reference")
async def reference_resource() -> str:
    """API, node reference, project data and scripting guidance."""
    return await rpc("reference")


@mcp.resource("armorpaint://nodes")
async def nodes_resource() -> str:
    """Material node types and sockets."""
    return await rpc("nodes")


def main():
    global bridge
    parser = argparse.ArgumentParser(description="ArmorPaint MCP stdio server")
    parser.add_argument("--socket", help="Absolute path to the running app's MCP socket")
    parser.add_argument("--check", action="store_true", help="Check the native bridge and print project state")
    args = parser.parse_args()
    bridge = Bridge(args.socket)
    if args.check:
        print(json.dumps(bridge.request("state"), ensure_ascii=False, indent=2))
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
