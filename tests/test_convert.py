"""Conversion PDF des fichiers bureautiques (service Gotenberg simulé)."""
from __future__ import annotations

import io

import httpx

from ged.connectors import FileSystemConnector
from ged.convert import OFFICE_EXTENSIONS, OfficeConverter
from ged.models import Status
from ged.sync import sync_source

from .conftest import ACL, make_docx

LONG = ["Contrat de maintenance avec la société Durand.", "Article 12 : pénalité de retard de 2 % par semaine."]


def pdf_bytes(pages: list[str]) -> bytes:
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    for text in pages:
        c.drawString(50, 800, text)
        c.showPage()
    c.save()
    return buf.getvalue()


class FakeGotenberg:
    """Rend un PDF d'une page par paragraphe du .docx reçu (pagination simulée)."""

    def __init__(self):
        self.calls: list[str] = []
        self.down = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("refused", request=request)
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "up"})
        assert request.url.path == "/forms/libreoffice/convert"
        body = request.content
        name = body.split(b'filename="', 1)[1].split(b'"', 1)[0].decode()
        self.calls.append(name)
        if name.endswith(".xlsx"):
            return httpx.Response(400, text="LibreOffice failed to process a document")
        from docx import Document

        data = body.split(b"\r\n\r\n", 1)[1].rsplit(b"\r\n--", 1)[0]
        paragraphs = [p.text for p in Document(io.BytesIO(data)).paragraphs if p.text.strip()]
        return httpx.Response(200, content=pdf_bytes(paragraphs), headers={"content-type": "application/pdf"})


def converter(fake: FakeGotenberg) -> OfficeConverter:
    return OfficeConverter("http://gotenberg.test", transport=httpx.MockTransport(fake.handler))


def test_docx_gets_real_pages(share, store, embedder):
    make_docx(share / "Compta" / "contrat-fournisseur.docx", LONG)
    fake = FakeGotenberg()
    connector = FileSystemConnector("partage", share, ACL, extensions=frozenset({".docx", ".pdf", ".txt"}))
    report = sync_source(connector, store, embedder, converter=converter(fake))
    assert report.converted == 1 and fake.calls == ["contrat-fournisseur.docx"]

    rows, _ = store.list_documents(q="contrat-fournisseur")
    chunks = store.document_chunks("partage", rows[0].doc_id)
    assert [c.page for c in chunks] == [1, 2]
    q = "pénalité de retard"
    hit = store.search(q, embedder.embed([q])[0], frozenset({"compta"}), k=1)[0]
    assert hit.path.endswith(".docx") and hit.page == 2  # lien vers l'original, page exacte


def test_without_converter_docx_is_single_page(share, store, embedder):
    make_docx(share / "Compta" / "contrat-fournisseur.docx", LONG)
    sync_source(FileSystemConnector("partage", share, ACL), store, embedder)
    rows, _ = store.list_documents(q="contrat-fournisseur")
    assert {c.page for c in store.document_chunks("partage", rows[0].doc_id)} == {1}


def test_converted_docx_is_sent_to_light_as_pdf(share, store, embedder):
    from .test_alambic_light import FakeLight, client

    light = FakeLight(pending_polls=0)
    connector = FileSystemConnector("partage", share, ACL)
    sync_source(connector, store, embedder, converter=converter(FakeGotenberg()), analyzer=client(light))
    sent = {r["filename"]: r for r in light.received}
    assert "contrat-fournisseur.docx.pdf" in sent  # avant : refusé (415) par Alambic Light
    assert sent["contrat-fournisseur.docx.pdf"]["identifiant"] == "Compta/contrat-fournisseur.docx"


def test_conversion_failure_is_retried(share, store, embedder):
    fake = FakeGotenberg()
    fake.down = True
    connector = FileSystemConnector("partage", share, ACL)
    report = sync_source(connector, store, embedder, converter=converter(fake))
    rows, _ = store.list_documents(q="contrat-fournisseur")
    assert rows[0].status == Status.ERROR and "injoignable" in rows[0].error
    assert any("contrat-fournisseur" in e for e in report.errors)

    fake.down = False
    sync_source(connector, store, embedder, converter=converter(fake))
    rows, _ = store.list_documents(q="contrat-fournisseur")
    assert rows[0].status == Status.INDEXED


def test_other_office_formats_indexable_with_converter(share, store, embedder, tmp_path, monkeypatch):
    from ged.config import load_connectors

    (share / "Compta" / "budget.xlsx").write_bytes(b"PK fake")
    cfg = tmp_path / "sources.yaml"
    cfg.write_text(f"sources:\n  - name: p\n    type: filesystem\n    root: {share.as_posix()}\n    acl: {{'': [compta]}}\n")
    connector = load_connectors(cfg, OFFICE_EXTENSIONS)["p"]
    assert ".xlsx" in connector.extensions and ".pptx" in connector.extensions
    report = sync_source(connector, store, embedder, converter=converter(FakeGotenberg()))
    assert any("budget.xlsx" in e and "400" in e for e in report.errors)


def test_check():
    fake = FakeGotenberg()
    assert converter(fake).check()[0]
    fake.down = True
    ok, detail = converter(fake).check()
    assert not ok and "injoignable" in detail


def test_citation_opens_office_document_as_pdf_at_page(share, store, embedder):
    from fastapi.testclient import TestClient

    from ged.api import create_app
    from tests.conftest import FakeLLM

    make_docx(share / "Compta" / "contrat-fournisseur.docx", LONG)
    conv = converter(FakeGotenberg())
    connector = FileSystemConnector("partage", share, ACL)
    sync_source(connector, store, embedder, converter=conv)
    http = TestClient(create_app(store, embedder, FakeLLM("Pénalité [1]."), {"partage": connector}, converter=conv))
    h = {"X-User-Groups": "compta"}

    url = http.post("/ask", json={"question": "pénalité de retard"}, headers=h).json()["citations"][0]["url"]
    assert "?pdf=true#page=" in url
    r = http.get(url.split("#")[0], headers=h)
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf" and r.content.startswith(b"%PDF")
    assert http.get(url.split("#")[0], headers={"X-User-Groups": "rh"}).status_code == 403  # droits revérifiés
    pdf_url = [x["url"] for x in http.get("/search", params={"q": "facture ACME"}, headers=h).json()]
    assert all("pdf=true" not in u for u in pdf_url if ".pdf" in u)
