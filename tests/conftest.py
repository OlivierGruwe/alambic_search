from pathlib import Path

import pytest

from ged.connectors import FileSystemConnector
from ged.embeddings import HashEmbedder
from ged.store import MemoryStore

ACL = {"": ["direction"], "Public": ["tous"], "Compta": ["compta", "direction"], "RH": ["rh"]}


def make_pdf(path: Path, pages: list[str]) -> None:
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(path))
    for text in pages:
        y = 800
        for line in text.split("\n"):
            c.drawString(50, y, line)
            y -= 16
        c.showPage()
    c.save()


def make_docx(path: Path, paragraphs: list[str]) -> None:
    from docx import Document

    doc = Document()
    for p in paragraphs:
        doc.add_paragraph(p)
    doc.save(str(path))


@pytest.fixture
def share(tmp_path: Path) -> Path:
    root = tmp_path / "partage"
    (root / "Public").mkdir(parents=True)
    (root / "Compta" / "Factures").mkdir(parents=True)
    (root / "RH").mkdir()
    (root / "Public" / "horaires.txt").write_text(
        "La mairie est ouverte du lundi au vendredi de 9h à 12h.", encoding="utf-8"
    )
    make_pdf(
        root / "Compta" / "Factures" / "facture-acme.pdf",
        ["Facture ACME n° 2026-114\nMontant total : 4 850 euros HT", "Conditions : paiement à 30 jours"],
    )
    make_docx(
        root / "Compta" / "contrat-fournisseur.docx",
        ["Contrat de maintenance avec la société Durand.", "Pénalité de retard : 2 % par semaine de retard."],
    )
    (root / "RH" / "salaires.txt").write_text("Grille des salaires confidentielle 2026.", encoding="utf-8")
    (root / "Public" / "~$verrou.docx").write_bytes(b"lock")  # fichier de verrou Office, ignoré
    (root / "Public" / "image.png").write_bytes(b"\x89PNG")  # extension non gérée, ignorée
    return root


@pytest.fixture
def connector(share: Path) -> FileSystemConnector:
    return FileSystemConnector("partage", share, ACL)


@pytest.fixture
def store() -> MemoryStore:
    return MemoryStore()


@pytest.fixture
def embedder() -> HashEmbedder:
    return HashEmbedder()


class FakeLLM:
    """Renvoie une réponse qui cite le premier extrait, et garde la trace du prompt."""

    def __init__(self, reply: str = "Réponse fondée sur le document [1]."):
        self.reply = reply
        self.last_user = ""

    def complete(self, system: str, user: str) -> str:
        self.last_user = user
        return self.reply


@pytest.fixture
def llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture(autouse=True)
def trust_identity_headers(monkeypatch):
    """Les tests d'API passent l'identité par en-têtes (X-User-Groups) ; en production c'est
    désactivé par défaut (voir tests/test_auth.py)."""
    monkeypatch.setenv("TRUST_IDENTITY_HEADERS", "true")
