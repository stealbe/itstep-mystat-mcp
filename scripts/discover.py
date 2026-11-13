"""One-off discovery script for MyStat API.

Prints a *shape* report (no secrets, no tokens) and stores raw responses under
cache/raw/ for later parser development.

Run:  .venv\\Scripts\\python.exe scripts\\discover.py
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
from pathlib import Path
from typing import Any

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "cache" / "raw"
ENV_PATH = ROOT / ".env"

BASE_URL = "https://mapi.itstep.org/v1/mystat"
LIST_PATH = "/od/homework/list"
LIST_QUERY = "?sort=-hw.time&status=3&limit=1000"

SENSITIVE_MARKERS = ("token", "password", "secret", "cookie", "authorization", "sid", "jwt")
MAX_STRING = 160


def load_creds() -> tuple[str, str]:
    env = dotenv_values(ENV_PATH)
    login = (env.get("LOGIN") or env.get("MYSTAT_LOGIN") or "").strip()
    password = (env.get("PASSWORD") or env.get("MYSTAT_PASSWORD") or "").strip()
    if not login or not password:
        raise SystemExit(f"LOGIN/PASSWORD not found in {ENV_PATH}")
    return login, password


def is_sensitive(name: str) -> bool:
    low = name.lower()
    return any(m in low for m in SENSITIVE_MARKERS)


def decode_jwt_payload(token: str) -> dict[str, Any] | None:
    parts = token.split(".")
    if len(parts) < 2:
        return None
    body = parts[1]
    body += "=" * (-len(body) % 4)
    try:
        raw = base64.urlsafe_b64decode(body)
        return json.loads(raw)
    except Exception:
        return None


def skeleton(value: Any, path: str = "", depth: int = 0) -> Any:
    """Return a compact structural description instead of raw content."""
    pad = "  " * depth
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            out[k] = skeleton(v, f"{path}.{k}", depth + 1)
        return out
    if isinstance(value, list):
        if not value:
            return {"__type": "list", "__len": 0}
        return {
            "__type": "list",
            "__len": len(value),
            "__item": skeleton(value[0], f"{path}[0]", depth + 1),
        }
    if isinstance(value, str):
        if is_sensitive(path):
            return {"__type": "str", "__len": len(value), "__value": "<masked>"}
        trimmed = value if len(value) <= MAX_STRING else value[:MAX_STRING] + f"...<{len(value)} chars>"
        return {"__type": "str", "__len": len(value), "__value": trimmed}
    return {"__type": type(value).__name__, "__value": value}


def render(node: Any, depth: int = 0) -> list[str]:
    """Render skeleton as an indented key list for human reading."""
    pad = "  " * depth
    lines: list[str] = []
    if isinstance(node, dict):
        if "__type" in node and node["__type"] == "list":
            lines.append(f"{pad}[list len={node['__len']}]")
            if "__item" in node:
                lines.extend(render(node["__item"], depth + 1))
            return lines
        if "__type" in node and node["__type"] == "str":
            lines.append(f"{pad}<- str len={node['__len']} value={node['__value']!r}>")
            return lines
        if "__type" in node:
            lines.append(f"{pad}<- {node['__type']} {node.get('__value')!r}")
            return lines
        for k, v in node.items():
            if isinstance(v, dict) and "__type" not in v:
                lines.append(f"{pad}{k}: {{...}}")
                lines.extend(render(v, depth + 1))
            elif isinstance(v, dict) and v.get("__type") == "list":
                lines.append(f"{pad}{k}: [list len={v['__len']}]")
                if "__item" in v:
                    lines.extend(render(v["__item"], depth + 1))
            elif isinstance(v, dict) and v.get("__type") == "str":
                lines.append(f"{pad}{k}: str len={v['__len']} {v['__value']!r}")
            elif isinstance(v, dict):
                lines.append(f"{pad}{k}: {v.get('__type')} {v.get('__value')!r}")
            else:
                lines.append(f"{pad}{k}: {v!r}")
        return lines
    lines.append(f"{pad}{node!r}")
    return lines


def find_first_id(payload: Any) -> Any:
    """Find the first homework id-ish value in an unknown list payload."""
    def rows(node: Any) -> list[dict] | None:
        if isinstance(node, list):
            return [x for x in node if isinstance(x, dict)]
        if isinstance(node, dict):
            for key in ("data", "items", "rows", "list", "homework", "result"):
                if key in node:
                    found = rows(node[key])
                    if found:
                        return found
            for value in node.values():
                found = rows(value)
                if found:
                    return found
        return None

    found = rows(payload)
    if not found:
        return None
    row = found[0]
    for key in ("id", "homework_id", "hw_id", "Id", "ID"):
        if key in row:
            return row[key]
    return None


async def main() -> int:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    login_name, password = load_creds()
    report: list[str] = []
    say = report.append

    headers_base = {
        "Content-Type": "application/json",
        "X-Language": "uk",
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://mystat.itstep.org",
        "Referer": "https://mystat.itstep.org/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) itstep-mystat-mcp/0.0 discovery",
    }

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30.0, follow_redirects=True) as client:
        # ---- 1. login -------------------------------------------------
        say("=" * 70)
        say("1) POST /auth/login")
        resp = await client.post("/auth/login", json={"login": login_name, "password": password}, headers=headers_base)
        say(f"status: {resp.status_code}")
        say(f"content-type: {resp.headers.get('content-type')}")
        for h in ("x-request-id", "set-cookie", "cache-control"):
            if h in resp.headers:
                value = resp.headers[h]
                if h == "set-cookie":
                    value = "<masked>"
                say(f"header {h}: {value}")
        body_text = resp.text
        say(f"body length: {len(body_text)}")
        (RAW_DIR / "login.shape.txt").write_text(
            "\n".join(render(skeleton({"raw": body_text}))), encoding="utf-8"
        )

        token = None
        content_type = (resp.headers.get("content-type") or "").lower()
        if "json" in content_type:
            try:
                parsed = resp.json()
            except Exception:
                parsed = None
            if isinstance(parsed, str):
                token = parsed
                say("json body: bare JSON string (string token)")
            elif isinstance(parsed, dict):
                say("json body keys: " + ", ".join(parsed.keys()))
                for key in ("access_token", "accessToken", "token", "data", "result"):
                    candidate = parsed.get(key)
                    if isinstance(candidate, str) and len(candidate) > 10:
                        token = candidate
                        say(f"token taken from key: {key}")
                        break
                    if isinstance(candidate, dict):
                        for sub in ("access_token", "accessToken", "token"):
                            if isinstance(candidate.get(sub), str):
                                token = candidate[sub]
                                say(f"token taken from key: {key}.{sub}")
                                break
                        if token:
                            break
            else:
                say(f"json body type: {type(parsed).__name__}")
        if token is None:
            stripped = body_text.strip().strip('"')
            if stripped and " " not in stripped and len(stripped) < 4096:
                token = stripped
                say("body treated as raw text token")

        if not token:
            say("!! token not extracted")
            print("\n".join(report))
            return 1

        say(f"token: <len={len(token)}>")
        say(f"token starts with 'eyJ' (JWT-like): {token.startswith('eyJ')}")
        payload = decode_jwt_payload(token)
        if payload:
            say("jwt payload keys: " + ", ".join(payload.keys()))
            for key in ("exp", "iat", "nbf", "sub", "user_id", "id"):
                if key in payload:
                    value = payload[key]
                    if key == "sub" and isinstance(value, str) and len(value) > 20:
                        value = f"<masked len={len(value)}>"
                    say(f"jwt {key}: {value}")

        # ---- 2. homework list ----------------------------------------
        auth_headers = dict(headers_base)
        auth_headers["Authorization"] = f"Bearer {token}"
        auth_headers.pop("Content-Type", None)

        say("=" * 70)
        say(f"2) GET {LIST_PATH}{LIST_QUERY}")
        resp = await client.get(LIST_PATH + LIST_QUERY, headers=auth_headers)
        say(f"status (no X-Userid): {resp.status_code}")
        say(f"content-type: {resp.headers.get('content-type')}")
        say(f"body length: {len(resp.text)}")
        if resp.status_code in (401, 403):
            say("-> retry with X-Userid is not possible yet (id source unknown)")
        if resp.status_code == 200:
            (RAW_DIR / "list.json").write_text(resp.text, encoding="utf-8")
            try:
                list_payload = resp.json()
            except Exception:
                list_payload = None
            say("-- structure --")
            say("\n".join(render(skeleton(list_payload))))
            (RAW_DIR / "list.shape.txt").write_text("\n".join(render(skeleton(list_payload))), encoding="utf-8")

            hw_id = find_first_id(list_payload)
            say(f"first homework id candidate: {hw_id!r}")

            # ---- 3. homework details ---------------------------------
            if hw_id is not None:
                say("=" * 70)
                say(f"3) GET /od/homework/{hw_id}")
                detail = await client.get(f"/od/homework/{hw_id}", headers=auth_headers)
                say(f"status: {detail.status_code}")
                say(f"content-type: {detail.headers.get('content-type')}")
                say(f"etag: {detail.headers.get('etag')}")
                say(f"last-modified: {detail.headers.get('last-modified')}")
                say(f"body length: {len(detail.text)}")
                if detail.status_code == 200:
                    (RAW_DIR / f"homework_{hw_id}.json").write_text(detail.text, encoding="utf-8")
                    try:
                        detail_payload = detail.json()
                    except Exception:
                        detail_payload = None
                    say("-- structure --")
                    say("\n".join(render(skeleton(detail_payload))))
                    (RAW_DIR / f"homework_{hw_id}.shape.txt").write_text(
                        "\n".join(render(skeleton(detail_payload))), encoding="utf-8"
                    )

        text = "\n".join(report)
        (RAW_DIR / "report.txt").write_text(text, encoding="utf-8")
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
