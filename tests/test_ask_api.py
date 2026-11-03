from fastapi.testclient import TestClient

from ged.answer import NO_RESULT, ask
from ged.api import create_app
from ged.sync import sync_source
from tests.conftest import FakeLLM


def test_acl_filtering_happens_before_llm(connector, store, embedder):
    sync_source(connector, store, embedder)
    llm = FakeLLM()
    answer = ask("grille des salaires", frozenset({"tous"}), store, embedder, llm)
    context = llm.last_user.split("Question :")[0]
    assert "confidentielle" not in context  # le texte RH n'est jamais envoyé au LLM
    assert all(c.path.startswith("Public/") for c in answer.citations)


def test_no_group_no_answer(connector, store, embedder):
    sync_source(connector, store, embedder)
    llm = FakeLLM()
    answer = ask("facture ACME", frozenset(), store, embedder, llm)
    assert answer.text == NO_RESULT and llm.last_user == ""


def test_citations_point_to_page(connector, store, embedder):
    sync_source(connector, store, embedder)
    answer = ask("conditions de paiement facture ACME 30 jours", frozenset({"compta"}), store, embedder, FakeLLM())
    assert answer.citations[0].path == "Compta/Factures/facture-acme.pdf"
    assert answer.citations[0].page == 2
    assert answer.citations[0].url.endswith("#page=2")


def test_unknown_citation_numbers_ignored(connector, store, embedder):
    sync_source(connector, store, embedder)
    answer = ask("horaires mairie", frozenset({"tous"}), store, embedder, FakeLLM("Voir [1] et [99]."))
    assert [c.n for c in answer.citations] == [1]


def test_duplicate_copies_appear_once(connector, store, embedder, share):
    (share / "Public/horaires-copie.txt").write_bytes((share / "Public/horaires.txt").read_bytes())
    sync_source(connector, store, embedder)
    llm = FakeLLM()
    ask("horaires ouverture mairie", frozenset({"tous"}), store, embedder, llm)
    assert llm.last_user.count("lundi au vendredi") == 1


ADMIN = ("admin", "pw")


def make_client(connector, store, embedder):
    return TestClient(create_app(store, embedder, FakeLLM(), {connector.name: connector}, admin_password="pw"))


def test_api_end_to_end(connector, store, embedder, share):
    client = make_client(connector, store, embedder)
    assert client.post("/sources/partage/sync").status_code == 401  # réservé à l'administrateur
    assert client.post("/sources/partage/sync", auth=("admin", "faux")).status_code == 401
    assert client.post("/sources/partage/sync", auth=ADMIN).json()["added"] == 4

    hits = client.get("/search", params={"q": "pénalité de retard"}, headers={"X-User-Groups": "compta"}).json()
    assert hits[0]["path"] == "Compta/contrat-fournisseur.docx"

    r = client.post("/ask", json={"question": "Quelle pénalité de retard ?"}, headers={"X-User-Groups": "compta"})
    url = r.json()["citations"][0]["url"].split("#")[0]
    assert client.get(url, headers={"X-User-Groups": "compta"}).status_code == 200
    assert client.get(url, headers={"X-User-Groups": "tous"}).status_code == 403


def test_api_rechecks_live_acl(connector, store, embedder, share):
    """Un fichier déplacé vers RH après indexation n'est plus téléchargeable par la compta."""
    client = make_client(connector, store, embedder)
    client.post("/sources/partage/sync", auth=ADMIN)
    hit = client.get("/search", params={"q": "pénalité"}, headers={"X-User-Groups": "compta"}).json()[0]
    (share / "Compta/contrat-fournisseur.docx").rename(share / "RH/contrat-fournisseur.docx")
    assert client.get(hit["url"].split("#")[0], headers={"X-User-Groups": "compta"}).status_code == 403


def test_api_unknown_source(connector, store, embedder):
    assert make_client(connector, store, embedder).post("/sources/inconnue/sync", auth=ADMIN).status_code == 404


# --- Réponse sans formulation par le LLM --------------------------------------------------

def test_without_llm_answer_is_ranked_extracts(connector, store, embedder):
    from ged.answer import EXTRACTS

    sync_source(connector, store, embedder)
    answer = ask("pénalité de retard", frozenset({"compta"}), store, embedder, None)
    assert not answer.generated and answer.text == EXTRACTS
    assert [c.n for c in answer.citations] == list(range(1, len(answer.hits) + 1))
    assert answer.citations[0].path.endswith("contrat-fournisseur.docx")


def test_generate_false_skips_llm(connector, store, embedder):
    sync_source(connector, store, embedder)
    llm = FakeLLM()
    answer = ask("pénalité de retard", frozenset({"compta"}), store, embedder, llm, generate=False)
    assert not answer.generated and llm.last_user == ""


