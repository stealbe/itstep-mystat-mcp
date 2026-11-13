"""Homework normalisation, sync and read model."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .api import ApiError, ApiNotFoundError, MyStatAPI
from .attachments import (
    AttachmentRef,
    collect_attachment_refs,
    download_attachment,
)
from .cache import Cache
from .config import Settings
from .extraction import extract_text

logger = logging.getLogger(__name__)

SYNC_CONCURRENCY = 4


# ------------------------------------------------------------ normalising ---
def normalize_detail(payload: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return (homework object, related homeworks) from any observed shape.

    Observed: ``{"0": {...}, "relatedHomeworks": [...]}``. Tolerated as well:
    a bare object, or a single-element list.
    """
    main: dict[str, Any] = {}
    related: list[dict[str, Any]] = []

    if isinstance(payload, list):
        dicts = [item for item in payload if isinstance(item, dict)]
        if dicts:
            main = dicts[0]
            related = [item for item in dicts[1:] if isinstance(item, dict)]
    elif isinstance(payload, dict):
        candidate = payload.get("0")
        if isinstance(candidate, dict):
            main = candidate
        elif any(key in payload for key in ("id", "theme", "comment")):
            main = payload
        found = payload.get("relatedHomeworks") or payload.get("related_homeworks")
        if isinstance(found, list):
            related = [item for item in found if isinstance(item, dict)]

    return main, related


def stable_hash(value: Any) -> str:
    """SHA-256 over a deterministically serialised JSON value."""
    dumped = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(dumped.encode("utf-8")).hexdigest()


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)


def homework_row_fields(main: dict[str, Any]) -> dict[str, Any]:
    """Fields stored as separate columns, taken only if present in the API."""
    return {
        "title": _text(main.get("theme")),
        "status": _text(main.get("status")),
        "subject": _text(main.get("name_spec")),
        "teacher": _text(main.get("fio_teach")),
        "due_at": _text(main.get("completion_time")),
        "created_at": _text(main.get("creation_time")),
    }


# --------------------------------------------------------------- markdown ---
def build_content_markdown(
    homework_id: str,
    main: dict[str, Any],
    related: list[dict[str, Any]],
    attachments: Iterable[dict[str, Any]],
) -> str:
    title = main.get("theme") or f"Homework {homework_id}"
    lines: list[str] = [f"# {title}", ""]
    lines.append(f"- **ID:** {homework_id}")

    meta = (
        ("Предмет", main.get("name_spec")),
        ("Преподаватель", main.get("fio_teach")),
        ("Создано", main.get("creation_time")),
        ("Сдать до", main.get("completion_time")),
        ("Крайний срок", main.get("overdue_time")),
        ("Статус", main.get("status")),
    )
    for label, value in meta:
        if value not in (None, ""):
            lines.append(f"- **{label}:** {value}")

    student = main.get("homework_stud") if isinstance(main.get("homework_stud"), dict) else {}
    if student.get("mark") not in (None, ""):
        lines.append(f"- **Оценка:** {student['mark']}")
    if student.get("stud_answer"):
        lines.append(f"- **Ответ студента:** {student['stud_answer']}")

    comment = (main.get("comment") or "").strip()
    lines += ["", "## Задание", "", comment or "_текст задания отсутствует в ответе API_"]

    teacher_comment = ""
    homework_comment = main.get("homework_comment")
    if isinstance(homework_comment, dict):
        teacher_comment = (homework_comment.get("text_comment") or "").strip()
    if teacher_comment:
        lines += ["", "## Комментарий преподавателя", "", teacher_comment]

    attachment_rows = list(attachments)
    lines += ["", "## Вложения", ""]
    if attachment_rows:
        for row in attachment_rows:
            name = row.get("original_name") or row.get("source_url", "")
            role = row.get("role") or "task"
            status = row.get("download_status")
            bits = [f"`{name}`", f"роль: {role}", f"статус: {status}"]
            if row.get("file_size"):
                bits.append(f"{row['file_size']} B")
            if row.get("mime_type"):
                bits.append(row["mime_type"])
            local = row.get("local_path")
            if local:
                bits.append(f"локально: {local}")
            extracted = row.get("extracted_text_path")
            if extracted:
                bits.append(f"текст: {extracted}")
            if row.get("last_error"):
                bits.append(f"ошибка: {row['last_error'][:160]}")
            lines.append(f"- " + " · ".join(bits))
    else:
        lines.append("_вложений нет_")

    if related:
        lines += ["", "## Связанные сдачи", ""]
        for item in related[:10]:
            parts = []
            if item.get("date_vizit"):
                parts.append(str(item["date_vizit"]))
            if item.get("mark") not in (None, ""):
                parts.append(f"оценка {item['mark']}")
            if item.get("status") not in (None, ""):
                parts.append(f"статус {item['status']}")
            if item.get("updated_at"):
                parts.append(f"обновлено {item['updated_at']}")
            lines.append("- " + ", ".join(parts) if parts else "- запись без данных")

    return "\n".join(lines).rstrip() + "\n"


