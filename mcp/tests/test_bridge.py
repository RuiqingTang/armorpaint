import json
import os
import socket
import threading
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from armorpaint_mcp import bridge
from armorpaint_mcp.bridge import Bridge, BridgeError


@pytest.fixture
def socket_path():
    # macOS sockaddr_un has a 104-byte path limit; pytest's default temp paths
    # include long test names, so use an isolated short directory for sockets.
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="ap-") as directory:
        yield Path(directory) / "bridge.sock"


def serve_once(path, reply):
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    os.chmod(path, 0o600)
    server.listen(1)
    server.settimeout(5)
    errors = []

    def worker():
        try:
            with server.accept()[0] as peer:
                data = b""
                while b"\n" not in data:
                    part = peer.recv(4096)
                    if not part:
                        raise RuntimeError("Client disconnected before sending a request")
                    data += part
                request = json.loads(data)
                response = reply(request)
                if response is None:
                    return
                output = response if isinstance(response, bytes) else json.dumps(response, ensure_ascii=False).encode() + b"\n"
                for start in range(0, len(output), 7):
                    peer.sendall(output[start:start + 7])
        except Exception as exc:
            errors.append(exc)
        finally:
            server.close()

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    return thread, errors


def test_fragmented_unicode_response(socket_path):
    path = socket_path
    thread, errors = serve_once(path, lambda r: {"id": r["id"], "ok": True, "data": json.dumps({"name": '中文 "material"', "log": "line1\nline2"})})
    assert Bridge(path).request("state") == {"name": '中文 "material"', "log": "line1\nline2"}
    thread.join(5)
    assert not errors


def test_native_failure_preserves_log_and_never_retries(socket_path):
    path = socket_path
    thread, errors = serve_once(path, lambda r: {"id": r["id"], "ok": False, "error": "partial changes", "data": '{"log":"line 4: invalid function"}'})
    with pytest.raises(BridgeError, match="line 4") as exc:
        Bridge(path).request("execute", code="bad code")
    assert exc.value.result["log"] == "line 4: invalid function"
    thread.join(5)
    assert not errors


def test_response_id_is_checked(socket_path):
    path = socket_path
    thread, errors = serve_once(path, lambda r: {"id": "different", "ok": True})
    with pytest.raises(BridgeError, match="ID mismatch"):
        Bridge(path).request("ping")
    thread.join(5)
    assert not errors


def test_missing_bridge_is_actionable(tmp_path):
    with pytest.raises(BridgeError, match="Help.*Start MCP Server"):
        Bridge(tmp_path / "missing.sock").request("ping")


def test_insecure_socket_rejected(socket_path):
    path = socket_path
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(path))
        os.chmod(path, 0o666)
        with pytest.raises(BridgeError, match="mode 0600"):
            Bridge(path).request("ping")


def test_oversized_script_rejected_before_connecting(tmp_path):
    with pytest.raises(BridgeError, match="1 MiB"):
        Bridge(tmp_path / "missing.sock").request("execute", code="x" * (1024 * 1024))


@pytest.mark.parametrize("payload", [{"layers": []}, [1, 2], 42, None, "plain text"])
def test_response_data_types(socket_path, payload):
    thread, errors = serve_once(socket_path, lambda r: {"id": r["id"], "ok": True, "data": payload})
    assert Bridge(socket_path).request("state") == payload
    thread.join(5)
    assert not errors


@pytest.mark.parametrize("ok", ["false", "true", 1, 0, None])
def test_response_requires_boolean_ok(socket_path, ok):
    thread, errors = serve_once(socket_path, lambda r: {"id": r["id"], "ok": ok, "data": {"status": "completed"}})
    with pytest.raises(BridgeError, match="Invalid bridge response"):
        Bridge(socket_path).request("execute", code="void main() {}")
    thread.join(5)
    assert not errors


