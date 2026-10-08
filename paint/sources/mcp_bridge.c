// Local, newline-delimited JSON RPC bridge. MCP itself runs in the companion
// Python process; all application calls execute on ArmorPaint's main thread.
#include "global.h"
#include <math.h>

#if defined(IRON_MACOS) || defined(IRON_LINUX)
#include <errno.h>
#include <fcntl.h>
#define JSMN_HEADER
#include <jsmn.h>
#undef JSMN_HEADER
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>

#define MCP_PEERS 4
#define MCP_MAX_REQUEST (1024 * 1024)
#define MCP_RETAINED_CONTEXTS 256

typedef struct {
	int fd;
	char *input;
	int input_len;
	char *output;
	int output_len;
	int output_sent;
	char *id;
	bool waiting;
	bool wait_for_scripts;
	bool check_errors;
	bool stopping_player;
	bool retain_context;
	minic_ctx_t *ctx;
	int frames;
	double started;
	double timeout;
} mcp_peer_t;

static int mcp_listener = -1;
static char *mcp_socket_path = NULL;
static mcp_peer_t mcp_peers[MCP_PEERS];
static int mcp_active_peer = -1;
static bool mcp_screenshot_done = false;
static bool mcp_timed_out = false;
static minic_ctx_t *mcp_contexts[MCP_RETAINED_CONTEXTS];
static int mcp_context_count = 0;
static bool mcp_native_executing = false;

// The engine encoder expects already escaped strings. Escape locally so this
// bridge can carry code, API headers, Unicode names and multiline console logs.
static char *mcp_escape(const char *s) {
	if (s == NULL) s = "";
	char *out = malloc(strlen(s) * 6 + 1); int w = 0;
	for (const unsigned char *p = (const unsigned char *)s; *p; ++p) {
		if (*p == '"' || *p == '\\') { out[w++] = '\\'; out[w++] = *p; }
		else if (*p < 32) { snprintf(out + w, 7, "\\u%04x", *p); w += 6; }
		else out[w++] = *p;
	}
	out[w] = 0; return out;
}

static void mcp_encode_string(char *key, char *value) {
	char *escaped = mcp_escape(value);
	json_encode_string(key, escaped); free(escaped);
}
#define json_encode_string mcp_encode_string

// Do not use the engine's JSON decoder for requests: code needs full JSON escape
// decoding, including backslashes, newlines, Unicode and surrogate pairs.
static int mcp_hex4(const char *p) {
	int v = 0;
	for (int i = 0; i < 4; ++i) {
		char c = p[i];
		int n = c >= '0' && c <= '9' ? c - '0' : c >= 'a' && c <= 'f' ? c - 'a' + 10 : c >= 'A' && c <= 'F' ? c - 'A' + 10 : -1;
		if (n < 0) return -1;
		v = v * 16 + n;
	}
	return v;
}

static char *mcp_json_string(const char *json, jsmntok_t *t) {
	if (t == NULL || t->type != JSMN_STRING) return NULL;
	char *out = malloc(t->end - t->start + 1);
	int w = 0;
	for (int i = t->start; i < t->end; ++i) {
		unsigned char c = json[i];
		if (c != '\\') { if (c < 32) goto invalid; out[w++] = c; continue; }
		if (++i >= t->end) goto invalid;
		c = json[i];
		if (c == 'n') out[w++] = '\n';
		else if (c == 'r') out[w++] = '\r';
		else if (c == 't') out[w++] = '\t';
		else if (c == 'b') out[w++] = '\b';
		else if (c == 'f') out[w++] = '\f';
		else if (c == '"' || c == '\\' || c == '/') out[w++] = c;
		else if (c == 'u') {
			if (i + 4 >= t->end) goto invalid;
			int cp = mcp_hex4(json + i + 1); i += 4;
			if (cp <= 0) goto invalid;
			if (cp >= 0xd800 && cp <= 0xdbff) {
				if (i + 6 >= t->end || json[i + 1] != '\\' || json[i + 2] != 'u') goto invalid;
				int low = mcp_hex4(json + i + 3);
				if (low < 0xdc00 || low > 0xdfff) goto invalid;
				cp = 0x10000 + ((cp - 0xd800) << 10) + low - 0xdc00; i += 6;
			}
			else if (cp >= 0xdc00 && cp <= 0xdfff) goto invalid;
			if (cp < 0x80) out[w++] = cp;
			else if (cp < 0x800) { out[w++] = 0xc0 | (cp >> 6); out[w++] = 0x80 | (cp & 63); }
			else if (cp < 0x10000) { out[w++] = 0xe0 | (cp >> 12); out[w++] = 0x80 | ((cp >> 6) & 63); out[w++] = 0x80 | (cp & 63); }
			else { out[w++] = 0xf0 | (cp >> 18); out[w++] = 0x80 | ((cp >> 12) & 63); out[w++] = 0x80 | ((cp >> 6) & 63); out[w++] = 0x80 | (cp & 63); }
		}
		else goto invalid;
	}
	out[w] = 0;
	return out;
invalid:
	free(out); return NULL;
}

