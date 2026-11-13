"""Inspect the web client: find how it calls /auth/login (endpoint, headers, payload)."""

from __future__ import annotations

import re

import httpx

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"


def analyze(text: str, label: str) -> None:
    for pattern in (r"auth/login", r"X-Userid", r"mapi\.itstep\.org", r"access_token", r"mystat"):
        hits = [m.start() for m in re.finditer(pattern, text)]
        print(f"  {label}: '{pattern}' hits={len(hits)}")


async def main() -> None:
    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True, headers={"User-Agent": UA}) as c:
        r = await c.get("https://mystat.itstep.org/")
        print(f"index: {r.status_code} len={len(r.text)}")
        srcs = re.findall(r'src="([^"]+\.js[^"]*)"', r.text) + re.findall(r'href="([^"]+\.js[^"]*)"', r.text)
        print(f"scripts found: {len(srcs)}")
        for s in srcs[:12]:
            print("  ", s)
        for s in srcs[:1]:
            continue

        found_any = False
        for src in srcs[:12]:
            url = src if src.startswith("http") else "https://mystat.itstep.org/" + src.lstrip("/")
            try:
                js = await c.get(url)
            except Exception as exc:  # noqa: BLE001
                print(f"  fetch {url} EXC {exc}")
                continue
            text = js.text
            if "auth/login" in text or "mapi.itstep" in text:
                found_any = True
                print(f"--- MATCH {url} ({js.status_code}, {len(text)} bytes) ---")
                analyze(text, url.split("/")[-1][:40])
                for m in re.finditer(r".{120}auth/login.{200}", text):
                    snippet = m.group(0).replace("\n", " ")
                    print("  >", snippet[:340])
                for m in re.finditer(r".{80}access_token.{160}", text):
                    print("  T>", m.group(0).replace("\n", " ")[:280])
        if not found_any:
            print("no bundle contained auth/login")


import asyncio

asyncio.run(main())
