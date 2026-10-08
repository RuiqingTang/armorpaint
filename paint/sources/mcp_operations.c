#include "global.h"
#include <math.h>

static i32 mcp_pending_tasks = 0;
static char *mcp_task_error = NULL;

// A script may keep its RPC open while native baking/timer callbacks finish.
void mcp_task_begin(void) { ++mcp_pending_tasks; }
void mcp_task_end(char *error) {
	if (mcp_pending_tasks > 0) --mcp_pending_tasks;
	if (error != NULL && error[0] != '\0') mcp_task_error = string_copy(error);
}

void mcp_create_fill_layer(i32 uv_type, i32 position) {
	layers_create_fill_layer(uv_type, mat4_nan(), position);
}

bool mcp_can_delete_layer(slot_layer_t *layer) {
	return tab_layers_can_delete(layer);
}

void mcp_export_textures(char *path, export_preset_t *preset, i32 bits, i32 format) {
	if (path == NULL || preset == NULL) { mcp_task_end("Invalid export path or preset"); return; }
	gpu_texture_t *current;
	bool in_use;
	script_gpu_begin(&current, &in_use);
	i32 old_bits = base_bits;
	i32 old_format = g_context->format_type;
	export_preset_t *old_preset = box_export_preset;
	base_bits = bits;
	g_context->format_type = format;
	box_export_preset = preset;
	if (layers_temp_image != NULL) {
		gpu_delete_texture(layers_temp_image); layers_temp_image = NULL;
		map_delete(render_path_render_targets, "temptex0");
	}
	// Export buffers must match the requested bit depth even when the project
	// layers remain 8-bit. GPU sampling converts inputs during compositing.
	if (layers_expa != NULL) {
		gpu_delete_texture(layers_expa); gpu_delete_texture(layers_expb); gpu_delete_texture(layers_expc);
		layers_expa = NULL; layers_expb = NULL; layers_expc = NULL;
		map_delete(render_path_render_targets, "expa"); map_delete(render_path_render_targets, "expb"); map_delete(render_path_render_targets, "expc");
	}
	export_texture_run(path, false);
	base_bits = old_bits;
	g_context->format_type = old_format;
	box_export_preset = old_preset;
	script_gpu_end(current, in_use);
}

slot_layer_t *mcp_get_layer(i32 id) {
	for (i32 i = 0; i < g_project->_->layers->length; ++i) {
		slot_layer_t *l = g_project->_->layers->buffer[i];
		if (l->id == id) return l;
	}
	return NULL;
}

slot_brush_t *mcp_get_brush(i32 id) {
	for (i32 i = 0; i < g_project->_->brushes->length; ++i) {
		slot_brush_t *b = g_project->_->brushes->buffer[i];
		if (b->id == id) return b;
	}
	return NULL;
}

asset_t *mcp_get_asset(i32 index) {
	return index >= 0 && index < g_project->_->assets->length ? g_project->_->assets->buffer[index] : NULL;
}

bool mcp_set_setting(char *scope, char *name, f64 value) {
	if (scope == NULL || name == NULL || !isfinite(value)) return false;
#define SETTING_b(target, field) target->field = value != 0
#define SETTING_i(target, field) target->field = (i32)value
#define SETTING_f(target, field) target->field = (f32)value
#define SETTING_d(target, field) target->field = value
#define TARGET_context_t g_context
#define TARGET_config_t g_config
#define S(type, field, kind) \
	if (string_equals(scope, #type) && string_equals(name, #field)) { \
		SETTING_##kind(TARGET_##type, field); \
		g_context->ddirty = 2; base_redraw_ui(); return true; \
	}
#include "mcp_settings.h"
#undef S
#undef TARGET_context_t
#undef TARGET_config_t
#undef SETTING_b
#undef SETTING_i
#undef SETTING_f
#undef SETTING_d
	return false;
}