static jsmntok_t *mcp_field(char *s, jsmntok_t *t, int count, const char *name) {
	for (int i = 1; i + 1 < count;) {
		jsmntok_t *key = &t[i], *value = &t[i + 1];
		if (key->type != JSMN_STRING) return NULL;
		if (key->end - key->start == strlen(name) && strncmp(s + key->start, name, strlen(name)) == 0) return value;
		int end = value->end;
		i += 2;
		while (i < count && t[i].start < end) ++i;
	}
	return NULL;
}

static double mcp_number(char *s, jsmntok_t *t, double fallback) {
	if (t == NULL || t->type != JSMN_PRIMITIVE) return fallback;
	char *end;
	double v = strtod(s + t->start, &end);
	return end == s + t->end && isfinite(v) ? v : fallback;
}

static void mcp_response(mcp_peer_t *p, bool ok, char *error, char *data) {
	json_encode_begin();
	json_encode_string("id", p->id == NULL ? "" : p->id);
	json_encode_bool("ok", ok);
	if (error != NULL) json_encode_string("error", error);
	if (data != NULL) json_encode_string("data", data);
	char *json = json_encode_end();
	if (strlen(json) >= 16 * 1024 * 1024) { mcp_response(p, false, "Response too large; changes may already have been applied", NULL); return; }
	p->output_len = (int)strlen(json) + 1;
	p->output = malloc(p->output_len + 1);
	snprintf(p->output, p->output_len + 1, "%s\n", json);
	p->output_sent = 0;
	p->waiting = false;
}

static char *mcp_state(void) {
	minic_register_builtins();
	json_encode_begin();
	json_encode_string("application", "ArmorPaint");
	json_encode_string("version", manifest_version);
	json_encode_i32("bridge_version", 1);
	json_encode_i32("registered_functions", minic_ext_func_count_get());
	json_encode_i32("registered_structs", minic_struct_count);
	json_encode_string("project_path", g_project->_->filepath);
	json_encode_bool("busy", mcp_active_peer >= 0);
	json_encode_bool("player_running", player_in_editor);
	json_encode_i32("retained_contexts", mcp_context_count);
	json_encode_i32("texture_width", config_get_texture_res_x());
	json_encode_i32("texture_height", config_get_texture_res_y());
	json_encode_i32("workspace", g_config->workspace);
	json_encode_i32("workflow", g_config->workflow);
	json_encode_i32("tool", g_context->tool);
	json_encode_i32("selected_layer_id", g_context->layer == NULL ? -1 : g_context->layer->id);
	json_encode_i32("selected_material_id", g_context->material == NULL ? -1 : g_context->material->id);
	json_encode_i32("selected_brush_id", g_context->brush == NULL ? -1 : g_context->brush->id);
	json_encode_string("selected_object", g_context->paint_object == NULL ? "" : g_context->paint_object->base->name);
	json_encode_begin_array("objects");
	for (int i = 0; i < g_project->_->paint_objects->length; ++i) {
		mesh_object_t *o = g_project->_->paint_objects->buffer[i];
		json_encode_begin_object();
		json_encode_string("name", o->base->name);
		json_encode_bool("visible", o->base->visible);
		transform_t *tr = o->base->transform;
		json_encode_f32_array("location", f32_array_create_from_raw((f32[]){tr->loc.x, tr->loc.y, tr->loc.z}, 3));
		json_encode_f32_array("scale", f32_array_create_from_raw((f32[]){tr->scale.x, tr->scale.y, tr->scale.z}, 3));
		json_encode_f32_array("rotation_quaternion", f32_array_create_from_raw((f32[]){tr->rot.x, tr->rot.y, tr->rot.z, tr->rot.w}, 4));
		json_encode_end_object();
	}
	json_encode_end_array();
	json_encode_begin_array("layers");
	for (int i = 0; i < g_project->_->layers->length; ++i) {
		slot_layer_t *l = g_project->_->layers->buffer[i];
		json_encode_begin_object();
		json_encode_i32("id", l->id);
		json_encode_string("name", l->name);
		json_encode_string("type", slot_layer_is_group(l) ? "group" : slot_layer_is_mask(l) ? "mask" : "layer");
		json_encode_bool("visible", l->visible);
		json_encode_f32("opacity", l->mask_opacity);
		json_encode_i32("blending", l->blending);
		json_encode_i32("parent_id", l->parent == NULL ? -1 : l->parent->id);
		json_encode_i32("fill_material_id", l->fill_material == NULL ? -1 : l->fill_material->id);
		json_encode_end_object();
	}
	json_encode_end_array();
	json_encode_begin_array("materials");
	for (int i = 0; i < g_project->_->materials->length; ++i) {
		slot_material_t *m = g_project->_->materials->buffer[i];
		json_encode_begin_object(); json_encode_i32("id", m->id); json_encode_string("name", m->canvas->name);
		json_encode_i32("node_count", m->canvas->nodes->length); json_encode_end_object();
	}
	json_encode_end_array();
	json_encode_begin_array("brushes");
	for (int i = 0; i < g_project->_->brushes->length; ++i) {
		slot_brush_t *b = g_project->_->brushes->buffer[i];
		json_encode_begin_object(); json_encode_i32("id", b->id); json_encode_string("name", b->canvas->name); json_encode_end_object();
	}
	json_encode_end_array();
	json_encode_begin_array("assets");
	for (int i = 0; i < g_project->_->assets->length; ++i) {
		asset_t *a = g_project->_->assets->buffer[i];
		json_encode_begin_object(); json_encode_i32("index", i); json_encode_string("name", a->name); json_encode_string("path", a->file); json_encode_end_object();
	}
	json_encode_end_array();
	json_encode_begin_array("scripts");
	if (g_project->script_names != NULL) for (int i = 0; i < g_project->script_names->length; ++i) {
		json_encode_begin_object(); json_encode_string("name", g_project->script_names->buffer[i]); json_encode_end_object();
	}
	json_encode_end_array();
	return json_encode_end();
}

