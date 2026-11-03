"""Extraction du texte, page par page.

Un PDF sans couche texte (scan) ou une image ressort vide : le texte vient alors de
l'OCR d'Alambic Light (s'il est configuré), sinon le document est marqué `ocr_required`.
"""
from __future__ import annotations

import io
from pathlib import PurePosixPath

from ged.models import Page

IMAGES = frozenset({".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp"})


class UnsupportedFormat(Exception):
    pass


def extract_pages(filename: str, data: bytes) -> list[Page]:
    ext = PurePosixPath(filename).suffix.lower()
    if ext == ".pdf":
        return _pdf(data)
    if ext == ".docx":
        return _docx(data)
    if ext in {".txt", ".md", ".csv"}:
        return [Page(1, _decode(data))]
    if ext in IMAGES:
        return []  # aucun texte sans OCR

    raise UnsupportedFormat(ext)


def has_text(pages: list[Page], min_chars: int = 20) -> bool:
    return sum(len(p.text.strip()) for p in pages) >= min_chars


def _pdf(data: bytes) -> list[Page]:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    return [Page(i + 1, page.extract_text() or "") for i, page in enumerate(reader.pages)]


def _docx(data: bytes) -> list[Page]:
    from docx import Document

    doc = Document(io.BytesIO(data))
    parts = [p.text for p in doc.paragraphs if p.text.strip()]
    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            if any(cells):
                parts.append(" | ".join(cells))
    # Word n'expose pas la pagination : tout le document = page 1
    return [Page(1, "\n\n".join(parts))]


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")
