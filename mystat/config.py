"""Configuration loading and validation.

All paths are resolved relative to a fixed base directory, never relative to
the process working directory: an MCP client may start the server from
anywhere. For a development checkout the base is the project root; for an
installed tool it is the per-user config directory
(``%APPDATA%\\itstep-mystat-mcp``), so nothing is ever written into
``site-packages``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

def _looks_like_checkout(root: Path) -> bool:
    """True when ``root`` is a source tree rather than a site-packages dir."""
    return (root / "pyproject.toml").is_file() and (root / "mystat").is_dir()


_CANDIDATE_ROOT = Path(__file__).resolve().parents[1]

#: Development checkout directory, or ``None`` when the package is installed
#: (for an installed tool ``parents[1]`` points into ``site-packages``, which
#: must never receive the cache or the config).
PROJECT_ROOT: Path | None = _CANDIDATE_ROOT if _looks_like_checkout(_CANDIDATE_ROOT) else None


def app_config_dir() -> Path:
    """Per-user config directory for an installed copy of the server."""
    base = os.environ.get("APPDATA") or os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "itstep-mystat-mcp"


def default_root() -> Path:
    """Base directory for relative paths and for the implicit ``.env``."""
    return PROJECT_ROOT if PROJECT_ROOT is not None else app_config_dir()


def default_env_candidates() -> list[Path]:
    """Where ``.env`` is looked up when no explicit path is given.

    Order matters: an explicit project-local file wins over the per-user one,
    so a development checkout keeps working while an installed tool uses the
    user profile directory.
    """
    candidates = [app_config_dir() / ".env"]
    if PROJECT_ROOT is not None:
        candidates.insert(0, PROJECT_ROOT / ".env")
    return candidates

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
    env_file: Path | None = None

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
        root = default_root()
        if env_file is not None:
            env_path: Path | None = env_file
        elif os.environ.get("MYSTAT_ENV_FILE"):
            env_path = Path(os.environ["MYSTAT_ENV_FILE"])
        else:
            env_path = next((path for path in default_env_candidates() if path.is_file()), None)

        values: dict[str, str] = {}
        if env_path is not None and env_path.is_file():
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
            env_file=env_path,
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
                "run `itstep-mystat-mcp setup` or put them in the .env file "
                f"({self.env_file or default_env_candidates()[0]})"
            )

    def ensure_directories(self) -> None:
        for path in (self.cache_dir, self.homework_dir, self.extracted_dir, self.logs_dir):
            path.mkdir(parents=True, exist_ok=True)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
