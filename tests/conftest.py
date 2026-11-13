from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from mystat.cache import Cache
from mystat.config import Settings

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        base_url="https://mapi.itstep.org/v1/mystat",
        language="uk",
        login="user",
        password="secret",
        cache_dir=tmp_path / "cache",
        database_path=tmp_path / "cache" / "mystat.db",
        sync_limit=1000,
        http_timeout=5.0,
        max_attachment_size=1024 * 1024,
        status_filter="3",
        sort="-hw.time",
    )


@pytest.fixture
async def cache(settings: Settings) -> Cache:
    settings.ensure_directories()
    instance = await Cache(settings.database_path).open()
    try:
        yield instance
    finally:
        await instance.close()


@pytest.fixture
def list_payload() -> dict:
    return json.loads((FIXTURES / "list.json").read_text(encoding="utf-8"))


@pytest.fixture
def detail_payload() -> dict:
    return json.loads((FIXTURES / "detail.json").read_text(encoding="utf-8"))


def make_client(handler, base_url: str = "https://mapi.itstep.org/v1/mystat") -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=base_url, transport=httpx.MockTransport(handler))
