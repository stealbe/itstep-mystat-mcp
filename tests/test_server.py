from __future__ import annotations

import pytest

import server


async def test_server_registers_all_tools():
    names = {tool.name for tool in await server.mcp.list_tools()}
    assert {
        "mystat_login",
        "mystat_auth_status",
        "mystat_logout",
        "homework_sync",
        "homework_list",
        "homework_get",
        "homework_search",
        "homework_read_file",
        "homework_cache_status",
    } <= names


async def test_lifespan_and_offline_tools(monkeypatch, tmp_path):
    monkeypatch.setenv("CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "cache" / "mystat.db"))

    async with server.lifespan(server.mcp):
        status = await server.homework_cache_status()
        assert status["homework"]["total"] == 0
        assert status["full_text_index"] is True
        assert status["token_present"] is False

        auth_status = await server.mystat_auth_status()
        assert auth_status["token_present"] is False
        assert "access_token" not in auth_status

        listing = await server.homework_list()
        assert listing["items"] == []