def test_response_requires_ok_field(socket_path):
    thread, errors = serve_once(socket_path, lambda r: {"id": r["id"], "data": {}})
    with pytest.raises(BridgeError, match="Invalid bridge response"):
        Bridge(socket_path).request("state")
    thread.join(5)
    assert not errors


@pytest.mark.parametrize("error", [None, {"message": "failed"}, 123])
def test_malformed_error_is_reported_as_bridge_error(socket_path, error):
    thread, errors = serve_once(socket_path, lambda r: {"id": r["id"], "ok": False, "error": error})
    with pytest.raises(BridgeError, match="Invalid bridge response"):
        Bridge(socket_path).request("state")
    thread.join(5)
    assert not errors


@pytest.mark.parametrize("response", [b"not json\n", b"\xff\n", b"[]\n", b'{"id":\n'])
def test_malformed_wire_response(socket_path, response):
    thread, errors = serve_once(socket_path, lambda r: response)
    with pytest.raises(BridgeError, match="Invalid bridge response"):
        Bridge(socket_path).request("state")
    thread.join(5)
    assert not errors


def test_premature_disconnect_never_retries(socket_path):
    thread, errors = serve_once(socket_path, lambda r: None)
    with pytest.raises(BridgeError, match="partially run"):
        Bridge(socket_path).request("execute", code="void main() {}")
    thread.join(5)
    assert not errors


def test_response_size_limit(socket_path, monkeypatch):
    monkeypatch.setattr(bridge, "MAX_RESPONSE", 64)
    thread, errors = serve_once(socket_path, lambda r: b"x" * 65 + b"\n")
    with pytest.raises(BridgeError, match="16 MiB"):
        Bridge(socket_path).request("state")
    thread.join(5)
    assert not errors


def test_stale_socket_is_actionable(socket_path):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(socket_path))
        os.chmod(socket_path, 0o600)
    with pytest.raises(BridgeError, match="Do not automatically repeat mutations"):
        Bridge(socket_path).request("state")


def test_regular_file_and_symlink_are_not_sockets(socket_path):
    socket_path.write_text("not a socket")
    with pytest.raises(BridgeError, match="socket owned by this user"):
        Bridge(socket_path).request("state")
    socket_path.unlink()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        target = socket_path.with_name("target.sock")
        server.bind(str(target))
        os.chmod(target, 0o600)
        socket_path.symlink_to(target)
        with pytest.raises(BridgeError, match="socket owned by this user"):
            Bridge(socket_path).request("state")


@pytest.mark.parametrize("timeout", [0, -1, 301, float("nan"), float("inf"), True])
def test_invalid_timeout_rejected_before_connecting(tmp_path, timeout):
    with pytest.raises(BridgeError, match="^Bridge timeout"):
        Bridge(tmp_path / "missing.sock").request("state", timeout=timeout)


def test_nonfinite_request_reports_bridge_error(tmp_path):
    with pytest.raises(BridgeError, match="JSON"):
        Bridge(tmp_path / "missing.sock").request("execute", value=float("nan"))


@pytest.mark.parametrize("method", ["ping", "state", "settings", "api", "reference", "nodes", "material_graph", "brush_graph"])
def test_read_queries_do_not_wait_for_mutation_lock(socket_path, method):
    thread, errors = serve_once(socket_path, lambda r: {"id": r["id"], "ok": True, "data": "read reply"})
    client = Bridge(socket_path)
    with ThreadPoolExecutor(max_workers=1) as executor:
        client.lock.acquire()
        try:
            future = executor.submit(client.request, method)
            assert future.result(timeout=2) == "read reply"
        finally:
            client.lock.release()
    thread.join(5)
    assert not errors


def test_request_id_cannot_be_overridden_by_parameters(socket_path):
    def reply(request):
        assert request["id"] != "injected"
        return {"id": request["id"], "ok": True, "data": "reply"}

    thread, errors = serve_once(socket_path, reply)
    assert Bridge(socket_path).request("state", id="injected") == "reply"
    thread.join(5)
    assert not errors
