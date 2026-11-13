"""SQLite persistence layer (aiosqlite).

One connection per process; WAL enabled so that a long sync does not block
reads from MCP tools.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

import aiosqlite

from .search import build_fts_query

SCHEMA_VERSION = 1

SCHEMA_SQL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS auth (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        access_token TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        expires_at TEXT,
        token_type TEXT NOT NULL DEFAULT 'Bearer'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS homework (
        id TEXT PRIMARY KEY,
        title TEXT,
        status TEXT,
        subject TEXT,
        teacher TEXT,
        due_at TEXT,
        created_at TEXT,
        list_json TEXT,
        details_json TEXT,
        content_markdown TEXT,
        content_hash TEXT,
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        last_synced_at TEXT,
        is_missing INTEGER NOT NULL DEFAULT 0,
        sync_error TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS attachments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        homework_id TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'task',
        source_url TEXT NOT NULL,
        original_name TEXT,
        local_path TEXT,
        mime_type TEXT,
        file_size INTEGER,
        sha256 TEXT,
        download_status TEXT NOT NULL DEFAULT 'pending',
        extracted_text_path TEXT,
        extracted_text TEXT,
        last_downloaded_at TEXT,
        last_error TEXT,
        UNIQUE(homework_id, source_url, role),
        FOREIGN KEY(homework_id) REFERENCES homework(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sync_state (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_homework_due ON homework(due_at)",
    "CREATE INDEX IF NOT EXISTS idx_attachments_homework ON attachments(homework_id)",
)

FTS_SQL = (
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5(
        homework_id UNINDEXED,
        source UNINDEXED,
        body,
        tokenize = 'unicode61 remove_diacritics 2'
    )
    """,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def utc_from_epoch(epoch: int | float) -> str:
    return datetime.fromtimestamp(float(epoch), tz=timezone.utc).replace(microsecond=0).isoformat()


