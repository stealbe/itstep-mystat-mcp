"""Check whether the account is email-based: compare error shapes for login vs email payloads."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
env = dotenv_values(ROOT / ".env")
login = (env.get("LOGIN") or "").strip()
password = (env.get("PASSWORD") or "").strip()

URL = "https://mapi.itstep.org/v1/mystat/auth/login"
H = {"Content-Type": "application/json", "X-Language": "uk"}


async def main() -> None:
    async with httpx.AsyncClient(timeout=30.0) as c:
        async def post(name: str, body: dict):
            r = await c.post(URL, json=body, headers=H)
            print(f"[{name}] {r.status_code} :: {r.text[:150]!r}")

        await post("login-again", {"login": login, "password": password})
        await post("email-plausible", {"email": f"{login}@itstep.org", "password": password})
        await post("email-and-login", {"login": login, "email": f"{login}@itstep.org", "password": password})
        await post("login-lowercase", {"login": login.lower(), "password": password})
        await post("login-upper", {"login": login.upper(), "password": password})
        await post("wrong-pass-known-login", {"login": login, "password": password + "x"})
        await post("password-case", {"login": login, "password": password.upper()})


asyncio.run(main())