def test_api_ask_without_formulation(connector, store, embedder):
    sync_source(connector, store, embedder)
    llm = FakeLLM()
    client = TestClient(create_app(store, embedder, llm, {"partage": connector}))
    body = client.post("/ask", json={"question": "Quelle pénalité de retard ?", "formuler": False},
                       headers={"X-User-Groups": "compta"}).json()
    assert body["formulee"] is False and llm.last_user == ""
    assert body["extraits"][0]["n"] == 1 and "Pénalité" in body["extraits"][0]["text"]
    assert body["citations"][0]["url"].startswith("/documents/partage/")

    body = client.post("/ask", json={"question": "Quelle pénalité de retard ?"},
                       headers={"X-User-Groups": "compta"}).json()
    assert body["formulee"] is True and body["extraits"] == []


def test_api_and_sandbox_with_no_llm_configured(connector, store, embedder):
    sync_source(connector, store, embedder)
    client = TestClient(create_app(store, embedder, None, {"partage": connector}, admin_password="pw"))
    assert client.get("/health").json()["llm"] is False
    body = client.post("/ask", json={"question": "Quelle pénalité de retard ?"},
                       headers={"X-User-Groups": "compta"}).json()
    assert body["formulee"] is False and body["extraits"]
    page = client.get("/admin/sandbox", auth=("admin", "pw")).text
    assert "Aucun LLM configuré" in page and "disabled" in page
    res = client.post("/admin/sandbox", auth=("admin", "pw"),
                      data={"question": "pénalité de retard", "groups": "compta", "formuler": "true"}).text
    assert "Extraits pertinents" in res and "contrat-fournisseur.docx" in res


def _many_pages(share):
    from tests.conftest import make_pdf

    make_pdf(share / "Compta" / "Factures" / "rapport-durand.pdf",
             [f"Durand maintenance chaudière, intervention {i}" for i in range(1, 6)])


def test_one_result_per_document(connector, store, embedder, share):
    _many_pages(share)
    sync_source(connector, store, embedder)
    groups = frozenset({"compta"})
    plain = ask("Durand maintenance", groups, store, embedder, None, k=6)
    paths = [c.path for c in plain.citations]
    assert len(paths) != len(set(paths))  # plusieurs passages d'un même document

    answer = ask("Durand maintenance", groups, store, embedder, None, k=6, per_document=True)
    paths = [d.path for d in answer.documents]
    assert len(paths) == len(set(paths)) and "Compta/Factures/rapport-durand.pdf" in paths
    rapport = next(d for d in answer.documents if d.path.endswith("rapport-durand.pdf"))
    assert len(rapport.pages) > 1 and all(u.endswith(f"#page={p}") for p, u in rapport.pages)
    assert rapport.url.endswith(f"#page={rapport.best.page}")
    assert [c.n for c in answer.citations] == [d.n for d in answer.documents]


def test_one_result_per_document_with_llm_numbers_documents(connector, store, embedder, share):
    _many_pages(share)
    sync_source(connector, store, embedder)
    llm = FakeLLM("Voir [1].")
    answer = ask("Durand maintenance", frozenset({"compta"}), store, embedder, llm, k=3, per_document=True)
    context = llm.last_user.split("Question :")[0]
    assert context.count("[1]") == 1 and "pages " in context and len(answer.documents) <= 3
    assert [c.path for c in answer.citations] == [answer.documents[0].path]


def test_per_document_in_api_and_search_page(connector, store, embedder, share):
    _many_pages(share)
    sync_source(connector, store, embedder)
    c = TestClient(create_app(store, embedder, None, {connector.name: connector}))
    h = {"X-User-Groups": "compta"}
    r = c.post("/ask", json={"question": "Durand maintenance", "par_document": True, "k": 5}, headers=h).json()
    paths = [d["path"] for d in r["documents"]]
    assert len(paths) == len(set(paths)) and r["extraits"] == []
    doc = next(d for d in r["documents"] if d["path"].endswith("rapport-durand.pdf"))
    assert len(doc["pages"]) > 1 and doc["pages"][0]["url"].startswith("/documents/")
    s = c.get("/search", params={"q": "Durand maintenance", "par_document": "true"}, headers=h).json()
    assert len({d["path"] for d in s}) == len(s)
    assert c.post("/ask", json={"question": "Durand maintenance"}, headers=h).json()["documents"] is None


def test_identifiers():
    from ged.answer import identifiers

    assert identifiers("G6006-270173-05") == ("G6006-270173-05",)
    assert identifiers("montant de la facture 2026-114 ?") == ("2026-114",)
    assert identifiers("factures de 2026 pour Durand") == ()  # une année n'est pas un identifiant


def _permis(share):
    (share / "Compta" / "permis-gruwe.txt").write_text(
        "Permis de conduire n° G6006-270173-05 délivré à Olivier Gruwe.", encoding="utf-8")
    (share / "Compta" / "permis-autre.txt").write_text(
        "Permis de conduire n° H1234-111111-01 délivré à Alice Martin.", encoding="utf-8")


