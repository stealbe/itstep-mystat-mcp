"""MCP server (stdio) exposing MyStat homework tools.

``stdout`` belongs to the MCP protocol: all diagnostics go to stderr and to
``cache/logs/mystat.log``. Never use ``print()`` here.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from mystat.api import ApiAuthError, ApiError, MyStatAPI
from mystat.auth import AuthError, AuthManager
from mystat.cache import Cache
from mystat.config import ConfigError, Settings
from mystat.homework import HomeworkService

logger = logging.getLogger("mystat")


def configure_logging(settings: Settings) -> None:
    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    stream = logging.StreamHandler(stream=sys.stderr)
    stream.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(stream)

    file_handler = logging.handlers.RotatingFileHandler(
        settings.logs_dir / "mystat.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s [%(process)d]: %(message)s")
    )
    root.addHandler(file_handler)

    # httpx logs full request URLs at INFO; keep it quiet about them.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


@dataclass
class AppContext:
    settings: Settings
    cache: Cache
    auth: AuthManager
    api: MyStatAPI
    homework: HomeworkService


_app: AppContext | None = None


def app() -> AppContext:
    if _app is None:  # pragma: no cover - lifespan always runs first
        raise ToolError("server is not initialised")
    return _app


@asynccontextmanager
async def lifespan(server: MCPServer) -> AsyncIterator[AppContext]:
    global _app
    settings = Settings.from_env()
    settings.ensure_directories()
    configure_logging(settings)
    logger.info("starting itstep-mystat-mcp, cache=%s", settings.cache_dir)

    cache = await Cache(settings.database_path).open()
    client = httpx.AsyncClient(
        base_url=settings.base_url,
        timeout=httpx.Timeout(settings.http_timeout),
        follow_redirects=True,
    )
    auth = AuthManager(settings, cache, client)
    api = MyStatAPI(settings, auth, client)
    _app = AppContext(settings=settings, cache=cache, auth=auth, api=api,
                      homework=HomeworkService(settings, cache, api))
    try:
        yield _app
    finally:
        logger.info("shutting down")
        await client.aclose()
        await cache.close()
        _app = None


mcp = MCPServer(
    name="itstep-mystat",
    version="0.1.0",
    instructions=(
        "Access to ITStep MyStat homework. Call homework_sync once to mirror the "
        "account, then use homework_list / homework_get / homework_search / "
        "homework_read_file. Tokens and passwords are never returned."
    ),
    lifespan=lifespan,
    log_level="INFO",
)


def _fail(exc: Exception) -> ToolError:
    """Convert internal errors into tool errors without leaking secrets."""
    if isinstance(exc, (ConfigError, AuthError, ApiAuthError)):
        return ToolError(str(exc))
    if isinstance(exc, (ApiError,)):
        return ToolError(str(exc))
    if isinstance(exc, KeyError):
        return ToolError(str(exc).strip("'\""))
    if isinstance(exc, ValueError):
        return ToolError(str(exc))
    logger.exception("unexpected tool failure")
    return ToolError(f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------- auth tools ---
@mcp.tool(name="mystat_login", description="Force a MyStat login and replace the stored token. Credentials come from .env; the token itself is never returned.")
async def mystat_login() -> dict[str, Any]:
    ctx = app()
    try:
        await ctx.auth.login()
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc
    status = await ctx.auth.status()
    return {"ok": True, "message": "token stored", **status}


@mcp.tool(name="mystat_auth_status", description="Show whether a token is cached, when it was obtained and whether its expiry is known. Never reveals the token.")
async def mystat_auth_status() -> dict[str, Any]:
    ctx = app()
    return await ctx.auth.status()


@mcp.tool(name="mystat_logout", description="Delete the cached token. The next authenticated call will log in again.")
async def mystat_logout() -> dict[str, Any]:
    ctx = app()
    await ctx.auth.logout()
    return {"ok": True, "message": "token removed from cache"}


# ----------------------------------------------------------- homework tools ---
@mcp.tool(name="homework_sync", description="Mirror the MyStat homework list into the local cache, optionally downloading attachments.")
async def homework_sync(force: bool = False, download_attachments: bool = True) -> dict[str, Any]:
    ctx = app()
    try:
        return await ctx.homework.sync(force=force, download_attachments=download_attachments)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc


@mcp.tool(name="homework_list", description="List cached homework (short form, newest deadline first).")
async def homework_list(
    limit: int = 20,
    offset: int = 0,
    status: str | None = None,
    include_missing: bool = False,
) -> dict[str, Any]:
    ctx = app()
    try:
        return await ctx.homework.list(limit=limit, offset=offset, status=status,
                                       include_missing=include_missing)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc


@mcp.tool(name="homework_get", description="Return one cached homework with task text, attachments and processing state.")
async def homework_get(homework_id: str) -> dict[str, Any]:
    ctx = app()
    try:
        return await ctx.homework.get(homework_id)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc


@mcp.tool(name="homework_search", description="Full-text search over homework metadata, task text and extracted attachment text.")
async def homework_search(query: str, limit: int = 10) -> dict[str, Any]:
    ctx = app()
    try:
        return await ctx.homework.search(query, limit=limit)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc


@mcp.tool(name="homework_read_file", description="Read a cached attachment (extracted text when available) by its name inside one homework.")
async def homework_read_file(homework_id: str, file: str) -> dict[str, Any]:
    ctx = app()
    try:
        return await ctx.homework.read_file(homework_id, file)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc


@mcp.tool(name="homework_cache_status", description="Cache statistics: homework counts, attachments, sizes, last sync, auth and index state.")
async def homework_cache_status() -> dict[str, Any]:
    ctx = app()
    try:
        return await ctx.homework.cache_status()
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
