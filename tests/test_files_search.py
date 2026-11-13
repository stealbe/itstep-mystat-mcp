from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
import pytest

from mystat.attachments import (
    AttachmentRef,
    DownloadError,
    UnsafeUrlError,
    collect_attachment_refs,
    download_attachment,
    parse_content_disposition,
    sanitize_filename,
    sniff_mime,
    validate_url,
)
from mystat.search import build_fts_query, parse_query

from conftest import make_client

PUBLIC = ["93.184.216.34"]


async def _resolver(_host: str) -> list[str]:
    return PUBLIC


# ------------------------------------------------------------------- urls ----
async def test_validate_url_accepts_myastat_file_url():
    url = "https://fs3.itstep.org/api/v1/files/abc123"
    assert await validate_url(url, resolver=_resolver) == url


@pytest.mark.parametrize(
    "url",
    [
        "http://fs3.itstep.org/api/v1/files/abc",
        "https://evil.example.com/api/v1/files/abc",
        "https://fs3.itstep.org/other/abc",
        "https://fs3.itstep.org.evil.com/api/v1/files/abc",
        "https://127.0.0.1/api/v1/files/abc",
        "https://fs3.itstep.org:8443/api/v1/files/abc",
    ],
)
async def test_validate_url_rejects_dangerous_targets(url):
    with pytest.raises(UnsafeUrlError):
        await validate_url(url, resolver=_resolver)


async def test_validate_url_rejects_private_dns_answer():
    async def private(_host: str) -> list[str]:
        return ["10.0.0.5"]

    with pytest.raises(UnsafeUrlError):
        await validate_url("https://fs3.itstep.org/api/v1/files/abc", resolver=private)


# ------------------------------------------------------------------ names ----
@pytest.mark.parametrize(
    "raw, expected",
    [
        ("../../etc/passwd", "passwd"),
        ("C:\\Windows\\system32\\cmd.exe", "cmd.exe"),
        ("CON.txt", "_CON.txt"),
        ("bad\x00name.txt", "badname.txt"),
        ('weird:*?"<>|name.pdf', "weird_______name.pdf"),
        ("..", "attachment"),
    ],
)
def test_sanitize_filename(raw, expected):
    assert sanitize_filename(raw) == expected


def test_sanitize_filename_limits_length():
    assert len(sanitize_filename("a" * 400 + ".pdf")) <= 120


def test_parse_content_disposition_prefers_rfc5987():
    header = (
        'attachment; filename="DZ.pdf"; '
        "filename*=utf-8''%D0%94%D0%97_2026.pdf"
    )
    assert parse_content_disposition(header) == "ДЗ_2026.pdf"
    assert parse_content_disposition('attachment; filename="plain.docx"') == "plain.docx"
    assert parse_content_disposition(None) is None


# ------------------------------------------------------------------ sniff ----
def test_sniff_mime_from_magic_bytes():
    assert sniff_mime(b"%PDF-1.7", "text/plain", "x.bin") == "application/pdf"
    assert sniff_mime(b"\x89PNG\r\n\x1a\n", None, "x") == "image/png"
    assert sniff_mime(b"<!DOCTYPE html><html>", None, "x") == "text/html"
    assert sniff_mime(b"PK\x03\x04....", None, "doc.docx").endswith("wordprocessingml.document")


# ------------------------------------------------------------- references ----
def test_collect_attachment_refs_covers_all_fields(detail_payload):
    main = detail_payload["0"]
    refs = collect_attachment_refs(main)
    roles = {ref.role for ref in refs}
    assert "task" in roles
    assert all(ref.url.startswith("https://") for ref in refs)


def test_collect_attachment_refs_ignores_non_http():
    refs = collect_attachment_refs({"file_path": "not-a-url", "cover_image": None})
    assert refs == []


# --------------------------------------------------------------- download ----
async def test_download_writes_file_and_metadata(settings, cache, tmp_path):
    payload = b"%PDF-1.4 fake pdf body"
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(
            200,
            content=payload,
            headers={
                "content-type": "application/pdf",
                "content-disposition": "attachment; filename*=utf-8''%D0%94%D0%97.pdf",
            },
        )

    client = make_client(handler)
    ref = AttachmentRef(role="task", url="https://fs3.itstep.org/api/v1/files/abc")
    dest = settings.homework_dir / "1" / "attachments"

    result = await download_attachment(
        client, ref, dest, cache_dir=settings.cache_dir,
        max_size=settings.max_attachment_size, resolver=_resolver,
    )

    assert result.path.is_file()
    assert result.path.read_bytes() == payload
    assert result.original_name == "ДЗ.pdf"
    assert result.mime_type == "application/pdf"
    assert result.sha256 == hashlib.sha256(payload).hexdigest()
    assert result.relative_path.startswith("homework")
    assert not list(dest.glob(".partial-*"))
    await client.aclose()


async def test_download_enforces_size_limit(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 5000, headers={"content-type": "text/plain"})

    client = make_client(handler)
    ref = AttachmentRef(role="task", url="https://fs3.itstep.org/api/v1/files/big")
    with pytest.raises(DownloadError):
        await download_attachment(
            client, ref, settings.homework_dir / "1", cache_dir=settings.cache_dir,
            max_size=1024, resolver=_resolver,
        )
    await client.aclose()


async def test_download_does_not_duplicate_identical_file(settings):
    payload = b"hello"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload, headers={"content-type": "text/plain",
                                                           "content-disposition": 'attachment; filename="a.txt"'})

    client = make_client(handler)
    ref = AttachmentRef(role="task", url="https://fs3.itstep.org/api/v1/files/a")
    dest = settings.homework_dir / "1" / "attachments"
    first = await download_attachment(client, ref, dest, cache_dir=settings.cache_dir,
                                      max_size=1024, resolver=_resolver)
    second = await download_attachment(client, ref, dest, cache_dir=settings.cache_dir,
                                       max_size=1024, resolver=_resolver)
    assert first.path == second.path
    assert len(list(dest.iterdir())) == 1
    await client.aclose()


# ------------------------------------------------------------ fts queries ----
def test_build_fts_query_is_safe():
    assert build_fts_query('C++ -исключение "кавычки" OR') == '"C++" "-исключение" "кавычки"'
    assert build_fts_query("   ") == ""
    assert "OR" not in build_fts_query("OR OR")
    query = parse_query("a  bb cc")
    assert query.terms == ("bb", "cc")


async def test_cache_search_returns_snippet(cache):
    await cache.reindex_homework("42", [("task", "Лабораторная работа по нейронным сетям")])
    hits = await cache.search("нейронным")
    assert hits and hits[0]["homework_id"] == "42"
    assert hits[0]["snippet"]
    await cache.reindex_homework("42", [])
    assert await cache.search("нейронным") == []
