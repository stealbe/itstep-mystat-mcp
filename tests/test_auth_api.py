from __future__ import annotations

import asyncio

import httpx
import pytest

from mystat.api import ApiAuthError, ApiError, ApiNotFoundError, MyStatAPI
from mystat.auth import AuthError, AuthManager, decode_jwt_claims, parse_token_response, token_expiry

from conftest import make_client


def _jwt(exp: int = 1797093447) -> str:
    import base64
    import json

    def enc(obj: dict) -> str:
        raw = json.dumps(obj).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{enc({'alg': 'HS256', 'typ': 'JWT'})}.{enc({'exp': exp, 'iat': exp - 2592000})}.sig"


# --------------------------------------------------------------- token utils ---
def test_decode_jwt_claims_and_expiry():
    token = _jwt(1797093447)
    claims = decode_jwt_claims(token)
    assert claims and claims["exp"] == 1797093447
    assert token_expiry(token).startswith("2026-12")


def test_decode_jwt_claims_rejects_garbage():
    assert decode_jwt_claims("not-a-jwt") is None
    assert token_expiry("not-a-jwt") is None


def test_parse_token_response_plain_text():
    response = httpx.Response(200, text="abc.def.ghi", headers={"content-type": "text/html"})
    assert parse_token_response(response) == "abc.def.ghi"


def test_parse_token_response_json_object():
    response = httpx.Response(200, json={"access_token": "tok"}, headers={"content-type": "application/json"})
    assert parse_token_response(response) == "tok"


def test_parse_token_response_json_string():
    response = httpx.Response(200, json="tok", headers={"content-type": "application/json"})
    assert parse_token_response(response) == "tok"


# -------------------------------------------------------------------- login ---
async def test_login_stores_token_and_status_hides_it(settings, cache):
    token = _jwt()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/auth/login")
        return httpx.Response(200, text=token, headers={"content-type": "text/html"})

    client = make_client(handler)
    auth = AuthManager(settings, cache, client)
    stored = await auth.login()
    assert stored == token

    record = await cache.get_auth()
    assert record["access_token"] == token
    assert record["expires_at"] and record["expires_at"].startswith("2026-12")

    status = await auth.status()
    assert status["authenticated"] is True
    assert "token" not in str(status).lower().replace("token_present", "").replace("token_opened", "")

    # second call reuses the cached token instead of logging in again
    assert await auth.get_token() == token
    await client.aclose()


async def test_login_rejected_gives_actionable_error(settings, cache):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="Unauthorized: error_unauthorized")

    client = make_client(handler)
    auth = AuthManager(settings, cache, client)
    with pytest.raises(AuthError) as excinfo:
        await auth.login()
    assert "401" in str(excinfo.value)
    assert await cache.get_auth() is None
    await client.aclose()


async def test_login_without_credentials(settings):
    from dataclasses import replace

    bare = replace(settings, login="", password="")
    client = make_client(lambda request: httpx.Response(200, text="x"))
    from mystat.cache import Cache

    acync_cache = await Cache(settings.database_path).open()
    auth = AuthManager(bare, acync_cache, client)
    with pytest.raises(Exception) as excinfo:
        await auth.get_token()
    assert "MYSTAT_LOGIN" in str(excinfo.value)
    await acync_cache.close()
    await client.aclose()


async def test_recover_refreshes_before_logging_in(settings, cache):
    old, new = _jwt(1790000000), _jwt(1797093447)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/auth/refresh"):
            assert request.headers["authorization"] == f"Bearer {old}"
            return httpx.Response(200, text=new)
        return httpx.Response(200, text=_jwt(1800000000))

    client = make_client(handler)
    auth = AuthManager(settings, cache, client)
    await cache.save_auth(old, "2026-11-01T00:00:00+00:00")
    recovered = await auth.recover()
    assert recovered == new
    assert calls == ["/v1/mystat/auth/refresh"]
    assert (await cache.get_auth())["access_token"] == new
    await client.aclose()


async def test_recover_falls_back_to_login_when_refresh_fails(settings, cache):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/refresh"):
            return httpx.Response(401, text="Unauthorized: Token is empty")
        return httpx.Response(200, text=_jwt())

    client = make_client(handler)
    auth = AuthManager(settings, cache, client)
    await cache.save_auth("stale", None)
    assert await auth.recover() == _jwt()
    await client.aclose()


# ---------------------------------------------------------------- api retry ---
async def test_api_recovers_once_from_401(settings, cache):
    token = _jwt()
    state = {"api_calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/refresh"):
            return httpx.Response(200, text=token)
        state["api_calls"] += 1
        if state["api_calls"] == 1:
            return httpx.Response(401, text="Unauthorized")
        return httpx.Response(200, json={"data": [], "_meta": {}})

    client = make_client(handler)
    auth = AuthManager(settings, cache, client)
    await cache.save_auth("stale", None)
    api = MyStatAPI(settings, auth, client)
    payload = await api.get_homework_list()
    assert payload["data"] == []
    assert state["api_calls"] == 2
    await client.aclose()


async def test_api_gives_up_after_second_401(settings, cache):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(("/auth/refresh", "/auth/login")):
            return httpx.Response(200, text=_jwt())
        return httpx.Response(401, text="Unauthorized")

    client = make_client(handler)
    auth = AuthManager(settings, cache, client)
    api = MyStatAPI(settings, auth, client)
    with pytest.raises(ApiAuthError):
        await api.get_homework_list()
    await client.aclose()


async def test_api_retries_5xx_then_succeeds(settings, cache, monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/login"):
            return httpx.Response(200, text=_jwt())
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(503, text="down")
        return httpx.Response(200, json={"data": [1], "_meta": {}})

    client = make_client(handler)
    auth = AuthManager(settings, cache, client)
    api = MyStatAPI(settings, auth, client)
    assert (await api.get_homework_list())["data"] == [1]
    assert attempts["n"] == 3
    await client.aclose()


async def test_api_honours_retry_after(settings, cache, monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    delays: list[float] = []
    real_sleep = _no_sleep

    async def tracking_sleep(seconds: float) -> None:
        delays.append(seconds)
        await real_sleep(seconds)

    monkeypatch.setattr(asyncio, "sleep", tracking_sleep)
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/login"):
            return httpx.Response(200, text=_jwt())
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "7"})
        return httpx.Response(200, json={"data": [], "_meta": {}})

    client = make_client(handler)
    api = MyStatAPI(settings, AuthManager(settings, cache, client), client)
    await api.get_homework_list()
    assert 7.0 in delays
    await client.aclose()


async def test_api_404_and_error_statuses(settings, cache):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/login"):
            return httpx.Response(200, text=_jwt())
        if request.url.path.endswith("/od/homework/1"):
            return httpx.Response(404, json={"message": "not found"})
        return httpx.Response(403, json={"message": "forbidden"})

    client = make_client(handler)
    api = MyStatAPI(settings, AuthManager(settings, cache, client), client)
    with pytest.raises(ApiNotFoundError):
        await api.get_homework_detail("1")
    with pytest.raises(ApiError) as excinfo:
        await api.get_homework_detail("2")
    assert excinfo.value.status == 403
    await client.aclose()


async def _no_sleep(seconds: float = 0.0) -> None:  # pragma: no cover - helper
    return None
