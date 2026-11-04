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
        # NUL (0x00) : présent dans certains PDF ou OCR, refusé par PostgreSQL, sans valeur pour la recherche
        for para in (p.strip() for p in _PARAGRAPH_SPLIT.split(page.text.replace("\x00", ""))):
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


# --- Contexte des passages -----------------------------------------------------------------
# Un passage isolé (« Le délai est de 30 jours ») ne dit pas de quel document ni de quelle
# section il vient. Le texte VECTORISÉ commence donc par un en-tête : document, type, section.
# Le texte affiché et la recherche par mots restent ceux du passage.

_NUMBERED = re.compile(r"^\s*(\d+(\.\d+)*[.)]?|[IVX]{1,5}[.)]|#{1,4})\s+\S")
_KEYWORD = re.compile(r"^\s*(article|chapitre|section|titre|annexe|partie|objet)\b", re.IGNORECASE)


def is_heading(line: str) -> bool:
    line = line.strip()
    if not 3 <= len(line) <= 90 or line.endswith((",", ";")):
        return False
    if sum(c.isalpha() for c in line) < 3:  # « 1 000 € », « 2026 » : pas des titres
        return False
    if _NUMBERED.match(line) or _KEYWORD.match(line):
        # un titre est court et ne finit pas comme une phrase (« 1. Le délai est de 30 jours. » n'en est pas un)
        return len(line.split()) <= 14 and not line.endswith(".")
    letters = [c for c in line if c.isalpha()]
    return len(letters) >= 4 and sum(c.isupper() for c in letters) / len(letters) > 0.85 and len(line) <= 70


def sections(texts: list[str]) -> list[str | None]:
    """Titre de section en vigueur pour chaque passage (dans l'ordre du document)."""
    current, out = None, []
    for text in texts:
        lines = [x.strip() for x in text.splitlines() if x.strip()]
        first = lines[0] if lines and is_heading(lines[0]) else None  # passage qui commence par un titre
        out.append(first or current)
        for x in lines:
            if is_heading(x):
                current = x.lstrip("# ").strip()[:90]
        if first:
            out[-1] = first.lstrip("# ").strip()[:90]
    return out


def document_label(path: str) -> str:
    """« Compta/Factures/facture-acme.pdf » → « Compta / Factures / facture-acme »."""
    parts = [p for p in path.replace("\\", "/").split("/") if p]
    if parts:
        stem = parts[-1].rsplit(".", 1)[0] if "." in parts[-1] else parts[-1]
        parts[-1] = stem.replace("_", " ")
    return " / ".join(parts)


def embedding_inputs(path: str, doc_label: str | None, texts: list[str]) -> list[str]:
    """Textes à vectoriser : en-tête (document, type, section) + passage."""
    head = f"Document : {document_label(path)}" + (f" ({doc_label})" if doc_label else "")
    return [f"{head}" + (f" — {s}" if s else "") + f"\n{t}" for s, t in zip(sections(texts), texts, strict=True)]
