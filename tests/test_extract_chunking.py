import pytest

from ged.chunking import chunk_pages
from ged.extract import UnsupportedFormat, extract_pages, has_text
from ged.models import Page


def test_pdf_pages_are_kept_separate(share):
    pages = extract_pages("f.pdf", (share / "Compta/Factures/facture-acme.pdf").read_bytes())
    assert [p.number for p in pages] == [1, 2]
    assert "4 850" in pages[0].text
    assert "30 jours" in pages[1].text


def test_docx_text(share):
    pages = extract_pages("c.docx", (share / "Compta/contrat-fournisseur.docx").read_bytes())
    assert "Pénalité de retard" in pages[0].text


def test_txt_cp1252_fallback():
    pages = extract_pages("a.txt", "Été à Payzac".encode("cp1252"))
    assert pages[0].text == "Été à Payzac"


def test_unsupported_and_empty():
    with pytest.raises(UnsupportedFormat):
        extract_pages("a.xyz", b"")
    assert not has_text([Page(1, "   "), Page(2, "")])


def test_chunks_never_cross_pages():
    chunks = chunk_pages([Page(1, "un"), Page(2, "deux")])
    assert [(c.page, c.text) for c in chunks] == [(1, "un"), (2, "deux")]


def test_chunk_size_and_overlap():
    para = "x" * 3000
    chunks = chunk_pages([Page(1, para)], max_chars=1000, overlap=200)
    assert all(len(c.text) <= 1000 for c in chunks)
    assert chunks[0].text[-200:] == chunks[1].text[:200]
    assert "".join(c.text[200 if i else 0 :] for i, c in enumerate(chunks)) == para


def test_paragraphs_are_grouped():
    text = "\n\n".join(f"Paragraphe {i}." for i in range(5))
    chunks = chunk_pages([Page(1, text)], max_chars=1000, overlap=0)
    assert len(chunks) == 1 and chunks[0].text.count("Paragraphe") == 5
