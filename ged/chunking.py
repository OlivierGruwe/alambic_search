"""Découpage en extraits (chunks) qui ne franchissent jamais une frontière de page,
pour que chaque citation pointe vers une page précise."""
from __future__ import annotations

import re

from ged.models import Chunk, Page

_PARAGRAPH_SPLIT = re.compile(r"\n\s*\n")


def chunk_pages(pages: list[Page], max_chars: int = 1200, overlap: int = 200) -> list[Chunk]:
    if overlap >= max_chars:
        raise ValueError("overlap doit être inférieur à max_chars")
    out: list[Chunk] = []
    for page in pages:
        buf = ""
        for para in (p.strip() for p in _PARAGRAPH_SPLIT.split(page.text)):
            if not para:
                continue
            if len(para) > max_chars:
                # Paragraphe trop long : fenêtres glissantes (qui se chevauchent déjà)
                if buf:
                    out.append(Chunk(len(out), page.number, buf))
                windows = _windows(para, max_chars, overlap)
                for w in windows[:-1]:
                    out.append(Chunk(len(out), page.number, w))
                buf = windows[-1]
            elif not buf:
                buf = para
            elif len(buf) + 2 + len(para) <= max_chars:
                buf = f"{buf}\n\n{para}"
            else:
                out.append(Chunk(len(out), page.number, buf))
                tail = buf[-overlap:] if overlap else ""
                buf = f"{tail}\n\n{para}" if tail and len(tail) + 2 + len(para) <= max_chars else para
        if buf:
            out.append(Chunk(len(out), page.number, buf))
    return out


def _windows(text: str, max_chars: int, overlap: int) -> list[str]:
    step = max_chars - overlap
    return [text[i : i + max_chars] for i in range(0, max(len(text) - overlap, 1), step)]
