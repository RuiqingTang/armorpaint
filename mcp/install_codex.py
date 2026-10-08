"""Install this checkout's MCP server into the user's Codex configuration."""
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path


def main():
    root = Path(__file__).resolve().parent
    executable = root / ".venv/bin/armorpaint-mcp"
    if not executable.is_file():
        raise SystemExit("Run `uv sync --project mcp` from the repository first.")
    check = subprocess.run(["codex", "mcp", "get", "armorpaint", "--json"], capture_output=True, text=True)
    if check.returncode == 0:
        entry = json.loads(check.stdout)
        if entry.get("transport", {}).get("command") != str(executable):
            raise SystemExit("An unrelated 'armorpaint' MCP configuration already exists; choose another server name before installing.")
    codex_dir = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    config = codex_dir / "config.toml"
    backup = None
    if config.is_file():
        fd, path = tempfile.mkstemp(prefix="config-before-armorpaint-", suffix=".toml", dir=codex_dir)
        os.close(fd)
        backup = Path(path)
        shutil.copyfile(config, backup)
        backup.chmod(0o600)
    if check.returncode != 0:
        subprocess.run(["codex", "mcp", "add", "armorpaint", "--", str(executable)], check=True)
    text = config.read_text()
    match = re.search(r"(?m)^\[mcp_servers\.armorpaint\]\n(?P<body>[\s\S]*?)(?=^\[|\Z)", text)
    if match is None:
        raise SystemExit("Server added; set tool_timeout_sec=330 in its config table manually.")
    body = match["body"]
    if re.search(r"(?m)^tool_timeout_sec\s*=", body):
        body = re.sub(r"(?m)^tool_timeout_sec\s*=.*$", "tool_timeout_sec = 330", body)
    else:
        body = body.rstrip() + "\ntool_timeout_sec = 330\n\n"
    fd, path = tempfile.mkstemp(prefix=".armorpaint-config-", dir=codex_dir)
    with os.fdopen(fd, "w") as file:
        file.write(text[:match.start("body")] + body + text[match.end("body"):])
    os.replace(path, config)
    print("Configured ArmorPaint MCP for Codex.")
    if backup:
        print(f"Previous config backup: {backup}")


if __name__ == "__main__":
    main()
