"""Vignettes : aperçu d'une page de document dans les résultats.

Rendues à la volée depuis le fichier de la source (rien n'est écrit sur disque) et gardées en
mémoire (LRU) par empreinte de contenu : un fichier modifié donne une nouvelle vignette.
PDF : PDFium (pypdfium2). Images : Pillow. Bureautique : converti en PDF si la conversion est
configurée. Autres formats (texte…) : pas de vignette.
"""
from __future__ import annotations

import io
import threading
from collections import OrderedDict
from pathlib import PurePosixPath

PDF = {".pdf"}
IMAGES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp", ".bmp", ".gif"}
MAX_BYTES = 80 * 1024 * 1024  # au-delà : pas de vignette (lecture trop longue)
WIDTHS = (160, 320, 480)  # largeurs servies (le navigateur choisit selon l'écran)

# Vignette « indisponible » : renvoyée plutôt qu'une erreur (pas d'image cassée dans la page)
PLACEHOLDER = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="160" height="214" viewBox="0 0 160 214">'
    b'<rect width="160" height="214" rx="6" fill="#EEF1F5"/>'
    b'<path d="M58 70h34l14 14v58a4 4 0 0 1-4 4H58a4 4 0 0 1-4-4V74a4 4 0 0 1 4-4z" fill="none" '
    b'stroke="#8A97AB" stroke-width="3"/><path d="M92 70v14h14" fill="none" stroke="#8A97AB" stroke-width="3"/>'
    b"</svg>"
)

_pdfium_lock = threading.Lock()  # PDFium n'accepte pas d'appels concurrents


def suffix(path: str) -> str:
    return PurePosixPath(path).suffix.lower()


def supported(path: str, office: frozenset[str] = frozenset()) -> bool:
    s = suffix(path)
    return s in PDF or s in IMAGES or s in office


def fit_width(w: int) -> int:
    return min(WIDTHS, key=lambda x: abs(x - w))


def render_pdf(data: bytes, page: int, width: int) -> bytes:
    import pypdfium2 as pdfium

    with _pdfium_lock:
        pdf = pdfium.PdfDocument(data)
        try:
            index = min(max(page, 1), len(pdf)) - 1
            p = pdf[index]
            scale = width / p.get_width()
            image = p.render(scale=scale).to_pil()
        finally:
            pdf.close()
    return _encode(image)


def render_image(data: bytes, page: int, width: int) -> bytes:
    from PIL import Image

    image = Image.open(io.BytesIO(data))
    try:
        image.seek(max(page, 1) - 1)  # TIFF multipage
    except EOFError:
        image.seek(0)
    image = image.convert("RGB")
    image.thumbnail((width, width * 2))
    return _encode(image)


def _encode(image) -> bytes:
    out = io.BytesIO()
    image.convert("RGB").save(out, "WEBP", quality=72, method=4)
    return out.getvalue()


class ThumbCache:
    """LRU en mémoire : (source, doc_id, empreinte, page, largeur) → WebP."""

    def __init__(self, max_items: int = 500):
        self.max_items = max_items
        self._items: OrderedDict[tuple, bytes] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: tuple) -> bytes | None:
        with self._lock:
            value = self._items.get(key)
            if value is not None:
                self._items.move_to_end(key)
            return value

    def put(self, key: tuple, value: bytes) -> None:
        with self._lock:
            self._items[key] = value
            self._items.move_to_end(key)
            while len(self._items) > self.max_items:
                self._items.popitem(last=False)


def render(data: bytes, filename: str, page: int, width: int, converter=None) -> bytes | None:
    """Vignette WebP de la page, ou None si le format n'en a pas."""
    s = suffix(filename)
    if s in IMAGES:
        return render_image(data, page, width)
    if s not in PDF:
        if converter is None or not converter.accepts(filename):
            return None
        data = converter.to_pdf(filename, data)
    return render_pdf(data, page, width)
