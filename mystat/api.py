"""Single HTTP client for the MyStat API.

Rules (from the specification and observed behaviour):

* every request goes through one client and one header set;
* ``401`` -> one token recovery (refresh, then login) and one repeat;
* ``429`` -> honour ``Retry-After``, never hammer the server;
* ``5xx`` / network errors -> a couple of bounded retries;
* never log Authorization headers, tokens or passwords.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from .auth import AuthError, AuthManager
from .config import Settings

logger = logging.getLogger(__name__)

RETRYABLE_STATUS = {500, 502, 503, 504}
MAX_SERVER_RETRIES = 2
MAX_RATE_LIMIT_RETRIES = 2
MAX_RETRY_AFTER_SECONDS = 30.0


class ApiError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class ApiAuthError(ApiError):
    """Authentication could not be established or restored."""


class ApiNotFoundError(ApiError):
    pass


@dataclass(slots=True)
class PaginatedList:
    """Homework list payload plus pagination headers from the API response."""

    data: dict[str, Any]
    total_count: int | None = None
    page_count: int | None = None
    current_page: int | None = None
    per_page: int | None = None
    x_reset_cache: str | None = None
    x_l_session: str | None = None

    @property
    def items(self) -> list[Any]:
        """The homework items array from the payload."""
        raw = self.data.get("data") or []
        return [item for item in raw if isinstance(item, dict)]


class MyStatAPI:
    def __init__(
        self,
        settings: Settings,
        auth: AuthManager,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self.auth = auth
        self._client = client
        self._owns_client = client is None

    # ---- lifecycle -----------------------------------------------------
    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.settings.base_url,
                timeout=httpx.Timeout(self.settings.http_timeout),
                follow_redirects=True,
                headers={"Accept": "application/json, text/plain, */*"},
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
        self._client = None

    async def __aenter__(self) -> "MyStatAPI":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    # ---- core request --------------------------------------------------
    def _base_headers(self) -> dict[str, str]:
        return {"X-Language": self.settings.language}

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        authenticated: bool = True,
        allow_auth_retry: bool = True,
    ) -> httpx.Response:
        headers = self._base_headers()
        if authenticated:
            headers["Authorization"] = f"Bearer {await self.auth.get_token()}"

        attempt = 0
        auth_retried = False
        rate_limited = 0
        started = time.monotonic()
        while True:
            attempt += 1
            try:
                response = await self.client.request(method, path, params=params, json=json, headers=headers)
            except httpx.HTTPError as exc:
                if attempt <= MAX_SERVER_RETRIES:
                    delay = 0.5 * attempt
                    logger.warning("api: %s %s network error (%s), retry in %.1fs",
                                   method, path, type(exc).__name__, delay)
                    await asyncio.sleep(delay)
                    continue
                raise ApiError(f"{method} {path} failed: {type(exc).__name__}: {exc}") from exc

            status = response.status_code
            logger.info("api: %s %s -> %s in %.0fms", method, path, status,
                        (time.monotonic() - started) * 1000)

            if status == 401 and authenticated and allow_auth_retry and not auth_retried:
                auth_retried = True
                logger.info("api: 401 on %s, recovering token", path)
                try:
                    token = await self.auth.recover()
                except (AuthError, Exception) as exc:  # noqa: BLE001
                    raise ApiAuthError(f"authentication failed: {exc}", status=401) from exc
                headers["Authorization"] = f"Bearer {token}"
                continue

            if status == 401:
                raise ApiAuthError("API rejected the token (401) after re-authentication", status=401)

            if status == 429 and rate_limited < MAX_RATE_LIMIT_RETRIES:
                rate_limited += 1
                delay = _retry_after_seconds(response)
                logger.warning("api: 429 on %s, waiting %.1fs", path, delay)
                await asyncio.sleep(delay)
                continue

            if status in RETRYABLE_STATUS and attempt <= MAX_SERVER_RETRIES:
                delay = 0.5 * attempt
                logger.warning("api: %s on %s, retry in %.1fs", status, path, delay)
                await asyncio.sleep(delay)
                continue

            if status == 404:
                raise ApiNotFoundError(f"{method} {path} returned 404", status=404)
            if status >= 400:
                detail = _safe_body(response)
                raise ApiError(f"{method} {path} -> HTTP {status}: {detail}", status=status)

            return response

    # ---- endpoints -----------------------------------------------------
    async def get_homework_list(
        self,
        *,
        limit: int | None = None,
        status: str | None = None,
        sort: str | None = None,
        page: int | None = None,
    ) -> PaginatedList:
        """Fetch the homework list from the API with pagination info.

        Defaults match the public endpoint contract:
        ``sort=-hw.time``, ``status=1``, ``limit=1000``.

        Pagination headers are captured from the response and exposed
        through the returned :class:`PaginatedList`.
        """
        params: dict[str, Any] = {
            "sort": sort or "-hw.time",
            "status": status if status is not None else "1",
            "limit": limit if limit is not None else 1000,
        }
        if page is not None:
            params["page"] = page
        response = await self.request("GET", "/od/homework/list", params=params)
        payload = response.json()
        if not isinstance(payload, dict):
            raise ApiError("unexpected homework list payload shape")
        headers = response.headers
        return PaginatedList(
            data=payload,
            total_count=_as_int_header(headers.get("X-Pagination-Total-Count")),
            page_count=_as_int_header(headers.get("X-Pagination-Page-Count")),
            current_page=_as_int_header(headers.get("X-Pagination-Current-Page")),
            per_page=_as_int_header(headers.get("X-Pagination-Per-Page")),
            x_reset_cache=headers.get("x-reset-cache"),
            x_l_session=headers.get("X-L-Session"),
        )

    async def get_homework_detail(self, homework_id: str | int) -> Any:
        response = await self.request("GET", f"/od/homework/{homework_id}")
        return response.json()


def _retry_after_seconds(response: httpx.Response) -> float:
    raw = response.headers.get("retry-after")
    delay = 2.0
    if raw:
        try:
            delay = float(raw.strip())
        except ValueError:
            delay = 2.0
    return max(0.0, min(delay, MAX_RETRY_AFTER_SECONDS))


def _as_int_header(value: str | None) -> int | None:
    """Parse an int from a response header, returning ``None`` when absent."""
    if value is None:
        return None
    try:
        return int(value.strip())
    except (ValueError, AttributeError):
        return None


def _safe_body(response: httpx.Response) -> str:
    """Short error body; never echoes credentials because none are in it."""
    text = (response.text or "").strip().replace("\n", " ")
    return text[:200] if text else "<empty>"