static char *mcp_settings(void) {
	json_encode_begin();
#define VALUE_b(v) json_encode_bool("value", v)
#define VALUE_i(v) json_encode_i32("value", v)
#define VALUE_f(v) json_encode_f32("value", v)
#define VALUE_d(v) json_encode_f32("value", v)
#define TARGET_context_t g_context
#define TARGET_config_t g_config
	json_encode_begin_array("settings");
#define S(type, field, kind) \
	json_encode_begin_object(); json_encode_string("scope", #type); json_encode_string("name", #field); \
	json_encode_string("type", #kind); VALUE_##kind(TARGET_##type->field); json_encode_end_object();
#include "mcp_settings.h"
#undef S
#undef TARGET_context_t
#undef TARGET_config_t
#undef VALUE_b
#undef VALUE_i
#undef VALUE_f
#undef VALUE_d
	json_encode_end_array(); return json_encode_end();
}

static void mcp_screenshot_finished(void) { mcp_screenshot_done = true; }

static void mcp_graph_sockets(char *key, ui_node_socket_array_t *sockets) {
	json_encode_begin_array(key);
	for (int i = 0; i < sockets->length; ++i) {
		ui_node_socket_t *s = sockets->buffer[i];
		json_encode_begin_object(); json_encode_i32("index", i);
		json_encode_string("name", s->name); json_encode_string("type", s->type);
		if (s->default_value != NULL) json_encode_f32_array("default_value", s->default_value);
		json_encode_end_object();
	}
	json_encode_end_array();
}

static char *mcp_graph(ui_node_canvas_t *canvas) {
	json_encode_begin(); json_encode_string("name", canvas->name);
	json_encode_begin_array("nodes");
	for (int i = 0; i < canvas->nodes->length; ++i) {
		ui_node_t *n = canvas->nodes->buffer[i];
		json_encode_begin_object(); json_encode_i32("id", n->id);
		json_encode_string("name", n->name); json_encode_string("type", n->type);
		json_encode_f32("x", n->x); json_encode_f32("y", n->y);
		mcp_graph_sockets("inputs", n->inputs); mcp_graph_sockets("outputs", n->outputs);
		json_encode_begin_array("buttons");
		for (int j = 0; j < n->buttons->length; ++j) {
			ui_node_button_t *b = n->buttons->buffer[j];
			json_encode_begin_object(); json_encode_i32("index", j); json_encode_string("name", b->name); json_encode_string("type", b->type);
			if (b->default_value != NULL) json_encode_f32_array("default_value", b->default_value);
			if (b->data != NULL) json_encode_string("data", u8_array_to_string(b->data));
			json_encode_end_object();
		}
		json_encode_end_array(); json_encode_end_object();
	}
	json_encode_end_array(); json_encode_begin_array("links");
	for (int i = 0; i < canvas->links->length; ++i) {
		ui_node_link_t *l = canvas->links->buffer[i];
		json_encode_begin_object(); json_encode_i32("id", l->id); json_encode_i32("from_node", l->from_id); json_encode_i32("from_socket", l->from_socket);
		json_encode_i32("to_node", l->to_id); json_encode_i32("to_socket", l->to_socket); json_encode_end_object();
	}
	json_encode_end_array(); return json_encode_end();
}

static void mcp_dispatch(int index) {
	mcp_peer_t *p = &mcp_peers[index];
	jsmntok_t tokens[128]; jsmn_parser parser; jsmn_init(&parser);
	int n = jsmn_parse(&parser, p->input, p->input_len, tokens, 128);
	if (n < 1 || tokens[0].type != JSMN_OBJECT) { mcp_response(p, false, "Invalid JSON object", NULL); return; }
	p->id = mcp_json_string(p->input, mcp_field(p->input, tokens, n, "id"));
	char *method = mcp_json_string(p->input, mcp_field(p->input, tokens, n, "method"));
	if (method == NULL || p->id == NULL) { free(method); mcp_response(p, false, "id and method must be strings", NULL); return; }
	if (string_equals(method, "ping")) mcp_response(p, true, NULL, "ArmorPaint MCP bridge v1");
	else if (string_equals(method, "state")) mcp_response(p, true, NULL, mcp_state());
	else if (string_equals(method, "settings")) mcp_response(p, true, NULL, mcp_settings());
	else if (string_equals(method, "api")) mcp_response(p, true, NULL, minic_api_header_generate());
	else if (string_equals(method, "reference")) mcp_response(p, true, NULL, agent_reference());
	else if (string_equals(method, "nodes")) mcp_response(p, true, NULL, agent_nodes_reference());
	else if (string_equals(method, "material_graph")) mcp_response(p, true, NULL, mcp_graph(g_context->material->canvas));
	else if (string_equals(method, "brush_graph")) mcp_response(p, true, NULL, mcp_graph(g_context->brush->canvas));
	else if (string_equals(method, "player_start") || string_equals(method, "player_stop")) {
		bool start = string_equals(method, "player_start");
		if (mcp_active_peer >= 0 || agent_running || (start && (script_is_running() || player_in_editor))) mcp_response(p, false, "Application is busy", NULL);
		else if (mcp_timed_out) mcp_response(p, false, "Restart ArmorPaint after the timed-out operation", NULL);
		else {
			mcp_active_peer = index; p->waiting = true; p->wait_for_scripts = false;
			p->check_errors = start; p->stopping_player = !start;
			p->frames = 8; p->timeout = 60; p->started = sys_time();
			mcp_screenshot_done = true; mcp_pending_tasks = 0; mcp_task_error = NULL;
			console_capture = ""; minic_error_count = 0;
			mcp_native_executing = true;
			if (start) { minic_set_execution_limit(5000000); player_start(NULL); minic_set_execution_limit(0); }
			else player_stop();
			mcp_native_executing = false;
		}
	}
	else if (!string_equals(method, "execute") && !string_equals(method, "screenshot")) mcp_response(p, false, "Unknown bridge method", NULL);
	else if (mcp_active_peer >= 0 || agent_running || console_capture != NULL || (string_equals(method, "execute") && (script_is_running() || g_config->workspace == WORKSPACE_PLAYER)))
		mcp_response(p, false, "Application is busy running another script, agent or player", NULL);
	else if (mcp_timed_out) mcp_response(p, false, "A previous asynchronous operation timed out. Restart ArmorPaint before sending more mutations.", NULL);
	else {
		p->frames = (int)mcp_number(p->input, mcp_field(p->input, tokens, n, "wait_frames"), 3);
		if (p->frames < 1 || p->frames > 120) p->frames = 3;
		p->timeout = mcp_number(p->input, mcp_field(p->input, tokens, n, "timeout"), 60);
		if (p->timeout < 1 || p->timeout > 300) p->timeout = 60;
		p->started = sys_time();
		p->wait_for_scripts = !string_equals(method, "screenshot");
		mcp_pending_tasks = 0; mcp_task_error = NULL;
		mcp_screenshot_done = !string_equals(method, "screenshot");
		if (!mcp_screenshot_done) {
			char *path = mcp_json_string(p->input, mcp_field(p->input, tokens, n, "path"));
			if (path == NULL || path[0] != '/') mcp_response(p, false, "Screenshot requires an absolute path", NULL);
			else { mcp_active_peer = index; p->waiting = true; console_capture = ""; script_screenshot_queue(path, 2, mcp_screenshot_finished); }
			free(path);
		}
		else {
			char *code = mcp_json_string(p->input, mcp_field(p->input, tokens, n, "code"));
			jsmntok_t *retain = mcp_field(p->input, tokens, n, "retain_context");
			p->retain_context = retain == NULL || strncmp(p->input + retain->start, "false", 5) != 0;
			if (code == NULL || code[0] == 0) mcp_response(p, false, "Code must be a nonempty JSON string", NULL);
			else if (p->retain_context && mcp_context_count == MCP_RETAINED_CONTEXTS) mcp_response(p, false, "Retained script context limit reached; restart ArmorPaint", NULL);
			else {
				mcp_active_peer = index; p->waiting = true; console_capture = ""; minic_error_count = 0;
				p->check_errors = true;
				minic_set_execution_limit(5000000);
				mcp_native_executing = true;
				p->ctx = minic_eval_named(code, "<mcp>");
				mcp_native_executing = false;
				minic_set_execution_limit(0);
				g_context->ddirty = 2; base_redraw_ui();
			}
			free(code);
		}
	}
	free(method);
}

static void mcp_peer_close(mcp_peer_t *p) {
	if (p->fd >= 0) close(p->fd);
	free(p->input); free(p->output); free(p->id);
	memset(p, 0, sizeof(*p)); p->fd = -1;
}

static void mcp_update(void *_) {
	if (mcp_native_executing) return; // Native operations may pump the UI loop.
	iron_delay_idle_sleep();
	for (int i = 0; i < MCP_PEERS; ++i) {
		mcp_peer_t *p = &mcp_peers[i];
		if (p->fd < 0) {
			int fd = accept(mcp_listener, NULL, NULL);
			if (fd < 0) continue;
			fcntl(fd, F_SETFL, O_NONBLOCK);
#ifdef SO_NOSIGPIPE
			int yes = 1; setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &yes, sizeof(yes));
#endif
			p->fd = fd; p->input = malloc(MCP_MAX_REQUEST + 1); p->started = sys_time();
		}
		if (p->waiting) {
			iron_delay_idle_sleep();
			bool timeout = sys_time() - p->started > p->timeout;
			bool failed = p->check_errors && minic_error_count > 0;
			if (!timeout && !failed && (!mcp_screenshot_done || mcp_pending_tasks > 0 || (p->wait_for_scripts && script_is_running()) || (p->stopping_player && player_in_editor) || --p->frames > 0)) continue;
			if (timeout || failed) script_stop();
			if (timeout) mcp_timed_out = true;
			bool ok = !timeout && !failed && mcp_task_error == NULL;
			json_encode_begin();
			json_encode_string("log", console_capture == NULL ? "" : console_capture);
			json_encode_i32("script_errors", p->check_errors ? minic_error_count : 0);
			json_encode_string("status", ok ? "completed" : "failed");
			json_encode_bool("changes_may_have_been_applied", !ok);
			if (p->ctx != NULL) {
				minic_val_t value = minic_ctx_return_val(p->ctx);
				if (value.type == MINIC_T_INT || value.type == MINIC_T_BOOL || value.type == MINIC_T_CHAR) json_encode_i32("return_value", value.i);
				else if (value.type != MINIC_T_PTR && value.type != MINIC_T_EMBED) json_encode_f32("return_value", minic_val_to_d(value));
			}
			mcp_response(p, ok, timeout ? "Operation timed out; partial changes may remain" : mcp_task_error != NULL ? mcp_task_error : failed ? "Script failed; partial changes may remain" : NULL, json_encode_end());
			console_capture = NULL; mcp_active_peer = -1;
			if (p->ctx != NULL) {
				if (p->retain_context || timeout || failed) {
					if (mcp_context_count < MCP_RETAINED_CONTEXTS) mcp_contexts[mcp_context_count++] = p->ctx;
				}
				else minic_ctx_free(p->ctx);
				p->ctx = NULL;
			}
		}
		if (p->output != NULL) {
#ifdef MSG_NOSIGNAL
			ssize_t sent = send(p->fd, p->output + p->output_sent, p->output_len - p->output_sent, MSG_NOSIGNAL);
#else
			ssize_t sent = send(p->fd, p->output + p->output_sent, p->output_len - p->output_sent, 0);
#endif
			if (sent > 0) p->output_sent += (int)sent;
			if (p->output_sent == p->output_len || (sent < 0 && errno != EAGAIN && errno != EWOULDBLOCK)) mcp_peer_close(p);
			continue;
		}
		if (sys_time() - p->started > 10) { mcp_peer_close(p); continue; }
		ssize_t received = recv(p->fd, p->input + p->input_len, MCP_MAX_REQUEST - p->input_len, 0);
		if (received == 0 || (received < 0 && errno != EAGAIN && errno != EWOULDBLOCK)) { mcp_peer_close(p); continue; }
		if (received < 0) continue;
		p->input_len += (int)received; p->input[p->input_len] = 0;
		if (strchr(p->input, '\n') != NULL) mcp_dispatch(i);
		else if (p->input_len == MCP_MAX_REQUEST) mcp_response(p, false, "Request too large", NULL);
	}
}

bool mcp_bridge_enabled(void) { return mcp_listener >= 0; }

void mcp_bridge_start(void) {
	if (mcp_listener >= 0) return;
	char *path = getenv("ARMORPAINT_MCP_SOCKET");
	if (path == NULL || path[0] == 0) path = string("%smcp.sock", iron_internal_save_path());
	for (int i = 1; i + 1 < iron_get_arg_count(); ++i) if (string_equals(iron_get_arg(i), "--mcp-socket")) path = iron_get_arg(i + 1);
	struct sockaddr_un addr = {0}; addr.sun_family = AF_UNIX;
	if (path[0] != '/' || strlen(path) >= sizeof(addr.sun_path)) { console_error("MCP socket path must be absolute and fit sockaddr_un"); return; }
	strcpy(addr.sun_path, path);
	int probe = socket(AF_UNIX, SOCK_STREAM, 0);
	if (probe < 0) { console_error("Could not create MCP socket"); return; }
	if (connect(probe, (struct sockaddr *)&addr, sizeof(addr)) == 0) { close(probe); console_error("Another ArmorPaint instance is serving this MCP socket"); return; }
	close(probe);
	struct stat st;
	if (lstat(path, &st) == 0) {
		if (!S_ISSOCK(st.st_mode) || st.st_uid != getuid()) { console_error("Refusing to replace existing MCP path"); return; }
		unlink(path);
	}
	mcp_listener = socket(AF_UNIX, SOCK_STREAM, 0);
	mode_t previous = umask(0077);
	int bound = mcp_listener < 0 ? -1 : bind(mcp_listener, (struct sockaddr *)&addr, sizeof(addr));
	umask(previous);
	if (bound != 0 || listen(mcp_listener, 8) != 0) {
		if (mcp_listener >= 0) close(mcp_listener);
		mcp_listener = -1; console_error("Could not bind MCP socket"); return;
	}
	chmod(path, 0600); fcntl(mcp_listener, F_SETFL, O_NONBLOCK);
	mcp_socket_path = string_copy(path);
	for (int i = 0; i < MCP_PEERS; ++i) mcp_peers[i].fd = -1;
	sys_notify_on_update(mcp_update, NULL);
	console_info(string("MCP server started: %s", path));
}

void mcp_bridge_stop(void) {
	if (mcp_listener < 0) return;
	if (mcp_active_peer >= 0) { console_error("Wait for the current MCP operation before stopping the server"); return; }
	sys_remove_update(mcp_update);
	for (int i = 0; i < MCP_PEERS; ++i) mcp_peer_close(&mcp_peers[i]);
	close(mcp_listener); mcp_listener = -1;
	unlink(mcp_socket_path); console_info("MCP server stopped");
}

void mcp_bridge_init(void *_) {
	for (int i = 1; i < iron_get_arg_count(); ++i) {
		if (string_equals(iron_get_arg(i), "--mcp") || string_equals(iron_get_arg(i), "--mcp-socket")) { mcp_bridge_start(); return; }
	}
}
#else
bool mcp_bridge_enabled(void) { return false; }
void mcp_bridge_start(void) { console_error("MCP bridge currently requires macOS or Linux"); }
void mcp_bridge_stop(void) {}
void mcp_bridge_init(void *_) {}
#endif

#ifdef json_encode_string
#undef json_encode_string
#endif
