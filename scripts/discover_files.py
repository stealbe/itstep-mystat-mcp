"""Stage 3 discovery: attachment headers, suspicious html file, comment parity."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "cache" / "raw"
BASE = "https://mapi.itstep.org/v1/mystat"
LIST_Q = "/od/homework/list?sort=-hw.time&status=3&limit=1000"

env = dotenv_values(ROOT / ".env")
LOGIN = (env.get("LOGIN") or "").strip()
PASSWORD = (env.get("PASSWORD") or "").strip()

H = {"Content-Type": "application/json", "X-Language": "uk"}
out: list[str] = []


def say(s: str = "") -> None:
    out.append(s)


def pick(detail):
    if isinstance(detail, dict) and isinstance(detail.get("0"), dict):
        return detail["0"]
    if isinstance(detail, list) and detail:
        return detail[0]
    return detail


async def main() -> None:
    async with httpx.AsyncClient(base_url=BASE, timeout=60.0, follow_redirects=True) as c:
        r = await c.post("/auth/login", json={"login": LOGIN, "password": PASSWORD}, headers=H)
        token = r.text.strip().strip('"')
        hdr = {"X-Language": "uk", "Accept": "application/json", "Authorization": f"Bearer {token}"}

        lst = (await c.get(LIST_Q, headers=hdr)).json()
        items = lst["data"]

        # comment parity: compare list comment vs detail comment
        differences = 0
        checked = 0
        for it in items[:10]:
            hid = it["id"]
            d = (await c.get(f"/od/homework/{hid}", headers=hdr)).json()
            part = pick(d)
            checked += 1
            if (it.get("comment") or "") != (part.get("comment") or ""):
                differences += 1
                say(f"comment differs for {hid}: list={len(it.get('comment') or '')} detail={len(part.get('comment') or '')}")
        say(f"comment parity checked={checked} differences={differences}")

        # unique attachment hosts
        hosts: dict[str, int] = {}
        for it in items:
            for key in ("file_path", "cover_image"):
                url = it.get(key)
                if url:
                    host = httpx.URL(url).host
                    hosts[host] = hosts.get(host, 0) + 1
        say("hosts: " + json.dumps(hosts, ensure_ascii=False))

        samples = [it for it in items if it.get("file_path")][:4]
        for it in samples:
            url = it["file_path"]
            rr = await c.get(url, headers={"Accept": "*/*"})
            say("")
            say(f"file {it['id']} {url[:80]}")
            for k in ("content-type", "content-length", "content-disposition", "etag",
                      "last-modified", "cache-control", "server", "access-control-allow-origin"):
                if k in rr.headers:
                    say(f"  {k}: {rr.headers[k]}")
            ct = rr.headers.get("content-type", "")
            if "text/html" in ct:
                text = rr.text
                say(f"  html len={len(text)}; title snippet: {text[:300]!r}")
                (RAW / f"suspect_html_{it['id']}.html").write_text(text, encoding="utf-8")

    (RAW / "files_report.txt").write_text("\n".join(out), encoding="utf-8")
    print("\n".join(out))


asyncio.run(main())
