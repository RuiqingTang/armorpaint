"""Authenticated by OS permissions on a local Unix domain socket."""
import json
import os
import socket
import stat
import sys
import threading
import uuid
from contextlib import nullcontext
from pathlib import Path

MAX_REQUEST = 1024 * 1024
MAX_RESPONSE = 16 * 1024 * 1024


class BridgeError(RuntimeError):
    def __init__(self, message, result=None):
        super().__init__(message + ("\n" + json.dumps(result, ensure_ascii=False) if result is not None else ""))
        self.result = result


def default_socket_path():
    if os.environ.get("ARMORPAINT_MCP_SOCKET"):
        return Path(os.environ["ARMORPAINT_MCP_SOCKET"]).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/ArmorPaint/mcp.sock"
    legacy = Path.home() / ".ArmorPaint"
    root = legacy if legacy.is_dir() else Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "ArmorPaint"
    return root / "mcp.sock"


class Bridge:
    def __init__(self, path=None):
        self.path = Path(path).expanduser().absolute() if path else default_socket_path()
        self.lock = threading.Lock()

    def request(self, method, *, timeout=60, **params):
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= timeout <= 300:
            raise BridgeError("Bridge timeout must be a finite number between 1 and 300 seconds")
        request_id = uuid.uuid4().hex
        try:
            request = json.dumps({**params, "id": request_id, "method": method, "timeout": timeout}, ensure_ascii=True, allow_nan=False).encode() + b"\n"
        except (ValueError, TypeError) as exc:
            raise BridgeError(f"Invalid bridge request JSON: {exc}") from exc
        if len(request) >= MAX_REQUEST:
            raise BridgeError("Request exceeds the bridge's 1 MiB limit")
        try:
            info = self.path.lstat()
        except FileNotFoundError as exc:
            raise BridgeError(f"ArmorPaint MCP is not running at {self.path}. In the rebuilt app, select Help → Start MCP Server or launch with --mcp.") from exc
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise BridgeError("Bridge path must be a socket owned by this user with mode 0600")
        read_only = method in ("ping", "state", "settings", "api", "reference", "nodes", "material_graph", "brush_graph")
        with nullcontext() if read_only else self.lock:
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
                    conn.settimeout(timeout + 10)
                    conn.connect(str(self.path))
                    conn.sendall(request)
                    data = bytearray()
                    while b"\n" not in data:
                        part = conn.recv(65536)
                        if not part:
                            raise BridgeError("ArmorPaint closed the connection before replying; an operation may have partially run")
                        data.extend(part)
                        if len(data) > MAX_RESPONSE:
                            raise BridgeError("Bridge response exceeds 16 MiB")
            except (ConnectionError, OSError) as exc:
                raise BridgeError(f"ArmorPaint connection failed: {exc}. Do not automatically repeat mutations; check the project state first.") from exc
        try:
            result = json.loads(data.split(b"\n", 1)[0])
            if not isinstance(result, dict) or result.get("id") != request_id:
                raise ValueError("Response ID mismatch")
            if not isinstance(result.get("ok"), bool):
                raise ValueError("Response ok must be a boolean")
            if "error" in result and not isinstance(result["error"], str):
                raise ValueError("Response error must be a string")
            payload = result.get("data")
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except json.JSONDecodeError:
                    pass
            if not result.get("ok"):
                raise BridgeError(result.get("error", "ArmorPaint operation failed"), payload)
            return payload
        except (ValueError, TypeError) as exc:
            raise BridgeError(f"Invalid bridge response: {exc}") from exc
