"""Probe login variants to distinguish 'bad creds' from 'bad request shape'."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
env = dotenv_values(ROOT / ".env")
login = (env.get("LOGIN") or "").strip()
password = (env.get("PASSWORD") or "").strip()
raw_login = env.get("LOGIN") or ""
raw_password = env.get("PASSWORD") or ""

print(f"LOGIN: len={len(login)} stripped_differs={raw_login != login} has_at={'@' in login}")
print(f"PASSWORD: len={len(password)} stripped_differs={raw_password != password}")
print(f"env keys: {list(env.keys())}")

url = "https://mapi.itstep.org/v1/mystat/auth/login"


async def probe(name: str, headers: dict, body: dict | str) -> None:
    async with httpx.AsyncClient(timeout=30.0) as c:
        try:
            if isinstance(body, str):
                r = await c.post(url, content=body, headers=headers)
            else:
                r = await c.post(url, json=body, headers=headers)
            print(f"[{name}] {r.status_code} {r.headers.get('content-type')} :: {r.text[:200]!r}")
        except Exception as exc:  # noqa: BLE001
            print(f"[{name}] EXC {type(exc).__name__}: {exc}")


async def main() -> None:
    base = {"Content-Type": "application/json", "X-Language": "uk"}
    await probe("baseline", base, {"login": login, "password": password})
    await probe("ru-lang", {**base, "X-Language": "ru"}, {"login": login, "password": password})
    await probe("en-lang", {**base, "X-Language": "en"}, {"login": login, "password": password})
    await probe("no-lang", {"Content-Type": "application/json"}, {"login": login, "password": password})
    await probe("ru+origin", {**base, "X-Language": "ru", "Origin": "https://mystat.itstep.org", "Referer": "https://mystat.itstep.org/"}, {"login": login, "password": password})
    await probe("bogus-password", base, {"login": login, "password": "definitely-wrong-password"})
    await probe("unknown-login", base, {"login": "no-such-user@example.invalid", "password": password})
    await probe("empty-body", base, {})


asyncio.run(main())
