"""Attachment discovery, validation and safe download.

Confirmed API facts: details expose only URLs (``file_path``, ``cover_image``,
``homework_stud.file_path``, ``homework_comment.attachment`` /
``homework_comment.attachment_path``). Files themselves are served openly
from ``https://fs[1-9x].itstep.org/api/v1/files/<token>`` and carry
``Content-Disposition`` with the original name.

Security posture:
* HTTPS only, host must be ``*.itstep.org``, path must start with
  ``/api/v1/files/``;
* hostname is resolved and private/loopback/link-local addresses rejected;
* redirect target is re-validated;
* size is capped while streaming;
* the on-disk name is sanitised and confined to the homework directory;
* MIME is sniffed from bytes, never trusted from the extension alone.
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import re
import socket
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Iterable
from urllib.parse import unquote, urlsplit

import httpx

from .config import ALLOWED_HOST_SUFFIXES, ALLOWED_PATH_PREFIX, Settings

logger = logging.getLogger(__name__)

CHUNK_SIZE = 64 * 1024
MAX_REDIRECTS = 3
WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
INVALID_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

Resolver = Callable[[str], Awaitable[Iterable[str]]]


class UnsafeUrlError(RuntimeError):
    """URL is not an allowed MyStat file URL."""


class DownloadError(RuntimeError):
    """Attachment could not be downloaded."""


@dataclass(frozen=True, slots=True)
class AttachmentRef:
    role: str
    url: str
    name_hint: str | None = None


@dataclass(frozen=True, slots=True)
class DownloadResult:
    path: Path
    relative_path: str
    size: int
    sha256: str
    mime_type: str
    original_name: str
    content_type_header: str | None


# ---------------------------------------------------------------- refs ----
def collect_attachment_refs(homework: dict) -> list[AttachmentRef]:
    """Extract attachment references from a normalised homework object."""
    refs: list[AttachmentRef] = []

    def add(role: str, value: object, name_hint: str | None = None) -> None:
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            if all(not (r.url == value and r.role == role) for r in refs):
                refs.append(AttachmentRef(role=role, url=value, name_hint=name_hint))

    add("task", homework.get("file_path"))
    add("cover", homework.get("cover_image"))

    student = homework.get("homework_stud")
    if isinstance(student, dict):
        add("student_answer", student.get("file_path"), student.get("fio_stud"))

    comment = homework.get("homework_comment")
    if isinstance(comment, dict):
        attachment = comment.get("attachment")
        attachment_path = comment.get("attachment_path")
        if isinstance(attachment_path, str):
            add("comment", attachment_path, attachment if isinstance(attachment, str) else None)
        elif isinstance(attachment, str) and attachment.startswith(("http://", "https://")):
            add("comment", attachment)

    return refs


# ------------------------------------------------------------ url rules ---
def _host_allowed(host: str) -> bool:
    host = host.lower().rstrip(".")
    return any(host == suffix.lstrip(".") or host.endswith(suffix) for suffix in ALLOWED_HOST_SUFFIXES)


async def _default_resolver(host: str) -> list[str]:
    loop = __import__("asyncio").get_running_loop()
    infos = await loop.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    addresses: list[str] = []
    for info in infos:
        addr = info[4][0]
        if addr not in addresses:
            addresses.append(addr)
    return addresses


def _ip_is_public(raw: str) -> bool:
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        return False
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


async def validate_url(url: str, *, resolver: Resolver | None = None) -> str:
    """Return the URL if it is a safe MyStat file URL, else raise."""
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise UnsafeUrlError(f"only https is allowed: {url}")
    if not parts.hostname:
        raise UnsafeUrlError(f"missing host: {url}")
    if not _host_allowed(parts.hostname):
        raise UnsafeUrlError(f"host {parts.hostname!r} is not an itstep.org host")
    if not parts.path.startswith(ALLOWED_PATH_PREFIX):
        raise UnsafeUrlError(f"path {parts.path!r} is not a MyStat file path")
    if parts.port not in (None, 443):
        raise UnsafeUrlError(f"unexpected port {parts.port}")

    host = parts.hostname
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        resolve = resolver or _default_resolver
        addresses = list(await resolve(host))
        if not addresses:
            raise UnsafeUrlError(f"host {host!r} did not resolve")
        for addr in addresses:
            if not _ip_is_public(addr):
                raise UnsafeUrlError(f"host {host!r} resolves to a non-public address {addr}")
    else:
        if not _ip_is_public(str(literal)):
            raise UnsafeUrlError(f"non-public address {literal}")

    return url


# --------------------------------------------------------------- naming ----
def parse_content_disposition(header: str | None) -> str | None:
    """Parse ``Content-Disposition`` preferring RFC 5987 ``filename*``."""
    if not header:
        return None
    extended = re.search(r"filename\*\s*=\s*([^;]+)", header, flags=re.IGNORECASE)
    if extended:
        raw = extended.group(1).strip().strip('"')
        if "''" in raw:
            charset, _, rest = raw.partition("''")
            try:
                return unquote(rest, encoding=charset or "utf-8", errors="strict")
            except Exception:  # noqa: BLE001
                return unquote(rest)
        return unquote(raw)
    plain = re.search(r'filename\s*=\s*"([^"]*)"', header, flags=re.IGNORECASE)
    if plain:
        return plain.group(1)
    plain = re.search(r"filename\s*=\s*([^;]+)", header, flags=re.IGNORECASE)
    if plain:
        return plain.group(1).strip().strip('"')
    return None


def sanitize_filename(name: str, fallback: str = "attachment") -> str:
    """Make an arbitrary name safe for a Windows file inside cache/homework."""
    name = unicodedata.normalize("NFC", name or "")
    name = name.replace("\\", "/").split("/")[-1]
    name = name.replace("\x00", "")
    name = INVALID_NAME_CHARS.sub("_", name).strip().strip(".")
    name = re.sub(r"\s+", " ", name)
    if not name or name in {".", ".."}:
        name = fallback
    stem, dot, suffix = name.partition(".")
    if stem.upper() in WINDOWS_RESERVED:
        name = f"_{name}"
    if len(name) > 120:
        stem = name[: 120 - len(suffix) - 1] if suffix else name[:120]
        name = f"{stem}.{suffix}" if suffix else stem
    return name


# ---------------------------------------------------------------- sniff ----
def sniff_mime(data: bytes, declared: str | None, filename: str) -> str:
    head = data[:512]
    if head.startswith(b"%PDF"):
        return "application/pdf"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[4:8] == b"ftyp":
        return "video/mp4"
    if head.startswith(b"PK\x03\x04"):
        by_name = Path(filename).suffix.lower()
        if by_name == ".docx":
            return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        if by_name == ".pptx":
            return "application/vnd.openxmlformats-officedocument.presentationml.presentation"
        if by_name == ".xlsx":
            return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        if by_name == ".zip":
            return "application/zip"
        return declared or "application/zip"
    lowered = head.lstrip()[:32].lower()
    if lowered.startswith((b"<!doctype html", b"<html")):
        return "text/html"
    if declared:
        return declared.split(";")[0].strip()
    return "application/octet-stream"


def is_textual(mime: str, filename: str) -> bool:
    if mime.startswith("text/"):
        return True
    return Path(filename).suffix.lower() in {".txt", ".md", ".csv", ".json", ".log", ".xml"}


# ------------------------------------------------------------- download ----
async def download_attachment(
    client: httpx.AsyncClient,
    ref: AttachmentRef,
    dest_dir: Path,
    *,
    cache_dir: Path,
    max_size: int,
    resolver: Resolver | None = None,
    index: int = 0,
) -> DownloadResult:
    """Download one attachment into ``dest_dir`` in a single streaming pass."""
    url = await validate_url(ref.url, resolver=resolver)
    dest_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = dest_dir / f".partial-{index}-{hashlib.sha1(url.encode()).hexdigest()[:8]}"

    digest = hashlib.sha256()
    head = bytearray()
    total = 0
    content_type: str | None = None
    disposition: str | None = None
    current_url = url

    response: httpx.Response | None = None
    try:
        for redirects in range(MAX_REDIRECTS + 1):
            response = await client.send(client.build_request("GET", current_url), stream=True)
            if not response.is_redirect:
                break
            location = response.headers.get("location", "")
            previous_url = str(response.url)
            await response.aclose()
            response = None
            if not location:
                raise DownloadError("redirect without location")
            current_url = await validate_url(
                str(httpx.URL(previous_url).join(location)), resolver=resolver
            )
        else:
            raise DownloadError("too many redirects")

        assert response is not None
        if response.status_code != 200:
            raise DownloadError(f"HTTP {response.status_code}")
        content_type = response.headers.get("content-type")
        disposition = response.headers.get("content-disposition")
        declared_length = response.headers.get("content-length")
        if declared_length and declared_length.isdigit() and int(declared_length) > max_size:
            raise DownloadError(f"file too large: {declared_length} bytes")
        with tmp_path.open("wb") as handle:
            async for chunk in response.aiter_bytes(CHUNK_SIZE):
                total += len(chunk)
                if total > max_size:
                    raise DownloadError(f"file exceeds limit of {max_size} bytes")
                digest.update(chunk)
                if len(head) < 512:
                    head.extend(chunk[: 512 - len(head)])
                handle.write(chunk)
    except httpx.HTTPError as exc:
        tmp_path.unlink(missing_ok=True)
        raise DownloadError(f"{type(exc).__name__}: {exc}") from exc
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    finally:
        if response is not None:
            await response.aclose()

    raw_name = parse_content_disposition(disposition)
    if not raw_name:
        raw_name = unquote(urlsplit(url).path.rsplit("/", 1)[-1]) or f"attachment-{index}"
    original_name = sanitize_filename(raw_name, fallback=f"attachment-{index}")
    sha = digest.hexdigest()
    mime = sniff_mime(bytes(head), content_type, original_name)

    path = _unique_path(dest_dir, original_name, sha)
    if path != dest_dir / original_name and (dest_dir / original_name).exists() \
            and _file_sha256(dest_dir / original_name) == sha:
        # Same content already stored under the plain name: reuse it.
        path = dest_dir / original_name
        tmp_path.unlink(missing_ok=True)
    elif path.exists() and _file_sha256(path) == sha:
        tmp_path.unlink(missing_ok=True)
    else:
        tmp_path.replace(path)

    return DownloadResult(
        path=path,
        relative_path=_relative(path, cache_dir),
        size=total,
        sha256=sha,
        mime_type=mime,
        original_name=original_name,
        content_type_header=content_type,
    )


def _unique_path(dest_dir: Path, name: str, sha: str) -> Path:
    candidate = dest_dir / name
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    return dest_dir / f"{stem}-{sha[:8]}{suffix}"


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative(path: Path, base: Path) -> str:
    try:
        return str(path.resolve().relative_to(base.resolve()))
    except ValueError:
        return str(path)
