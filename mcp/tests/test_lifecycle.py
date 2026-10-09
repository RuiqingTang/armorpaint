"""Tests that may poison or crash an app, always using a private subprocess."""
import asyncio
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import pytest

from armorpaint_mcp import server
from armorpaint_mcp.bridge import Bridge, BridgeError

pytestmark = pytest.mark.skipif(
    not os.environ.get("ARMORPAINT_MCP_TEST_SOCKET"),
    reason="requires explicit opt-in to disposable native app testing",
)
ROOT = Path(__file__).resolve().parents[2]
DEFAULT_APP = ROOT / "paint/build/mcp-derived/Build/Products/Debug/ArmorPaint.app/Contents/MacOS/ArmorPaint"


@pytest.fixture
def fresh_app(monkeypatch, request):
    source = Path(getattr(request, "param", None) or os.environ.get("ARMORPAINT_MCP_TEST_APP", DEFAULT_APP))
    if not source.is_file():
        pytest.skip("Build a native ArmorPaint test app first")
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="ap-life-") as directory:
        path = Path(directory) / "mcp.sock"
        bundle = source.parents[2]
        assert bundle.suffix == ".app", "Lifecycle tests currently require a macOS app bundle"
        clone = Path(directory) / bundle.name
        shutil.copytree(bundle, clone, ignore=shutil.ignore_patterns("config.json"))
        plist_path = clone / "Contents/Info.plist"
        with plist_path.open("rb") as file:
            info = plistlib.load(file)
        info["CFBundleIdentifier"] = "org.armory3d.armorpaint.mcp-test." + uuid.uuid4().hex
        with plist_path.open("wb") as file:
            plistlib.dump(info, file)
        binary = clone / source.relative_to(bundle)
        process = None
        client = Bridge(path)

        def stop():
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)

        def start():
            nonlocal process
            process = subprocess.Popen(
                [str(binary), "--mcp-socket", str(path), "-ApplePersistenceIgnoreState", "YES"],
                cwd=ROOT,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            deadline = time.monotonic() + 60
            while True:
                assert process.poll() is None, "Test app exited before starting its bridge"
                try:
                    client.request("ping", timeout=1)
                    break
                except BridgeError:
                    assert time.monotonic() < deadline, "Test app bridge did not start"
                    time.sleep(0.05)

        def restart():
            stop()
            start()

        try:
            start()
            client.restart = restart
            client.config_path = clone / "Contents/Resources/out/data/config.json"
            monkeypatch.setattr(server, "bridge", client)
            yield client
        finally:
            stop()


def test_async_timeout_requires_restart_but_keeps_reads_available(fresh_app):
    with pytest.raises(BridgeError, match="timed out") as exc:
        asyncio.run(server.execute_code(
            "void main() { mcp_task_begin(); }", timeout=1, wait_frames=1
        ))
    assert exc.value.result["changes_may_have_been_applied"] is True
    assert fresh_app.request("state")["busy"] is False
    assert fresh_app.request("settings")["settings"]
    for _ in range(2):
        with pytest.raises(BridgeError, match="Restart"):
            asyncio.run(server.execute_code("void main() {}", retain_context=False))
    assert fresh_app.request("ping") == "ArmorPaint MCP bridge v1"


def test_empty_project_file_is_rejected_without_crashing(fresh_app, tmp_path):
    path = tmp_path / "empty.arm"
    path.write_bytes(b"")
    before = fresh_app.request("state")
    with pytest.raises((ValueError, BridgeError), match="empty|Invalid"):
        asyncio.run(server.project_operation("open", str(path)))
    assert fresh_app.request("state")["layers"] == before["layers"]


@pytest.mark.parametrize("data", [b"", b"\xdf\0\0\0", b"not a project file"])
def test_native_invalid_project_header_does_not_crash(fresh_app, tmp_path, data):
    path = tmp_path / "broken.arm"
    path.write_bytes(data)
    before = fresh_app.request("state")
    result = asyncio.run(server.call_function("script_project_open", [str(path)]))
    assert "Invalid or truncated" in result["log"]
    assert fresh_app.request("state")["layers"] == before["layers"]


@pytest.mark.parametrize("filename", ["roundtrip.arm", '中文 "quoted".arm', "back\\slash.arm", "new\nline.arm", "emoji \U0001f3a8.arm"])
def test_project_path_and_config_survive_script_cleanup_and_restart(fresh_app, tmp_path, filename):
    path = tmp_path / filename
    asyncio.run(server.project_operation("save", str(path)))
    asyncio.run(server.project_operation("open", str(path)))
    for index in range(12):
        asyncio.run(server.execute_code(
            f'void main() {{ console_log("{"x" * (index * 16)}"); }}',
            retain_context=False, wait_frames=1,
        ))
    asyncio.run(server.call_function("config_save", []))
    config = json.loads(fresh_app.config_path.read_text())
    assert config["recent_projects"] == [str(path)]
    fresh_app.config_path.write_text(json.dumps(config, ensure_ascii=True, separators=(",", ":")))
    fresh_app.restart()
    assert fresh_app.request("ping") == "ArmorPaint MCP bridge v1"
    asyncio.run(server.call_function("config_save", []))
    assert json.loads(fresh_app.config_path.read_text())["recent_projects"] == [str(path)]


@pytest.mark.skipif(
    not os.environ.get("ARMORPAINT_MCP_LIMIT_TEST_APP"),
    reason="requires a separate app built with MCP_RETAINED_CONTEXTS=4",
)
@pytest.mark.parametrize("fresh_app", [os.environ.get("ARMORPAINT_MCP_LIMIT_TEST_APP")], indirect=True)
def test_retained_context_limit_applies_to_failed_nonretained_scripts(fresh_app):
    # Use a low-cap build rather than allocating hundreds of large VM contexts.
    for _ in range(4):
        with pytest.raises(BridgeError, match="Script failed"):
            fresh_app.request("execute", code="void main() { nonexistent_operator(); }", retain_context=False, wait_frames=1)
    assert fresh_app.request("state")["retained_contexts"] == 4
    with pytest.raises(BridgeError, match="context limit"):
        fresh_app.request("execute", code="void main() { nonexistent_operator(); }", retain_context=False, wait_frames=1)


@pytest.mark.skipif(not os.environ.get("ARMORPAINT_MCP_LIMIT_TEST_APP"), reason="requires MCP_RETAINED_CONTEXTS=4 build")
@pytest.mark.parametrize("fresh_app", [os.environ.get("ARMORPAINT_MCP_LIMIT_TEST_APP")], indirect=True)
def test_retained_context_limit_stops_before_running_next_script(fresh_app):
    for _ in range(4):
        assert fresh_app.request("execute", code="int main() { return 42; }", wait_frames=1)["return_value"] == 42
    state = fresh_app.request("state")
    assert state["retained_contexts"] == 4
    with pytest.raises(BridgeError, match="context limit"):
        fresh_app.request(
            "execute", code='void main() { script_get_context()->layer->name = string_copy("Should not run"); }',
            retain_context=False, wait_frames=1,
        )
    assert fresh_app.request("state")["layers"] == state["layers"]


def test_core_suite_on_isolated_application(fresh_app):
    env = {**os.environ, "ARMORPAINT_MCP_TEST_SOCKET": str(fresh_app.path)}
    result = subprocess.run(
        [
            sys.executable, "-m", "pytest", "-q",
            "mcp/tests/test_bridge.py", "mcp/tests/test_server.py",
            "mcp/tests/test_live.py", "mcp/tests/test_mcp.py",
        ],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    print(result.stdout.strip())
