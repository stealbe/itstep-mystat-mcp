"""Configuration loading and validation.

All paths are resolved relative to the project root (the directory that
contains ``server.py``), never relative to the process working directory:
an MCP client may start the server from anywhere.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_BASE_URL = "https://mapi.itstep.org/v1/mystat"
DEFAULT_LANGUAGE = "uk"
DEFAULT_STATUS_FILTER = "3"
DEFAULT_SORT = "-hw.time"

# Attachments are served from *.itstep.org only, under this path prefix.
ALLOWED_HOST_SUFFIXES = (".itstep.org", "itstep.org")
ALLOWED_PATH_PREFIX = "/api/v1/files/"


class ConfigError(RuntimeError):
    """Raised when configuration is missing or invalid."""


def _as_int(value: str | None, default: int) -> int:
    if value is None or not value.strip():
        return default
    try:
        return int(value.strip())
    except ValueError as exc:
        raise ConfigError(f"expected integer, got {value!r}") from exc


def _as_float(value: str | None, default: float) -> float:
    if value is None or not value.strip():
        return default
    try:
        return float(value.strip())
    except ValueError as exc:
        raise ConfigError(f"expected number, got {value!r}") from exc


def _resolve(path_value: str, root: Path) -> Path:
    path = Path(path_value).expanduser()
    if not path.is_absolute():
        path = root / path
    return path.resolve()


@dataclass(frozen=True, slots=True)
class Settings:
    """Resolved runtime settings."""

    base_url: str
    language: str
    login: str
    password: str
    cache_dir: Path
    database_path: Path
    sync_limit: int
    http_timeout: float
    max_attachment_size: int
    status_filter: str
    sort: str

    # ---- derived paths -------------------------------------------------
    @property
    def homework_dir(self) -> Path:
        return self.cache_dir / "homework"

    @property
    def extracted_dir(self) -> Path:
        return self.cache_dir / "extracted"

    @property
    def logs_dir(self) -> Path:
        return self.cache_dir / "logs"

    @property
    def has_credentials(self) -> bool:
        return bool(self.login and self.password)

    # ---- construction --------------------------------------------------
    @classmethod
    def from_env(cls, env_file: Path | None = None) -> "Settings":
        root = PROJECT_ROOT
        env_path = env_file or Path(os.environ.get("MYSTAT_ENV_FILE") or (root / ".env"))
        values: dict[str, str] = {}
        if env_path.is_file():
            values = {k: v for k, v in dotenv_values(env_path).items() if v is not None}

        def get(*names: str) -> str:
            for name in names:
                raw = values.get(name)
                if raw is None:
                    raw = os.environ.get(name)
                if raw is not None and raw.strip():
                    return raw.strip()
            return ""

        cache_dir = _resolve(get("CACHE_DIR") or "./cache", root)
        database_path = _resolve(get("DATABASE_PATH") or str(cache_dir / "mystat.db"), root)

        settings = cls(
            base_url=(get("MYSTAT_BASE_URL") or DEFAULT_BASE_URL).rstrip("/"),
            language=get("MYSTAT_LANGUAGE") or DEFAULT_LANGUAGE,
            login=get("MYSTAT_LOGIN", "LOGIN"),
            password=get("MYSTAT_PASSWORD", "PASSWORD"),
            cache_dir=cache_dir,
            database_path=database_path,
            sync_limit=_as_int(get("SYNC_LIMIT"), 1000),
            http_timeout=_as_float(get("HTTP_TIMEOUT"), 30.0),
            max_attachment_size=_as_int(get("MAX_ATTACHMENT_SIZE_MB"), 100) * 1024 * 1024,
            status_filter=get("MYSTAT_STATUS_FILTER") or DEFAULT_STATUS_FILTER,
            sort=get("MYSTAT_SORT") or DEFAULT_SORT,
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if not self.base_url.startswith("https://"):
            raise ConfigError("MYSTAT_BASE_URL must use https://")
        if self.sync_limit <= 0:
            raise ConfigError("SYNC_LIMIT must be positive")
        if self.http_timeout <= 0:
            raise ConfigError("HTTP_TIMEOUT must be positive")
        if self.max_attachment_size <= 0:
            raise ConfigError("MAX_ATTACHMENT_SIZE_MB must be positive")

    def require_credentials(self) -> None:
        if not self.has_credentials:
            raise ConfigError(
                "MYSTAT_LOGIN and MYSTAT_PASSWORD are not set; "
                "put them in .env next to server.py"
            )

    def ensure_directories(self) -> None:
        for path in (self.cache_dir, self.homework_dir, self.extracted_dir, self.logs_dir):
            path.mkdir(parents=True, exist_ok=True)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