class Cache:
    """Thin async wrapper around SQLite with explicit schema versioning."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._db: aiosqlite.Connection | None = None
        self.fts_enabled = False

    # ---- lifecycle -----------------------------------------------------
    async def open(self) -> "Cache":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._db.execute("PRAGMA busy_timeout=10000")
        await self._migrate()
        await self._db.commit()
        return self

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def __aenter__(self) -> "Cache":
        return await self.open()

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("cache is not opened")
        return self._db

    async def _migrate(self) -> None:
        async with self.db.execute("PRAGMA user_version") as cur:
            row = await cur.fetchone()
        version = int(row[0]) if row else 0
        if version >= SCHEMA_VERSION:
            # Ensure tables exist even if the file was created elsewhere.
            await self._create_tables()
            return
        await self._create_tables()
        await self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    async def _create_tables(self) -> None:
        for statement in SCHEMA_SQL:
            await self.db.execute(statement)
        try:
            for statement in FTS_SQL:
                await self.db.execute(statement)
            self.fts_enabled = True
        except sqlite3.OperationalError:
            # SQLite built without FTS5 -> fall back to LIKE search.
            self.fts_enabled = False

    # ---- auth ----------------------------------------------------------
    async def get_auth(self) -> dict[str, Any] | None:
        async with self.db.execute("SELECT * FROM auth WHERE id = 1") as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def save_auth(self, token: str, expires_at: str | None, token_type: str = "Bearer") -> None:
        now = utc_now()
        await self.db.execute(
            """
            INSERT INTO auth (id, access_token, created_at, updated_at, expires_at, token_type)
            VALUES (1, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                access_token = excluded.access_token,
                updated_at = excluded.updated_at,
                expires_at = excluded.expires_at,
                token_type = excluded.token_type
            """,
            (token, now, now, expires_at, token_type),
        )
        await self.db.commit()

    async def delete_auth(self) -> None:
        await self.db.execute("DELETE FROM auth WHERE id = 1")
        await self.db.commit()

    # ---- homework ------------------------------------------------------
    async def get_homework(self, homework_id: str) -> dict[str, Any] | None:
        async with self.db.execute("SELECT * FROM homework WHERE id = ?", (str(homework_id),)) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def upsert_homework(
        self,
        *,
        homework_id: str,
        title: str | None,
        status: str | None,
        subject: str | None,
        teacher: str | None,
        due_at: str | None,
        created_at: str | None,
        list_json: str | None,
        details_json: str | None = None,
        content_markdown: str | None = None,
        content_hash: str | None = None,
        synced: bool = False,
        sync_error: str | None = None,
    ) -> None:
        now = utc_now()
        await self.db.execute(
            """
            INSERT INTO homework (
                id, title, status, subject, teacher, due_at, created_at,
                list_json, details_json, content_markdown, content_hash,
                first_seen_at, last_seen_at, last_synced_at, is_missing, sync_error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
            ON CONFLICT(id) DO UPDATE SET
                title = excluded.title,
                status = excluded.status,
                subject = excluded.subject,
                teacher = excluded.teacher,
                due_at = excluded.due_at,
                created_at = excluded.created_at,
                list_json = excluded.list_json,
                details_json = COALESCE(excluded.details_json, homework.details_json),
                content_markdown = COALESCE(excluded.content_markdown, homework.content_markdown),
                content_hash = COALESCE(excluded.content_hash, homework.content_hash),
                last_seen_at = excluded.last_seen_at,
                last_synced_at = CASE WHEN ? THEN excluded.last_synced_at ELSE homework.last_synced_at END,
                is_missing = 0,
                sync_error = excluded.sync_error
            """,
            (
                str(homework_id), title, status, subject, teacher, due_at, created_at,
                list_json, details_json, content_markdown, content_hash,
                now, now, now if synced else None, sync_error,
                1 if synced else 0,
            ),
        )
        await self.db.commit()

    async def set_homework_error(self, homework_id: str, message: str) -> None:
        await self.db.execute(
            "UPDATE homework SET sync_error = ?, last_seen_at = ? WHERE id = ?",
            (message[:1000], utc_now(), str(homework_id)),
        )
        await self.db.commit()

    async def mark_missing(self, active_ids: Iterable[str]) -> int:
        active = {str(i) for i in active_ids}
        async with self.db.execute("SELECT id FROM homework WHERE is_missing = 0") as cur:
            rows = await cur.fetchall()
        stale = [row["id"] for row in rows if row["id"] not in active]
        if stale:
            await self.db.executemany(
                "UPDATE homework SET is_missing = 1 WHERE id = ?",
                [(hid,) for hid in stale],
            )
            await self.db.commit()
        return len(stale)

    async def list_homeworks(
        self,
        *,
        limit: int = 20,
        offset: int = 0,
        status: str | None = None,
        include_missing: bool = False,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if not include_missing:
            clauses.append("is_missing = 0")
        if status is not None:
            clauses.append("status = ?")
            params.append(str(status))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.extend([limit, offset])
        query = f"""
            SELECT id, title, subject, teacher, status, due_at, created_at,
                   content_hash, last_synced_at, is_missing, sync_error,
                   LENGTH(COALESCE(content_markdown, '')) AS content_len
            FROM homework
            {where}
            ORDER BY COALESCE(due_at, '') DESC, id DESC
            LIMIT ? OFFSET ?
        """
        async with self.db.execute(query, tuple(params)) as cur:
            rows = await cur.fetchall()
        return [dict(row) for row in rows]

    async def count_homeworks(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for label, query in (
            ("total", "SELECT COUNT(*) FROM homework"),
            ("missing", "SELECT COUNT(*) FROM homework WHERE is_missing = 1"),
            ("with_details", "SELECT COUNT(*) FROM homework WHERE details_json IS NOT NULL"),
            ("with_errors", "SELECT COUNT(*) FROM homework WHERE sync_error IS NOT NULL"),
        ):
            async with self.db.execute(query) as cur:
                row = await cur.fetchone()
            result[label] = int(row[0]) if row else 0
        return result

    # ---- attachments ---------------------------------------------------
    async def upsert_attachment(
        self,
        *,
        homework_id: str,
        role: str,
        source_url: str,
        original_name: str | None = None,
        local_path: str | None = None,
        mime_type: str | None = None,
        file_size: int | None = None,
        sha256: str | None = None,
        download_status: str = "pending",
        extracted_text_path: str | None = None,
        extracted_text: str | None = None,
        last_error: str | None = None,
    ) -> None:
        await self.db.execute(
            """
            INSERT INTO attachments (
                homework_id, role, source_url, original_name, local_path, mime_type,
                file_size, sha256, download_status, extracted_text_path, extracted_text,
                last_downloaded_at, last_error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(homework_id, source_url, role) DO UPDATE SET
                original_name = COALESCE(excluded.original_name, attachments.original_name),
                local_path = COALESCE(excluded.local_path, attachments.local_path),
                mime_type = COALESCE(excluded.mime_type, attachments.mime_type),
                file_size = COALESCE(excluded.file_size, attachments.file_size),
                sha256 = COALESCE(excluded.sha256, attachments.sha256),
                download_status = excluded.download_status,
                extracted_text_path = COALESCE(excluded.extracted_text_path, attachments.extracted_text_path),
                extracted_text = COALESCE(excluded.extracted_text, attachments.extracted_text),
                last_downloaded_at = COALESCE(excluded.last_downloaded_at, attachments.last_downloaded_at),
                last_error = excluded.last_error
            """,
            (
                str(homework_id), role, source_url, original_name, local_path, mime_type,
                file_size, sha256, download_status, extracted_text_path, extracted_text,
                utc_now() if download_status == "downloaded" else None,
                last_error,
            ),
        )
        await self.db.commit()

    async def get_attachment(self, homework_id: str, source_url: str, role: str) -> dict[str, Any] | None:
        async with self.db.execute(
            "SELECT * FROM attachments WHERE homework_id = ? AND source_url = ? AND role = ?",
            (str(homework_id), source_url, role),
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def list_attachments(self, homework_id: str) -> list[dict[str, Any]]:
        async with self.db.execute(
            "SELECT * FROM attachments WHERE homework_id = ? ORDER BY id", (str(homework_id),)
        ) as cur:
            rows = await cur.fetchall()
        return [dict(row) for row in rows]

    async def attachment_stats(self) -> dict[str, int]:
        async with self.db.execute(
            "SELECT download_status, COUNT(*) AS n FROM attachments GROUP BY download_status"
        ) as cur:
            rows = await cur.fetchall()
        return {row["download_status"]: int(row["n"]) for row in rows}

    async def attachments_for_homeworks(self, homework_ids: Sequence[str]) -> list[dict[str, Any]]:
        if not homework_ids:
            return []
        placeholders = ",".join("?" for _ in homework_ids)
        async with self.db.execute(
            f"SELECT * FROM attachments WHERE homework_id IN ({placeholders}) ORDER BY homework_id, id",
            tuple(str(i) for i in homework_ids),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(row) for row in rows]

    # ---- sync state ----------------------------------------------------
    async def set_state(self, key: str, value: Any) -> None:
        payload = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        await self.db.execute(
            """
            INSERT INTO sync_state (key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (key, payload, utc_now()),
        )
        await self.db.commit()

    async def get_state(self, key: str) -> str | None:
        async with self.db.execute("SELECT value FROM sync_state WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
        return row["value"] if row else None

    # ---- search index --------------------------------------------------
    async def reindex_homework(self, homework_id: str, documents: Sequence[tuple[str, str]]) -> None:
        if not self.fts_enabled:
            return
        await self.db.execute("DELETE FROM search_index WHERE homework_id = ?", (str(homework_id),))
        rows = [(str(homework_id), source, body) for source, body in documents if body and body.strip()]
        if rows:
            await self.db.executemany(
                "INSERT INTO search_index (homework_id, source, body) VALUES (?, ?, ?)", rows
            )
        await self.db.commit()

    async def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        if self.fts_enabled:
            match = build_fts_query(query)
            if not match:
                return []
            sql = """
                SELECT homework_id, source,
                       snippet(search_index, 2, '[', ']', ' … ', 14) AS snippet,
                       bm25(search_index) AS score
                FROM search_index
                WHERE search_index MATCH ?
                ORDER BY score
                LIMIT ?
            """
            async with self.db.execute(sql, (match, limit)) as cur:
                rows = await cur.fetchall()
            return [dict(row) for row in rows]
        return await self._search_like(query, limit)

    async def _search_like(self, query: str, limit: int) -> list[dict[str, Any]]:
        like = f"%{query.strip()}%"
        sql = """
            SELECT h.id AS homework_id, 'content' AS source,
                   SUBSTR(COALESCE(h.content_markdown, ''), 1, 400) AS snippet, 0 AS score
            FROM homework h
            WHERE COALESCE(h.content_markdown, '') LIKE ?
            UNION ALL
            SELECT a.homework_id, COALESCE(a.original_name, 'attachment') AS source,
                   SUBSTR(COALESCE(a.extracted_text, ''), 1, 400) AS snippet, 0 AS score
            FROM attachments a
            WHERE COALESCE(a.extracted_text, '') LIKE ?
            LIMIT ?
        """
        async with self.db.execute(sql, (like, like, limit)) as cur:
            rows = await cur.fetchall()
        return [dict(row) for row in rows]
