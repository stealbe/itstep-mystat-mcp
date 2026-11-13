"""Extract login-related code fragments from the Angular bundle."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import httpx

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
OUT = Path(__file__).resolve().parents[1] / "cache" / "raw" / "bundle_findings.txt"

PATTERNS = {
    "login-method": r".{200}endpointLogin.{400}",
    "password-payload": r".{160}password:.{220}",
    "refresh": r".{160}auth/refresh.{300}",
    "hosts": r"https?://[a-z0-9.\-]*itstep\.org[a-z0-9/._\-]*",
    "api-base": r".{80}apiBase.{160}|.{80}apiUrl.{160}|.{60}baseUrl.{120}",
    "refresh-token": r".{120}refresh_token.{200}",
    "headers": r".{60}X-Language.{160}",
}


async def main() -> None:
    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True, headers={"User-Agent": UA}) as c:
        r = await c.get("https://mystat.itstep.org/main-PTSNAXV7.js")
        text = r.text
    lines: list[str] = []
    for name, pattern in PATTERNS.items():
        matches = re.findall(pattern, text)
        lines.append(f"===== {name}: {len(matches)} matches =====")
        for m in matches[:6]:
            cleaned = " ".join(m.split()) if isinstance(m, str) else " ".join(m)
            lines.append(f"  > {cleaned[:520]}")
        lines.append("")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines)[:6000])


asyncio.run(main())