# ------------------------------------------------------------------- sync ---
@dataclass
class SyncSummary:
    found: int = 0
    new: int = 0
    updated: int = 0
    unchanged: int = 0
    missing: int = 0
    attachments_downloaded: int = 0
    attachments_failed: int = 0
    errors: list[str] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "found": self.found,
            "new": self.new,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "marked_missing": self.missing,
            "attachments_downloaded": self.attachments_downloaded,
            "attachments_failed": self.attachments_failed,
            "error_count": len(self.errors),
            "errors": self.errors[:20],
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


class HomeworkService:
    """Everything MCP tools need: sync, read, search, cache stats."""

    def __init__(self, settings: Settings, cache: Cache, api: MyStatAPI) -> None:
        self.settings = settings
        self.cache = cache
        self.api = api
        self._sync_lock = asyncio.Lock()

    # ---- sync ---------------------------------------------------------
    async def sync(self, *, force: bool = False, download_attachments: bool = True) -> dict[str, Any]:
        if self._sync_lock.locked():
            raise RuntimeError("sync already in progress")
        async with self._sync_lock:
            summary = SyncSummary(started_at=_now())
            payload = await self.api.get_homework_list()
            items = [item for item in (payload.get("data") or []) if isinstance(item, dict)]
            summary.found = len(items)

            semaphore = asyncio.Semaphore(SYNC_CONCURRENCY)

            async def worker(item: dict[str, Any]) -> None:
                async with semaphore:
                    await self._sync_item(item, summary, force=force,
                                          download_attachments=download_attachments)

            await asyncio.gather(*(worker(item) for item in items))

            summary.missing = await self.cache.mark_missing(
                str(item.get("id")) for item in items if item.get("id") is not None
            )
            summary.finished_at = _now()
            await self.cache.set_state("last_sync", summary.as_dict())
            logger.info(
                "sync done: found=%s new=%s updated=%s unchanged=%s files=%s errors=%s",
                summary.found, summary.new, summary.updated, summary.unchanged,
                summary.attachments_downloaded, len(summary.errors),
            )
            return summary.as_dict()

    async def _sync_item(
        self,
        item: dict[str, Any],
        summary: SyncSummary,
        *,
        force: bool,
        download_attachments: bool,
    ) -> None:
        homework_id = str(item.get("id"))
        existing = await self.cache.get_homework(homework_id)
        list_json = json.dumps(item, ensure_ascii=False)

        try:
            raw_detail = await self.api.get_homework_detail(homework_id)
        except ApiNotFoundError:
            await self.cache.upsert_homework(
                homework_id=homework_id, list_json=list_json,
                synced=False, sync_error="details returned 404", **homework_row_fields(item)
            )
            summary.errors.append(f"{homework_id}: details 404")
            return
        except ApiError as exc:
            await self.cache.upsert_homework(
                homework_id=homework_id, list_json=list_json,
                synced=False, sync_error=str(exc)[:500], **homework_row_fields(item)
            )
            summary.errors.append(f"{homework_id}: {exc}")
            return

        main, related = normalize_detail(raw_detail)
        if not main:
            summary.errors.append(f"{homework_id}: unexpected details payload")
            return

        merged = {**item, **main}
        fields = homework_row_fields(merged)
        details_json = json.dumps(raw_detail, ensure_ascii=False)
        content_hash = stable_hash(main)
        unchanged = bool(existing and existing.get("content_hash") == content_hash)
        was_known = existing is not None

        await self.cache.upsert_homework(
            homework_id=homework_id,
            list_json=list_json,
            details_json=details_json,
            content_hash=content_hash,
            synced=True,
            sync_error=None,
            **fields,
        )

        refs = collect_attachment_refs(merged)
        attachment_changed = await self._sync_attachments(
            homework_id, refs, summary,
            force=force, download=download_attachments, unchanged=unchanged,
        )

        if unchanged and not attachment_changed and not force:
            summary.unchanged += 1
            return

        rows = await self.cache.list_attachments(homework_id)
        markdown = build_content_markdown(homework_id, merged, related, rows)
        await self.cache.upsert_homework(
            homework_id=homework_id,
            list_json=list_json,
            details_json=details_json,
            content_markdown=markdown,
            content_hash=content_hash,
            synced=True,
            sync_error=None,
            **fields,
        )
        await self._write_content_file(homework_id, markdown)
        await self._reindex(homework_id, merged, rows)

        if was_known:
            summary.updated += 1
        else:
            summary.new += 1

    async def _sync_attachments(
        self,
        homework_id: str,
        refs: list[AttachmentRef],
        summary: SyncSummary,
        *,
        force: bool,
        download: bool,
        unchanged: bool,
    ) -> bool:
        changed = False
        for index, ref in enumerate(refs):
            existing = await self.cache.get_attachment(homework_id, ref.url, ref.role)
            local_missing = True
            if existing and existing.get("local_path"):
                local_missing = not (self.settings.cache_dir / existing["local_path"]).exists()
            pending = existing is None or existing.get("download_status") != "downloaded" or local_missing

            if not download or (unchanged and not force and not pending):
                if existing is None:
                    await self.cache.upsert_attachment(
                        homework_id=homework_id, role=ref.role, source_url=ref.url,
                        original_name=ref.name_hint, download_status="pending",
                    )
                continue

            try:
                result = await download_attachment(
                    self.api.client,
                    ref,
                    self.settings.homework_dir / homework_id / "attachments",
                    cache_dir=self.settings.cache_dir,
                    max_size=self.settings.max_attachment_size,
                    index=index,
                )
            except Exception as exc:  # noqa: BLE001 - one bad file must not stop a sync
                summary.attachments_failed += 1
                message = f"{type(exc).__name__}: {exc}"
                await self.cache.upsert_attachment(
                    homework_id=homework_id, role=ref.role, source_url=ref.url,
                    original_name=ref.name_hint, download_status="failed", last_error=message[:500],
                )
                summary.errors.append(f"{homework_id} attachment {ref.role}: {message[:120]}")
                changed = True
                continue

            summary.attachments_downloaded += 1
            extracted_path, extracted_text, _note = self._extract(homework_id, result.path, result.mime_type)
            await self.cache.upsert_attachment(
                homework_id=homework_id,
                role=ref.role,
                source_url=ref.url,
                original_name=result.original_name,
                local_path=result.relative_path,
                mime_type=result.mime_type,
                file_size=result.size,
                sha256=result.sha256,
                download_status="downloaded",
                extracted_text_path=extracted_path,
                extracted_text=extracted_text,
            )
            changed = True
        return changed

    def _extract(self, homework_id: str, path: Path, mime: str | None) -> tuple[str | None, str | None, str]:
        result = extract_text(path, mime)
        if result.status != "ok" or not result.text:
            return None, result.text, result.note
        out_dir = self.settings.homework_dir / homework_id / "extracted"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{path.stem}.md"
        header = f"# {path.name}\n\n_{result.kind}: {result.note or 'extracted'}_\n\n"
        out_path.write_text(header + result.text, encoding="utf-8")
        try:
            relative = str(out_path.resolve().relative_to(self.settings.cache_dir.resolve()))
        except ValueError:
            relative = str(out_path)
        return relative, result.text, result.note

    async def _write_content_file(self, homework_id: str, markdown: str) -> None:
        directory = self.settings.homework_dir / homework_id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "content.md").write_text(markdown, encoding="utf-8")

    async def _reindex(self, homework_id: str, main: dict[str, Any], rows: list[dict[str, Any]]) -> None:
        documents: list[tuple[str, str]] = []
        title = " ".join(str(main.get(key) or "") for key in ("theme", "name_spec", "fio_teach")).strip()
        if title:
            documents.append(("meta", title))
        comment = (main.get("comment") or "").strip()
        if comment:
            documents.append(("task", comment))
        homework_comment = main.get("homework_comment")
        if isinstance(homework_comment, dict) and homework_comment.get("text_comment"):
            documents.append(("teacher_comment", str(homework_comment["text_comment"])))
        for row in rows:
            if row.get("extracted_text"):
                documents.append((row.get("original_name") or row.get("role") or "attachment",
                                  row["extracted_text"]))
        await self.cache.reindex_homework(homework_id, documents)

    # ---- read model ---------------------------------------------------
    async def list(self, *, limit: int = 20, offset: int = 0, status: str | None = None,
                   include_missing: bool = False) -> dict[str, Any]:
        limit = max(1, min(int(limit), 200))
        rows = await self.cache.list_homeworks(
            limit=limit, offset=max(0, int(offset)), status=status, include_missing=include_missing
        )
        last_sync = await self.cache.get_state("last_sync")
        return {
            "items": rows,
            "limit": limit,
            "offset": offset,
            "last_sync": json.loads(last_sync) if last_sync else None,
        }

    async def get(self, homework_id: str) -> dict[str, Any]:
        row = await self.cache.get_homework(str(homework_id))
        if row is None:
            raise KeyError(f"homework {homework_id} is not in the local cache; run homework_sync first")
        attachments = await self.cache.list_attachments(str(homework_id))
        details = json.loads(row["details_json"]) if row.get("details_json") else None
        main, related = normalize_detail(details) if details is not None else ({}, [])
        return {
            "id": row["id"],
            "title": row["title"],
            "subject": row["subject"],
            "teacher": row["teacher"],
            "status": row["status"],
            "due_at": row["due_at"],
            "created_at": row["created_at"],
            "is_missing": bool(row["is_missing"]),
            "last_synced_at": row["last_synced_at"],
            "content_markdown": row["content_markdown"],
            "task_text": main.get("comment"),
            "teacher_comment": (main.get("homework_comment") or {}).get("text_comment")
            if isinstance(main.get("homework_comment"), dict) else None,
            "related_homeworks": related[:10],
            "attachments": [
                {
                    "role": item["role"],
                    "name": item["original_name"],
                    "mime_type": item["mime_type"],
                    "size": item["file_size"],
                    "status": item["download_status"],
                    "local_path": item["local_path"],
                    "extracted_text_path": item["extracted_text_path"],
                    "error": item["last_error"],
                }
                for item in attachments
            ],
            "content_file": f"cache/homework/{row['id']}/content.md",
        }

    async def search(self, query: str, limit: int = 10) -> dict[str, Any]:
        limit = max(1, min(int(limit), 50))
        matches = await self.cache.search(query, limit)
        results = []
        for match in matches:
            homework = await self.cache.get_homework(match["homework_id"])
            results.append({
                "homework_id": match["homework_id"],
                "title": homework["title"] if homework else None,
                "subject": homework["subject"] if homework else None,
                "source": match["source"],
                "snippet": match["snippet"],
            })
        return {"query": query, "count": len(results), "results": results,
                "full_text_index": self.cache.fts_enabled}

    async def read_file(self, homework_id: str, name: str, *, max_chars: int = 60_000) -> dict[str, Any]:
        homework_id = str(homework_id)
        if not name or Path(name).is_absolute() or ".." in Path(name).parts:
            raise ValueError("file must be a plain name inside the homework directory")

        base = (self.settings.homework_dir / homework_id).resolve()
        attachments = await self.cache.list_attachments(homework_id)
        match = None
        for item in attachments:
            candidates = {
                (item.get("original_name") or "").lower(),
                Path(item.get("local_path") or "").name.lower(),
            }
            if name.lower() in candidates - {""}:
                match = item
                break

        if match and match.get("extracted_text"):
            return {
                "homework_id": homework_id,
                "file": match.get("original_name") or name,
                "kind": "extracted_text",
                "mime_type": match.get("mime_type"),
                "content": match["extracted_text"][:max_chars],
                "truncated": len(match["extracted_text"]) > max_chars,
                "local_path": match.get("local_path"),
            }

        direct = (base / name).resolve()
        if base != direct and base not in direct.parents:
            raise ValueError("resolved path escapes the homework directory")
        if direct.is_file():
            raw = direct.read_bytes()
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                text = None
            return {
                "homework_id": homework_id,
                "file": name,
                "kind": "file",
                "content": (text or "")[:max_chars] or None,
                "binary": text is None,
                "size": len(raw),
            }

        if match and match.get("local_path"):
            path = (self.settings.cache_dir / match["local_path"]).resolve()
            if path.is_file():
                return {
                    "homework_id": homework_id,
                    "file": match.get("original_name"),
                    "kind": "file",
                    "content": None,
                    "binary": True,
                    "size": match.get("file_size"),
                    "mime_type": match.get("mime_type"),
                    "note": "binary attachment; no extracted text available",
                }

        raise KeyError(f"file {name!r} not found for homework {homework_id}")

    async def cache_status(self) -> dict[str, Any]:
        counts = await self.cache.count_homeworks()
        attachments = await self.cache.attachment_stats()
        last_sync = await self.cache.get_state("last_sync")
        auth = await self.cache.get_auth()
        return {
            "database": str(self.settings.database_path),
            "database_size_bytes": self.settings.database_path.stat().st_size
            if self.settings.database_path.exists() else 0,
            "cache_dir": str(self.settings.cache_dir),
            "cache_size_bytes": _dir_size(self.settings.cache_dir),
            "homework": counts,
            "attachments": attachments,
            "last_sync": json.loads(last_sync) if last_sync else None,
            "full_text_index": self.cache.fts_enabled,
            "token_present": auth is not None,
        }


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _dir_size(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            continue
    return total
