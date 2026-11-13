"""Register the MCP server in local LLM clients.

Supported clients and their config locations:

* ``opencode``        - ``~/.config/opencode/opencode.json[c]``
* ``claude-code``     - ``claude mcp add --scope user`` (CLI is delegated)
* ``claude-desktop``  - ``%APPDATA%/Claude/claude_desktop_config.json``
* ``codex``           - ``~/.codex/config.toml``

Every writer makes a timestamped backup next to the file and refuses to
rewrite a file it cannot understand instead of silently corrupting it.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

SERVER_NAME = "itstep-mystat"


@dataclass
class Change:
    client: str
    path: str
    ok: bool
    detail: str = ""

    def line(self) -> str:
        mark = "OK  " if self.ok else "SKIP"
        return f"[{mark}] {self.client:<15} {self.path} {self.detail}".rstrip()


# ------------------------------------------------------------------ paths ---
def home() -> Path:
    return Path.home()


def opencode_config_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or (home() / ".config"))
    jsonc = base / "opencode" / "opencode.jsonc"
    plain = base / "opencode" / "opencode.json"
    if jsonc.is_file():
        return jsonc
    if plain.is_file():
        return plain
    return jsonc


def claude_desktop_config_path() -> Path:
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA") or str(home() / "AppData" / "Roaming")
        return Path(appdata) / "Claude" / "claude_desktop_config.json"
    if sys.platform == "darwin":
        return home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    return Path(os.environ.get("XDG_CONFIG_HOME") or (home() / ".config")) / "Claude" / "claude_desktop_config.json"


def codex_config_path() -> Path:
    base = Path(os.environ.get("CODEX_HOME") or (home() / ".codex"))
    return base / "config.toml"


def server_command() -> list[str]:
    """Absolute command line that starts this server over stdio."""
    return [sys.executable, "-m", "mystat"]


# --------------------------------------------------------------- helpers ---
def _backup(path: Path) -> Path | None:
    if not path.is_file():
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = path.with_name(f"{path.name}.bak-{stamp}")
    shutil.copy2(path, target)
    return target


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


SERVER_ENV = {"PYTHONUTF8": "1"}


def _entry() -> dict:
    return {
        "type": "local",
        "command": server_command(),
        "environment": dict(SERVER_ENV),
        "enabled": True,
        "timeout": 180000,
    }


def _json_entry() -> dict:
    return {"command": server_command(), "args": [], "env": dict(SERVER_ENV)}


# --------------------------------------------------------------- opencode ---
def _strip_jsonc(text: str) -> str:
    """Remove // and /* */ comments plus trailing commas, respecting strings."""
    out: list[str] = []
    i, n = 0, len(text)
    in_string = False
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(ch)
        i += 1
    cleaned = "".join(out)
    return re.sub(r",(\s*[}\]])", r"\1", cleaned)


def _insert_into_mcp_block(text: str, name: str, entry: dict) -> str | None:
    """Insert an entry into ``"mcp": { ... }`` inside a commented JSON file."""
    match = re.search(r'"mcp"\s*:\s*\{', text)
    if not match:
        return None
    if re.search(rf'"{re.escape(name)}"\s*:', text):
        return text
    start = match.end()
    depth = 1
    i = start
    in_string = False
    while i < len(text) and depth:
        ch = text[i]
        if in_string:
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        i += 1
    if depth:
        return None

    # Keep the entry visually nested under "mcp": the JSON serializer emits
    # two-space indentation, so shift every line by the indentation of the
    # `"mcp"` key itself plus two spaces.
    line_start = text.rfind("\n", 0, match.start()) + 1
    key_indent = text[line_start:match.start()]
    if key_indent.strip():
        key_indent = ""
    inner_indent = key_indent
    lines = json.dumps({name: entry}, ensure_ascii=False, indent=2).splitlines()[1:-1]
    body = "\n".join(inner_indent + line for line in lines)

    head, tail = text[:start], text[start:]
    if tail.lstrip().startswith("}"):
        return head + "\n" + body + "\n" + key_indent + tail.lstrip()
    return head + "\n" + body + ",\n" + tail


def _has_comments(text: str) -> bool:
    """True if the file contains // or /* */ outside of string literals."""
    in_string = False
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if in_string:
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] in "/*":
            return True
        i += 1
    return False


