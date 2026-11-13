"""Stage 2 discovery: attachments in homework details + open file access + refresh probe.

Writes cache/raw/details_report.txt. Never prints credentials or tokens.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "cache" / "raw"
BASE = "https://mapi.itstep.org/v1/mystat"
LIST_Q = "/od/homework/list?sort=-hw.time&status=3&limit=1000"

env = dotenv_values(ROOT / ".env")
LOGIN = (env.get("LOGIN") or "").strip()
PASSWORD = (env.get("PASSWORD") or "").strip()

H = {
    "Content-Type": "application/json",
    "X-Language": "uk",
    "Accept": "application/json, text/plain, */*",
}

out: list[str] = []


def say(line: str = "") -> None:
    out.append(line)


def keys_of(obj: Any) -> str:
    if isinstance(obj, dict):
        return ", ".join(obj.keys())
    if isinstance(obj, list):
        return f"list[{len(obj)}]"
    return type(obj).__name__


def rows(payload: Any) -> list[dict]:
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    return []


def pick(detail: Any) -> dict:
    if isinstance(detail, list):
        return detail[0] if detail else {}
    if isinstance(detail, dict):
        if "0" in detail and isinstance(detail["0"], dict):
            return detail["0"]
        return detail
    return {}


def collect_file_fields(node: Any, prefix: str = "") -> list[tuple[str, str]]:
    """Find every string that looks like a URL."""
    found: list[tuple[str, str]] = []
    if isinstance(node, dict):
        for k, v in node.items():
            found += collect_file_fields(v, f"{prefix}.{k}" if prefix else k)
    elif isinstance(node, list):
        for i, v in enumerate(node[:5]):
            found += collect_file_fields(v, f"{prefix}[{i}]")
    elif isinstance(node, str) and node.startswith(("http://", "https://")):
        found.append((prefix, node))
    return found


async def main() -> int:
    async with httpx.AsyncClient(base_url=BASE, timeout=60.0, follow_redirects=True) as c:
        r = await c.post("/auth/login", json={"login": LOGIN, "password": PASSWORD}, headers=H)
        say(f"login: {r.status_code}")
        token = r.text.strip().strip('"')
        if not token or len(token) < 20:
            say("!! login failed, abort")
            return 1
        auth = {"X-Language": "uk", "Accept": "application/json"}
        hdr = {**auth, "Authorization": f"Bearer {token}"}

        lst = await c.get(LIST_Q, headers=hdr)
        payload = lst.json()
        items = rows(payload)
        say(f"list: {lst.status_code}, items={len(items)}")
        say("pagination headers: " + ", ".join(
            f"{k}={v}" for k, v in lst.headers.items() if k.lower().startswith("x-pagination") or k.lower() == "x-release"
        ))
        say("top-level keys: " + keys_of(payload))
        say("meta: " + json.dumps(payload.get("_meta"), ensure_ascii=False))

        statuses = sorted({it.get("status") for it in items})
        say(f"status values: {statuses}")
        say(f"subjects: {len({it.get('name_spec') for it in items})}")

        def has_files(it: dict) -> bool:
            hs = it.get("homework_stud") or {}
            hc = it.get("homework_comment") or {}
            return bool(it.get("file_path") or hs.get("file_path") or hc.get("attachment") or hc.get("attachment_path"))

        with_files = [it for it in items if has_files(it)]
        say(f"items with file fields in list: {len(with_files)}")

        # also look for any url-looking strings in list rows
        urls_in_list: set[str] = set()
        for it in items:
            for path, url in collect_file_fields(it):
                if "/photo" in path or "photo_pas" in path:
                    continue
                urls_in_list.add(f"{path} -> {url}")
        say(f"non-photo urls in list: {len(urls_in_list)}")
        for u in sorted(urls_in_list)[:10]:
            say("  " + u)

        targets = (with_files or items)[:3]
        for it in targets:
            hid = it.get("id")
            say("")
            say("=" * 70)
            say(f"DETAIL /od/homework/{hid}")
            d = await c.get(f"/od/homework/{hid}", headers=hdr)
            say(f"status: {d.status_code}, content-type: {d.headers.get('content-type')}, len={len(d.text)}")
            (RAW / f"homework_{hid}.json").write_text(d.text, encoding="utf-8")
            detail = d.json()
            main_part = pick(detail)
            say("detail top-level keys: " + keys_of(detail))
            say("detail object keys: " + keys_of(main_part))
            say("theme: " + str(main_part.get("theme")))
            say("comment len: " + str(len(main_part.get("comment") or "")))
            say("file_path: " + repr(main_part.get("file_path")))
            hs = main_part.get("homework_stud") or {}
            say(f"homework_stud.file_path: {hs.get('file_path')!r}")
            say(f"homework_stud.stud_answer len: {len(hs.get('stud_answer') or '')}")
            hc = main_part.get("homework_comment") or {}
            say("homework_comment: " + json.dumps(hc, ensure_ascii=False)[:400])

            urls = [(p, u) for p, u in collect_file_fields(detail) if "photo_pas" not in p]
            say(f"url-like strings in detail: {len(urls)}")
            for p, u in urls[:12]:
                say(f"  {p} -> {u}")

            # download probe on the first non-photo url
            for p, u in urls[:2]:
                try:
                    f = await c.get(u, headers={"Accept": "*/*"})
                    head = f.content[:8]
                    say(f"  GET {u[:70]}... -> {f.status_code} {f.headers.get('content-type')} "
                        f"len={len(f.content)} magic={head!r}")
                except Exception as exc:  # noqa: BLE001
                    say(f"  GET {u[:70]}... EXC {type(exc).__name__}: {exc}")

        # ---- refresh endpoint probe (payload shape unknown) ----
        say("")
        say("=" * 70)
        say("refresh probes")
        for name, body in (("empty", {}), ("token-field", {"token": "x"}), ("bearer", {})):
            hh = dict(auth)
            if name == "bearer":
                hh["Authorization"] = f"Bearer {token}"
            rr = await c.post("/auth/refresh", json=body, headers=hh)
            say(f"  [{name}] {rr.status_code} :: {rr.text[:120]!r}")

    RAW.mkdir(parents=True, exist_ok=True)
    (RAW / "details_report.txt").write_text("\n".join(out), encoding="utf-8")
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(asyncio.run(main()))
