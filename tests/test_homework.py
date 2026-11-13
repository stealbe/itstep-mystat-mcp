from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from mystat.homework import (
    HomeworkService,
    build_content_markdown,
    normalize_detail,
    stable_hash,
)

from conftest import make_client


# ------------------------------------------------------------- normalising ---
def test_normalize_detail_handles_numeric_key(detail_payload):
    main, related = normalize_detail(detail_payload)
    assert main["id"] == 502191
    assert main["theme"]
    assert isinstance(related, list) and related


def test_normalize_detail_handles_list_and_bare_object(detail_payload):
    main = detail_payload["0"]
    assert normalize_detail([main])[0]["id"] == main["id"]
    assert normalize_detail(main)[0]["id"] == main["id"]
    assert normalize_detail(None) == ({}, [])


def test_stable_hash_ignores_key_order():
    assert stable_hash({"a": 1, "b": [1, 2]}) == stable_hash({"b": [1, 2], "a": 1})
    assert stable_hash({"a": 1}) != stable_hash({"a": 2})


def test_markdown_contains_task_text_and_attachments():
    main = {
        "theme": "Network Layer",
        "comment": "Прочитать главу 3",
        "name_spec": "Мережі",
        "completion_time": "2026-11-20",
        "homework_stud": {"mark": 10},
    }
    attachment = {
        "original_name": "task.pdf",
        "role": "task",
        "download_status": "downloaded",
        "file_size": 1234,
        "mime_type": "application/pdf",
        "local_path": "homework/1/attachments/task.pdf",
    }
    markdown = build_content_markdown("1", main, [], [attachment])
    assert "Network Layer" in markdown
    assert "Прочитать главу 3" in markdown
    assert "task.pdf" in markdown
    assert "**Оценка:** 10" in markdown


# --------------------------------------------------------------------- sync ---
class FakeApi:
    """Stands in for MyStatAPI: no network, deterministic payloads."""

    def __init__(self, items: list[dict], details: dict[str, Any], client: httpx.AsyncClient) -> None:
        self._items = items
        self._details = details
        self.client = client
        self.detail_calls: list[str] = []
        self.fail_for: set[str] = set()

    async def get_homework_list(self, **_: Any) -> dict:
        return {"data": self._items, "_meta": {"totalCount": len(self._items)}}

    async def get_homework_detail(self, homework_id: str) -> Any:
        self.detail_calls.append(str(homework_id))
        if str(homework_id) in self.fail_for:
            from mystat.api import ApiError

            raise ApiError("boom", status=500)
        return self._details[str(homework_id)]


def _item(homework_id: int, theme: str = "Тема", **extra: Any) -> dict:
    base = {
        "id": homework_id,
        "id_spec": 1,
        "id_teach": 2,
        "id_group": 3,
        "fio_teach": "Преподаватель",
        "theme": theme,
        "completion_time": "2026-11-20",
        "creation_time": "2026-11-10",
        "overdue_time": "2026-11-21",
        "file_path": None,
        "comment": "",
        "name_spec": "Предмет",
        "status": 3,
        "homework_stud": {"id": 1, "mark": None, "stud_answer": ""},
        "cover_image": None,
        "homework_comment": {"text_comment": "", "attachment": None, "attachment_path": None},
    }
    base.update(extra)
    return base


def _detail(item: dict, comment: str) -> dict:
    main = {**item, "comment": comment}
    return {"0": main, "relatedHomeworks": []}


async def test_sync_creates_rows_and_markdown(settings, cache):
    items = [_item(1, "Первая"), _item(2, "Вторая")]
    details = {"1": _detail(items[0], "Текст первого"), "2": _detail(items[1], "Текст второго")}
    client = make_client(lambda request: httpx.Response(200, json={}))
    api = FakeApi(items, details, client)
    service = HomeworkService(settings, cache, api)

    summary = await service.sync(download_attachments=False)
    assert summary["found"] == 2
    assert summary["new"] == 2
    assert summary["error_count"] == 0

    row = await cache.get_homework("1")
    assert row["title"] == "Первая"
    assert row["content_hash"]
    content_file = settings.homework_dir / "1" / "content.md"
    assert content_file.is_file()
    assert "Текст первого" in content_file.read_text(encoding="utf-8")

    listing = await service.list()
    assert {item["id"] for item in listing["items"]} == {"1", "2"}
    await client.aclose()


