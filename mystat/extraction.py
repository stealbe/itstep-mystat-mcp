"""Text extraction from downloaded attachments.

Supported: PDF, DOCX, PPTX, XLSX, TXT/MD/CSV/JSON, HTML. Images and media are
recorded with metadata only; ZIP archives are deliberately not unpacked.
OCR is out of scope for the first version.
"""

from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass
from pathlib import Path

from .attachments import is_textual

logger = logging.getLogger(__name__)

MAX_TEXT_CHARS = 200_000
TEXT_ENCODINGS = ("utf-8-sig", "utf-8", "cp1251", "koi8-r", "latin-1")


@dataclass(slots=True)
class ExtractionResult:
    text: str | None
    status: str  # "ok" | "unsupported" | "error"
    kind: str
    note: str = ""
    output_path: Path | None = None


def extract_text(path: Path, mime: str | None, *, max_chars: int = MAX_TEXT_CHARS) -> ExtractionResult:
    mime = (mime or "").lower()
    suffix = path.suffix.lower()
    try:
        if mime == "application/pdf" or suffix == ".pdf":
            return _from_pdf(path, max_chars)
        if mime.endswith("wordprocessingml.document") or suffix == ".docx":
            return _from_docx(path, max_chars)
        if mime.endswith("presentationml.presentation") or suffix == ".pptx":
            return _from_pptx(path, max_chars)
        if mime.endswith("spreadsheetml.sheet") or suffix == ".xlsx":
            return _from_xlsx(path, max_chars)
        if mime == "text/html" or suffix in {".html", ".htm"}:
            return _from_html(path, max_chars)
        if mime.startswith("image/"):
            return _from_image(path)
        if mime.startswith(("video/", "audio/")):
            return ExtractionResult(None, "unsupported", mime, "media file stored, no text extraction")
        if mime == "application/zip" or suffix == ".zip":
            return ExtractionResult(None, "unsupported", "zip", "archives are not unpacked automatically")
        if is_textual(mime, str(path)) or suffix in {".txt", ".md", ".csv", ".json", ".log", ".xml"}:
            return _from_plain(path, max_chars, suffix)
        if suffix == ".doc":
            return ExtractionResult(None, "unsupported", "doc", "legacy .doc is not supported")
    except Exception as exc:  # noqa: BLE001 - one bad file must not stop a sync
        logger.warning("extraction failed for %s: %s", path.name, exc)
        return ExtractionResult(None, "error", mime or suffix or "unknown", f"{type(exc).__name__}: {exc}")
    return ExtractionResult(None, "unsupported", mime or suffix or "unknown", "no extractor for this format")


# ---------------------------------------------------------------- helpers ---
def _clip(text: str, max_chars: int) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    text = text.strip()
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n\n…[truncated at {max_chars} characters]"
    return text


def _decode(raw: bytes) -> tuple[str, str]:
    for encoding in TEXT_ENCODINGS:
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace"), "utf-8/replace"


# ---------------------------------------------------------------- formats ---
def _from_pdf(path: Path, max_chars: int) -> ExtractionResult:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    pages: list[str] = []
    for number, page in enumerate(reader.pages, start=1):
        try:
            chunk = page.extract_text() or ""
        except Exception as exc:  # noqa: BLE001
            chunk = f"[page {number}: extraction failed: {exc}]"
        if chunk.strip():
            pages.append(chunk)
    text = _clip("\n\n".join(pages), max_chars)
    if not text:
        return ExtractionResult(None, "unsupported", "pdf", "no text layer (probably a scan); OCR not enabled")
    return ExtractionResult(text, "ok", "pdf", f"{len(reader.pages)} page(s)")


def _from_docx(path: Path, max_chars: int) -> ExtractionResult:
    import docx

    document = docx.Document(str(path))
    blocks: list[str] = [paragraph.text for paragraph in document.paragraphs if paragraph.text.strip()]
    for index, table in enumerate(document.tables, start=1):
        blocks.append(f"\n[table {index}]")
        for row in table.rows:
            cells = [cell.text.strip().replace("\n", " ") for cell in row.cells]
            blocks.append(" | ".join(cells))
    return ExtractionResult(_clip("\n".join(blocks), max_chars), "ok", "docx")


def _from_pptx(path: Path, max_chars: int) -> ExtractionResult:
    from pptx import Presentation

    presentation = Presentation(str(path))
    blocks: list[str] = []
    for number, slide in enumerate(presentation.slides, start=1):
        blocks.append(f"\n## Slide {number}")
        for shape in slide.shapes:
            if shape.has_text_frame and shape.text_frame.text.strip():
                blocks.append(shape.text_frame.text)
            if getattr(shape, "has_table", False):
                for row in shape.table.rows:
                    blocks.append(" | ".join(cell.text.strip() for cell in row.cells))
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame.text.strip():
            blocks.append("[notes] " + slide.notes_slide.notes_text_frame.text)
    return ExtractionResult(_clip("\n".join(blocks), max_chars), "ok", "pptx", f"{len(presentation.slides)} slide(s)")


def _from_xlsx(path: Path, max_chars: int) -> ExtractionResult:
    from openpyxl import load_workbook

    workbook = load_workbook(str(path), read_only=True, data_only=True)
    blocks: list[str] = []
    try:
        for sheet in workbook.worksheets:
            blocks.append(f"\n## Sheet: {sheet.title}")
            for row in sheet.iter_rows(values_only=True):
                if row is None:
                    continue
                cells = ["" if cell is None else str(cell) for cell in row]
                if any(cell.strip() for cell in cells):
                    blocks.append(" | ".join(cells).rstrip(" |"))
    finally:
        workbook.close()
    return ExtractionResult(_clip("\n".join(blocks), max_chars), "ok", "xlsx")


def _from_html(path: Path, max_chars: int) -> ExtractionResult:
    from bs4 import BeautifulSoup

    raw = path.read_bytes()
    html, encoding = _decode(raw)
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text("\n")
    return ExtractionResult(_clip(text, max_chars), "ok", "html", f"decoded as {encoding}")


def _from_plain(path: Path, max_chars: int, suffix: str) -> ExtractionResult:
    raw = path.read_bytes()
    text, encoding = _decode(raw)
    if suffix == ".csv":
        try:
            rows = list(csv.reader(io.StringIO(text)))
            text = "\n".join(" | ".join(cell.strip() for cell in row) for row in rows)
        except Exception:  # noqa: BLE001 - keep raw text on CSV quirks
            pass
    return ExtractionResult(_clip(text, max_chars), "ok", suffix.lstrip(".") or "text", f"decoded as {encoding}")


def _from_image(path: Path) -> ExtractionResult:
    from PIL import Image

    with Image.open(path) as image:
        note = f"{image.format} {image.width}x{image.height} {image.mode}"
    return ExtractionResult(None, "unsupported", "image", note)
