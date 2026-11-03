"""Conversion des fichiers bureautiques en PDF (LibreOffice, via le service Gotenberg).

Pourquoi : Word n'enregistre pas sa pagination, python-docx voit tout le document comme une
seule page. Converti en PDF, le document a ses vraies pages (citations exactes), et Alambic
Light, qui n'accepte que PDF et images, peut le typer et en extraire les données.

Le PDF n'est pas conservé : il sert à l'extraction puis est oublié, comme le fichier d'origine.
Gotenberg tourne dans son propre conteneur : un fichier piégé ou qui fait planter LibreOffice
n'affecte pas l'API.

  POST /forms/libreoffice/convert   multipart « files » → PDF
  GET  /health                      état
"""
from __future__ import annotations

import logging
from pathlib import PurePosixPath

import httpx

log = logging.getLogger(__name__)

OFFICE_EXTENSIONS = frozenset({
    ".docx", ".doc", ".odt", ".rtf",  # traitement de texte
    ".xlsx", ".xls", ".ods",  # tableurs
    ".pptx", ".ppt", ".odp",  # présentations
})


class ConversionFailed(Exception):
    pass


class OfficeConverter:
    def __init__(self, url: str, *, timeout: float = 120.0, transport: httpx.BaseTransport | None = None):
        self.url = url.rstrip("/")
        self._http = httpx.Client(base_url=self.url, timeout=httpx.Timeout(timeout, connect=10.0), transport=transport)

    def accepts(self, filename: str) -> bool:
        return PurePosixPath(filename).suffix.lower() in OFFICE_EXTENSIONS

    def to_pdf(self, filename: str, data: bytes) -> bytes:
        name = PurePosixPath(filename).name
        try:
            r = self._http.post("/forms/libreoffice/convert", files={"files": (name, data)})
        except httpx.HTTPError as exc:
            raise ConversionFailed(f"service de conversion injoignable ({self.url}) : {exc}") from exc
        if r.status_code != 200 or not r.content.startswith(b"%PDF"):
            raise ConversionFailed(f"conversion en PDF impossible ({r.status_code} : {r.text[:200]})")
        return r.content

    def check(self) -> tuple[bool, str]:
        try:
            r = self._http.get("/health")
        except httpx.HTTPError as exc:
            return False, f"{self.url} injoignable : {exc}"
        if r.status_code != 200:
            return False, f"{self.url} répond {r.status_code}"
        return True, f"{self.url} opérationnel"

    def close(self) -> None:
        self._http.close()
