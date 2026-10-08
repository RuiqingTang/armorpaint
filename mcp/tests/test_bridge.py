import json
import os
import socket
import threading
import tempfile
from pathlib import Path

import pytest

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
                    data += peer.recv(4096)
                request = json.loads(data)
                output = json.dumps(reply(request), ensure_ascii=False).encode() + b"\n"
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