def test_identifier_question_returns_only_documents_containing_it(connector, store, embedder, share):
    _permis(share)
    sync_source(connector, store, embedder)
    for per_document in (False, True):
        answer = ask("G6006-270173-05", frozenset({"compta"}), store, embedder, None, per_document=per_document)
        assert [c.path for c in answer.citations] == ["Compta/permis-gruwe.txt"], per_document
        assert answer.identifiers == ("G6006-270173-05",)
    llm = FakeLLM("C'est un permis [1].")
    answer = ask("à qui est le permis G6006-270173-05 ?", frozenset({"compta"}), store, embedder, llm)
    assert "H1234" not in llm.last_user and [c.path for c in answer.citations] == ["Compta/permis-gruwe.txt"]


def test_identifier_respects_rights_and_falls_back_when_absent(connector, store, embedder, share):
    _permis(share)
    sync_source(connector, store, embedder)
    answer = ask("G6006-270173-05", frozenset({"rh"}), store, embedder, None)
    assert all("permis-gruwe" not in c.path for c in answer.citations)
    answer = ask("permis Z9999-000000-00", frozenset({"compta"}), store, embedder, None)
    assert answer.identifiers == () and answer.identifiers_absent == ("Z9999-000000-00",) and answer.citations


def test_identifier_found_only_in_extracted_data(store, embedder):
    from ged.models import Acl, Chunk, DocMeta, DocumentRef, Status

    meta = DocMeta("PERMIS", "Permis de conduire", {"numero": {"valeur": "G6006-270173-05"}}, (), "u", "RECONNU", 0.9)
    for name, m in (("scan-1.pdf", meta), ("scan-2.pdf", None)):
        ref = DocumentRef("s", name, name, 1.0, 1)
        text = "Fiche Alambic : Permis de conduire" if m else "Permis de conduire, scan illisible"
        store.upsert_document(ref, "h" + name, Acl(frozenset({"g"})), Status.INDEXED, [Chunk(0, 1, text)],
                              embedder.embed([text]), meta=m)
    answer = ask("G6006-270173-05", frozenset({"g"}), store, embedder, None)
    assert [c.path for c in answer.citations] == ["scan-1.pdf"]


def test_thumbnails_respect_rights_and_formats(connector, store, embedder, share):
    from PIL import Image

    Image.new("RGB", (400, 600), "white").save(share / "Compta" / "scan.png")
    sync_source(connector, store, embedder)
    c = TestClient(create_app(store, embedder, None, {connector.name: connector}, admin_password="pw"))
    docs = {r.path: r for r in store.list_documents(limit=100)[0]}
    pdf = docs["Compta/Factures/facture-acme.pdf"]
    url = f"/vignettes/partage/{pdf.doc_id}?page=2&l=320"
    r = c.get(url, headers={"X-User-Groups": "compta"})
    assert r.status_code == 200 and r.headers["content-type"] == "image/webp"
    assert Image.open(__import__("io").BytesIO(r.content)).width == 320
    assert "private" in r.headers["cache-control"]
    assert c.get(url, headers={"X-User-Groups": "tous"}).status_code == 404  # pas le droit : introuvable
    assert c.get(url).status_code in (401, 404)  # sans identité : rien
    assert c.get(f"/admin/vignettes/partage/{pdf.doc_id}?page=1", auth=("admin", "pw")).status_code == 200

    png = docs["Compta/scan.png"] if "Compta/scan.png" in docs else None
    if png is not None:  # image indexée (OCR requis) : vignette aussi
        assert c.get(f"/vignettes/partage/{png.doc_id}", headers={"X-User-Groups": "compta"}).status_code == 200

    # résultats : vignette pour le PDF, pas pour le texte
    html = c.post("/admin/sandbox", data={"question": "facture ACME", "groups": "compta, tous"}, auth=("admin", "pw")).text
    assert f"/admin/vignettes/partage/{pdf.doc_id}" in html and "thumb-none" in html


def test_thumbnail_of_broken_file_is_a_placeholder(connector, store, embedder, share):
    sync_source(connector, store, embedder)
    doc = next(r for r in store.list_documents(limit=100)[0] if r.path.endswith("facture-acme.pdf"))
    (share / "Compta" / "Factures" / "facture-acme.pdf").write_bytes(b"%PDF-1.4 abime")
    c = TestClient(create_app(store, embedder, None, {connector.name: connector}))
    r = c.get(f"/vignettes/partage/{doc.doc_id}", headers={"X-User-Groups": "compta"})
    assert r.status_code == 200 and r.headers["content-type"] == "image/svg+xml"
    assert "x-vignette-erreur" not in r.headers  # la raison n'est donnée qu'à la console
    c = TestClient(create_app(store, embedder, None, {connector.name: connector}, admin_password="pw"))
    r = c.get(f"/admin/vignettes/partage/{doc.doc_id}", auth=("admin", "pw"))
    assert r.headers["content-type"] == "image/svg+xml" and r.headers["x-vignette-erreur"]


def test_thumbnail_of_scanned_pdf(store, embedder, tmp_path):
    from ged.thumbs import render
    from tests.test_alambic_light import make_scan

    make_scan(tmp_path / "scan.pdf")
    image = render((tmp_path / "scan.pdf").read_bytes(), "scan.pdf", 1, 160)
    assert image[:4] == b"RIFF"  # WebP
