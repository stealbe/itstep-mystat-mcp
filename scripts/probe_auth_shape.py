"""Second probe: field names, swapped creds, alternative encodings, OpenAPI."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
env = dotenv_values(ROOT / ".env")
login = (env.get("LOGIN") or "").strip()
password = (env.get("PASSWORD") or "").strip()

BASE = "https://mapi.itstep.org/v1/mystat"
H = {"Content-Type": "application/json", "X-Language": "uk"}


async def main() -> None:
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as c:
        async def post(name: str, body, headers=H):
            r = await c.post(f"{BASE}/auth/login", json=body, headers=headers)
            print(f"[{name}] {r.status_code} :: {r.text[:180]!r}")

        await post("only-password-field", {"password": password})
        await post("only-login-field", {"login": login})
        await post("swapped", {"login": password, "password": login})
        await post("username-field", {"username": login, "password": password})
        await post("email-field", {"email": login, "password": password})
        await post("upper-Login", {"Login": login, "Password": password})

        # form-encoded attempt
        r = await c.post(
            f"{BASE}/auth/login",
            data={"login": login, "password": password},
            headers={"X-Language": "uk"},
        )
        print(f"[form-encoded] {r.status_code} :: {r.text[:180]!r}")

        # auth endpoints discovery
        for path in ("/openapi.json", "/docs", "/swagger.json", "/api-docs", "/auth/me"):
            try:
                rr = await (c.get if path != "/auth/me" else c.post)(
                    f"{BASE}{path}", headers={"X-Language": "uk"}
                )
                print(f"[GET {path}] {rr.status_code} {rr.headers.get('content-type')} len={len(rr.text)}")
            except Exception as exc:  # noqa: BLE001
                print(f"[GET {path}] EXC {type(exc).__name__}: {exc}")

        # root of api host
        for url in ("https://mapi.itstep.org/", "https://mapi.itstep.org/v1/mystat"):
            try:
                rr = await c.get(url, headers={"X-Language": "uk"})
                print(f"[{url}] {rr.status_code} {rr.headers.get('content-type')} :: {rr.text[:120]!r}")
            except Exception as exc:  # noqa: BLE001
                print(f"[{url}] EXC {type(exc).__name__}: {exc}")


asyncio.run(main())
