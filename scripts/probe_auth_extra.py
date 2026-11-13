"""Third probe: extra fields/headers and alternative auth endpoints."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
env = dotenv_values(ROOT / ".env")
login = (env.get("LOGIN") or "").strip()
password = (env.get("PASSWORD") or "").strip()

H = {"Content-Type": "application/json", "X-Language": "uk", "Accept": "application/json, text/plain, */*"}


async def main() -> None:
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as c:
        async def post(name: str, url: str, body: dict, headers: dict | None = None):
            try:
                r = await c.post(url, json=body, headers=headers or H)
                extra = {k: v for k, v in r.headers.items() if k.lower() in ("www-authenticate", "x-ratelimit-remaining", "retry-after")}
                print(f"[{name}] {r.status_code} :: {r.text[:140]!r} :: {extra or ''}")
                return r
            except Exception as exc:  # noqa: BLE001
                print(f"[{name}] EXC {type(exc).__name__}: {exc}")
                return None

        base = "https://mapi.itstep.org/v1/mystat/auth/login"
        await post("x-userid", base, {"login": login, "password": password}, {**H, "X-Userid": "7307261"})
        await post("remember", base, {"login": login, "password": password, "remember": True})
        await post("grant_type", base, {"login": login, "password": password, "grant_type": "password"})
        await post("type-login", base, {"login": login, "password": password, "type": "login"})
        await post("device", base, {"login": login, "password": password, "device": "web"})
        await post("with-origin", base, {"login": login, "password": password},
                   {**H, "Origin": "https://mystat.itstep.org", "Referer": "https://mystat.itstep.org/",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"})
        # alternative endpoints
        await post("v1/auth/login", "https://mapi.itstep.org/v1/auth/login", {"login": login, "password": password})
        await post("web-auth-login", "https://mystat.itstep.org/auth/login", {"login": login, "password": password})
        await post("web-api-login", "https://mystat.itstep.org/api/auth/login", {"login": login, "password": password})

        # headers of a plain 401
        r = await c.post(base, json={"login": login, "password": password}, headers=H)
        print("--- 401 headers ---")
        for k, v in r.headers.items():
            print(f"  {k}: {v}")


asyncio.run(main())