def register_opencode(path: Path | None = None) -> Change:
    target = path or opencode_config_path()
    entry = _entry()
    if not target.is_file():
        payload = {"$schema": "https://opencode.ai/config.json", "mcp": {SERVER_NAME: entry}}
        _write(target, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        return Change("opencode", str(target), True, "created")

    text = target.read_text(encoding="utf-8")
    _backup(target)
    data = None
    if not _has_comments(text):
        try:
            data = json.loads(text)
        except ValueError:
            data = None

    if isinstance(data, dict):
        mcp = data.setdefault("mcp", {})
        if SERVER_NAME in mcp:
            return Change("opencode", str(target), True, "already configured")
        mcp[SERVER_NAME] = entry
        _write(target, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        return Change("opencode", str(target), True, "added")

    updated = _insert_into_mcp_block(text, SERVER_NAME, entry)
    if updated is None:
        return Change("opencode", str(target), False,
                      'no "mcp" block found; add the entry manually (see README)')
    if updated == text:
        return Change("opencode", str(target), True, "already configured")
    _write(target, updated)
    return Change("opencode", str(target), True, "added (textual insert, comments kept)")


# ----------------------------------------------------------- claude code ----
def register_claude_code() -> Change:
    binary = shutil.which("claude")
    if not binary:
        return Change("claude-code", "-", False, "claude CLI not found on PATH")
    command = [binary, "mcp", "add", "--scope", "user", SERVER_NAME]
    for key, value in SERVER_ENV.items():
        command += ["-e", f"{key}={value}"]
    command += ["--", *server_command()]
    try:
        result = subprocess.run(command, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=60)
    except Exception as exc:  # noqa: BLE001
        return Change("claude-code", binary, False, f"{type(exc).__name__}: {exc}")
    output = (result.stdout or result.stderr or "").strip().splitlines()
    detail = output[-1][:120] if output else ""
    if result.returncode != 0:
        already = "already exists" in detail.lower()
        return Change("claude-code", binary, already, detail or f"exit {result.returncode}")
    return Change("claude-code", binary, True, detail or "added")


# ------------------------------------------------------- claude desktop ----
def register_claude_desktop(path: Path | None = None) -> Change:
    target = path or claude_desktop_config_path()
    entry = _json_entry()
    data: dict = {}
    if target.is_file():
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except ValueError:
            return Change("claude-desktop", str(target), False, "config is not valid JSON; not modified")
        _backup(target)
    servers = data.setdefault("mcpServers", {})
    if SERVER_NAME in servers:
        return Change("claude-desktop", str(target), True, "already configured")
    servers[SERVER_NAME] = entry
    _write(target, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return Change("claude-desktop", str(target), True, "added")


# ---------------------------------------------------------------- codex ----
def register_codex(path: Path | None = None) -> Change:
    target = path or codex_config_path()
    if target.is_file() and f"[mcp_servers.{SERVER_NAME}]" in target.read_text(encoding="utf-8"):
        return Change("codex", str(target), True, "already configured")
    cmd = server_command()
    env_pairs = ", ".join(f'{key} = {json.dumps(value)}' for key, value in SERVER_ENV.items())
    block = (
        f"\n[mcp_servers.{SERVER_NAME}]\n"
        f'command = {json.dumps(cmd[0])}\n'
        f"args = {json.dumps(cmd[1:])}\n"
        f"env = {{ {env_pairs} }}\n"
    )
    if target.is_file():
        _backup(target)
        text = target.read_text(encoding="utf-8")
        if text and not text.endswith("\n"):
            text += "\n"
        _write(target, text + block)
    else:
        _write(target, block.lstrip("\n"))
    return Change("codex", str(target), True, "added")


# ---------------------------------------------------------------- public ---
AVAILABLE = {
    "opencode": lambda: register_opencode(),
    "claude-code": lambda: register_claude_code(),
    "claude-desktop": lambda: register_claude_desktop(),
    "codex": lambda: register_codex(),
}


def detect_clients() -> list[str]:
    found: list[str] = []
    if opencode_config_path().parent.is_dir():
        found.append("opencode")
    if shutil.which("claude"):
        found.append("claude-code")
    if claude_desktop_config_path().is_file():
        found.append("claude-desktop")
    if codex_config_path().parent.is_dir():
        found.append("codex")
    return found


def register(clients: list[str]) -> list[Change]:
    return [AVAILABLE[name]() for name in clients if name in AVAILABLE]


def manual_snippet() -> str:
    payload = {"mcp": {SERVER_NAME: _entry()}}
    return json.dumps(payload, ensure_ascii=False, indent=2)
