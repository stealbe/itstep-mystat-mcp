"""Authentication: access token storage, login, refresh.

Confirmed API behaviour (see docs/api-findings.md):

* ``POST /auth/login`` returns a JWT as a plain-text body.
* ``POST /auth/refresh`` with ``Authorization: Bearer <token>`` and ``{}``
  returns a fresh JWT.
* The token lives 30 days (``exp - iat = 2 592 000``).

All token reads/writes go through :class:`Cache`; the token only exists in
memory while an HTTP request is being prepared.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Any

import httpx

from .cache import Cache, utc_from_epoch, utc_now
from .config import ConfigError, Settings

logger = logging.getLogger(__name__)


class AuthError(RuntimeError):
    """Authentication failed and could not be recovered."""


def decode_jwt_claims(token: str) -> dict[str, Any] | None:
    """Read JWT claims without verifying the signature.

    The signature is not a trust boundary here: the token was just received
    from the issuer over TLS, and we only need ``exp`` for housekeeping.
    """
    parts = token.split(".")
    if len(parts) < 2:
        return None
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except Exception:  # noqa: BLE001 - malformed token is not fatal here
        return None
    return claims if isinstance(claims, dict) else None


def token_expiry(token: str) -> str | None:
    claims = decode_jwt_claims(token)
    if not claims:
        return None
    exp = claims.get("exp")
    if isinstance(exp, (int, float)):
        return utc_from_epoch(exp)
    return None


def parse_token_response(response: httpx.Response) -> str:
    """Extract the token from a login/refresh response body.

    The body is plain text, but a JSON string or an object with a token field
    is tolerated as well.
    """
    text = response.text
    stripped = text.strip()
    if not stripped:
        raise AuthError("empty token response")
    content_type = (response.headers.get("content-type") or "").lower()
    if "json" in content_type or stripped[:1] in "{[\"":
        try:
            parsed = json.loads(stripped)
        except ValueError:
            parsed = None
        if isinstance(parsed, str) and parsed.strip():
            return parsed.strip()
        if isinstance(parsed, dict):
            for key in ("access_token", "accessToken", "token"):
                value = parsed.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            data = parsed.get("data")
            if isinstance(data, dict):
                for key in ("access_token", "accessToken", "token"):
                    value = data.get(key)
                    if isinstance(value, str) and value.strip():
                        return value.strip()
    if stripped.startswith('"') and stripped.endswith('"'):
        stripped = stripped[1:-1]
    if " " in stripped.split("\n")[0].strip() or len(stripped) > 8192:
        raise AuthError("unexpected token response shape")
    return stripped


class AuthManager:
    """Owns the token lifecycle and serialises concurrent authorisation."""

    def __init__(self, settings: Settings, cache: Cache, client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._cache = cache
        self._client = client
        self._lock = asyncio.Lock()

    # ---- public API ----------------------------------------------------
    async def get_token(self) -> str:
        """Return a usable token, logging in only when nothing is stored."""
        record = await self._cache.get_auth()
        if record and record["access_token"]:
            return record["access_token"]
        async with self._lock:
            record = await self._cache.get_auth()
            if record and record["access_token"]:
                return record["access_token"]
            return await self._login_locked()

    async def login(self) -> str:
        """Force a new login (``mystat_login`` tool)."""
        async with self._lock:
            return await self._login_locked()

    async def logout(self) -> None:
        await self._cache.delete_auth()
        await self._cache.set_state("auth_last_result", {"event": "logout", "at": utc_now()})
        logger.info("token removed from cache")

    async def recover(self) -> str:
        """Called after a 401: refresh the token, falling back to a full login."""
        async with self._lock:
            record = await self._cache.get_auth()
            if record and record["access_token"]:
                refreshed = await self._refresh_locked(record["access_token"])
                if refreshed:
                    return refreshed
            return await self._login_locked()

    async def status(self) -> dict[str, Any]:
        record = await self._cache.get_auth()
        last = await self._cache.get_state("auth_last_result")
        expires_at = record["expires_at"] if record else None
        expired = None
        if expires_at:
            expired = expires_at <= utc_now()
        return {
            "authenticated": bool(record),
            "token_present": bool(record),
            "created_at": record["created_at"] if record else None,
            "updated_at": record["updated_at"] if record else None,
            "expires_at": expires_at,
            "expired": expired,
            "credentials_configured": self._settings.has_credentials,
            "last_result": json.loads(last) if last else None,
        }

    # ---- internals -----------------------------------------------------
    async def _request_headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "X-Language": self._settings.language,
        }

    async def _login_locked(self) -> str:
        self._settings.require_credentials()
        headers = await self._request_headers()
        payload = {"login": self._settings.login, "password": self._settings.password}
        logger.info("auth: login request")
        try:
            response = await self._client.post("/auth/login", json=payload, headers=headers)
        except httpx.HTTPError as exc:
            await self._record({"event": "login", "ok": False, "error": type(exc).__name__})
            raise AuthError(f"login request failed: {exc}") from exc

        if response.status_code == 401:
            await self._record({"event": "login", "ok": False, "status": 401})
            raise AuthError(
                "login rejected (401): the login/password in .env were not accepted "
                "by MyStat. Check them in a browser at https://mystat.itstep.org"
            )
        if response.status_code >= 400:
            await self._record({"event": "login", "ok": False, "status": response.status_code})
            raise AuthError(f"login failed with HTTP {response.status_code}")

        token = parse_token_response(response)
        await self._store(token)
        await self._record({"event": "login", "ok": True, "status": 200})
        logger.info("auth: login ok, expires_at=%s", token_expiry(token))
        return token

    async def _refresh_locked(self, token: str) -> str | None:
        headers = await self._request_headers()
        headers["Authorization"] = f"Bearer {token}"
        logger.info("auth: refresh request")
        try:
            response = await self._client.post("/auth/refresh", json={}, headers=headers)
        except httpx.HTTPError as exc:
            logger.warning("auth: refresh failed: %s", type(exc).__name__)
            return None
        if response.status_code != 200:
            logger.info("auth: refresh rejected with %s", response.status_code)
            await self._record({"event": "refresh", "ok": False, "status": response.status_code})
            return None
        try:
            new_token = parse_token_response(response)
        except AuthError:
            return None
        await self._store(new_token, preserve_created_at=True)
        await self._record({"event": "refresh", "ok": True, "status": 200})
        logger.info("auth: refresh ok, expires_at=%s", token_expiry(new_token))
        return new_token

    async def _store(self, token: str, preserve_created_at: bool = False) -> None:
        expires_at = token_expiry(token)
        previous = await self._cache.get_auth() if preserve_created_at else None
        await self._cache.save_auth(token, expires_at)
        if previous and preserve_created_at:
            await self._cache.db.execute(
                "UPDATE auth SET created_at = ? WHERE id = 1", (previous["created_at"],)
            )
            await self._cache.db.commit()

    async def _record(self, payload: dict[str, Any]) -> None:
        await self._cache.set_state("auth_last_result", {**payload, "at": utc_now()})


def explain_auth_error(exc: Exception) -> str:
    if isinstance(exc, ConfigError):
        return str(exc)
    if isinstance(exc, AuthError):
        return str(exc)
    return f"authentication error: {exc}"