async def test_sync_second_run_is_unchanged(settings, cache):
    items = [_item(1)]
    details = {"1": _detail(items[0], "Текст")}
    client = make_client(lambda request: httpx.Response(200, json={}))
    api = FakeApi(items, details, client)
    service = HomeworkService(settings, cache, api)

    await service.sync(download_attachments=False)
    second = await service.sync(download_attachments=False)
    assert second["unchanged"] == 1
    assert second["new"] == 0
    assert second["updated"] == 0
    await client.aclose()


async def test_sync_detects_changed_text(settings, cache):
    items = [_item(1)]
    client = make_client(lambda request: httpx.Response(200, json={}))
    api = FakeApi(items, {"1": _detail(items[0], "Старый текст")}, client)
    service = HomeworkService(settings, cache, api)
    await service.sync(download_attachments=False)

    api._details["1"] = _detail(items[0], "Новый текст")
    summary = await service.sync(download_attachments=False)
    assert summary["updated"] == 1
    row = await cache.get_homework("1")
    assert "Новый текст" in row["content_markdown"]
    await client.aclose()


async def test_sync_marks_disappeared_homework_missing(settings, cache):
    items = [_item(1), _item(2)]
    client = make_client(lambda request: httpx.Response(200, json={}))
    api = FakeApi(items, {"1": _detail(items[0], "a"), "2": _detail(items[1], "b")}, client)
    service = HomeworkService(settings, cache, api)
    await service.sync(download_attachments=False)

    api._items = [items[0]]
    summary = await service.sync(download_attachments=False)
    assert summary["marked_missing"] == 1
    row = await cache.get_homework("2")
    assert row["is_missing"] == 1
    visible = await service.list()
    assert [item["id"] for item in visible["items"]] == ["1"]
    with_missing = await service.list(include_missing=True)
    assert len(with_missing["items"]) == 2
    await client.aclose()


async def test_one_broken_homework_does_not_stop_sync(settings, cache):
    items = [_item(1), _item(2)]
    client = make_client(lambda request: httpx.Response(200, json={}))
    api = FakeApi(items, {"1": _detail(items[0], "ok"), "2": _detail(items[1], "ok")}, client)
    api.fail_for = {"2"}
    service = HomeworkService(settings, cache, api)

    summary = await service.sync(download_attachments=False)
    assert summary["found"] == 2
    assert summary["error_count"] == 1
    assert (await cache.get_homework("1"))["content_markdown"]
    assert (await cache.get_homework("2"))["sync_error"]
    await client.aclose()


async def test_search_and_read_file(settings, cache):
    items = [_item(1)]
    client = make_client(lambda request: httpx.Response(200, json={}))
    api = FakeApi(items, {"1": _detail(items[0], "Лабораторная про Neural Networks")}, client)
    service = HomeworkService(settings, cache, api)
    await service.sync(download_attachments=False)

    found = await service.search("Neural")
    assert found["full_text_index"] is True
    assert any(hit["homework_id"] == "1" for hit in found["results"])

    got = await service.get("1")
    assert "Neural Networks" in got["task_text"]

    with pytest.raises(KeyError):
        await service.get("404")
    with pytest.raises(ValueError):
        await service.read_file("1", "../mystat.db")
    await client.aclose()


async def test_read_file_serves_extracted_text(settings, cache):
    items = [_item(1)]
    client = make_client(lambda request: httpx.Response(200, json={}))
    api = FakeApi(items, {"1": _detail(items[0], "task")}, client)
    service = HomeworkService(settings, cache, api)
    await service.sync(download_attachments=False)

    target = settings.homework_dir / "1" / "attachments" / "notes.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("полезный текст", encoding="utf-8")
    await cache.upsert_attachment(
        homework_id="1", role="task", source_url="https://fs1.itstep.org/api/v1/files/x",
        original_name="notes.txt", local_path="homework/1/attachments/notes.txt",
        mime_type="text/plain", file_size=20, download_status="downloaded",
        extracted_text="полезный текст",
    )

    result = await service.read_file("1", "notes.txt")
    assert result["content"] == "полезный текст"
    assert result["kind"] == "extracted_text"
    await client.aclose()
