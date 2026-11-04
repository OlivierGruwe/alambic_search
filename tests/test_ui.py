from fastapi.testclient import TestClient

from ged.api import create_app
from ged.models import RunStatus
from ged.sync import sync_source
from tests.conftest import FakeLLM

AUTH = ("admin", "secret")


def client_for(connector, store, embedder, password="secret"):
    app = create_app(store, embedder, FakeLLM(), {connector.name: connector}, admin_password=password)
    return TestClient(app), app


def test_console_without_any_account(connector, store, embedder):
    c, _ = client_for(connector, store, embedder, password=None)
    assert c.get("/admin", auth=AUTH).status_code == 401  # aucun compte : personne n'entre


def test_console_requires_credentials(connector, store, embedder):
    c, _ = client_for(connector, store, embedder)
    assert c.get("/admin").status_code == 401
    assert c.get("/admin", auth=("admin", "faux")).status_code == 401
    assert c.get("/admin", auth=AUTH).status_code == 200


def test_static_assets_are_public(connector, store, embedder):
    c, _ = client_for(connector, store, embedder)
    for path in ("console.css", "htmx.min.js", "favicon.svg", "alambic-ui/alambic.css", "alambic-ui/alambic.js",
                 "alambic-ui/fonts/plex-sans-400.woff2", "alambic-ui/fonts/comfortaa-700.woff2",
                 "alambic-ui/img/arondor.png", "alambic-ui/img/arondor-blanc.png"):
        assert c.get(f"/admin/static/{path}").status_code == 200, path


def test_empty_dashboard_invites_first_sync(connector, store, embedder):
    c, _ = client_for(connector, store, embedder)
    html = c.get("/admin", auth=AUTH).text
    assert "L'index est vide" in html and "Jamais synchronisée" in html


def test_background_sync_and_history(connector, store, embedder):
    c, app = client_for(connector, store, embedder)
    r = c.post("/admin/sources/partage/sync", auth=AUTH)
    assert r.status_code == 200
    import time

    runs = None
    for _ in range(100):  # attendre la fin du thread de synchro
        runs = store.list_runs("partage")
        if runs and runs[0].status != RunStatus.RUNNING:
            break
        time.sleep(0.05)
    assert runs[0].status == RunStatus.OK and runs[0].report["added"] == 4
    html = c.get("/admin/sources/partage/runs", auth=AUTH).text
    assert "Terminée" in html and "hx-trigger" not in html  # plus de rafraîchissement automatique
    page = c.get("/admin/sources", auth=AUTH).text
    assert ">Compta<" in page and ">rh<" in page and "4</b> indexés" in page


def test_dashboard_after_sync(connector, store, embedder, share):
    (share / "Public/casse.pdf").write_bytes(b"%PDF-1.4 pas un pdf")
    c, _ = client_for(connector, store, embedder)
    c.post("/sources/partage/sync", auth=AUTH)
    html = c.get("/admin", auth=AUTH).text
    assert "documents connus de l'index" in html
    assert "seg-indexed" in html and "seg-error" in html
    assert "casse.pdf" in html  # dans « À traiter »


def test_documents_filter_and_detail(connector, store, embedder):
    sync_source(connector, store, embedder)
    c, _ = client_for(connector, store, embedder)
    html = c.get("/admin/documents", params={"q": "facture"}, auth=AUTH).text
    assert "facture-acme.pdf" in html and "salaires.txt" not in html
    fragment = c.get("/admin/documents", params={"status": "indexed"}, auth=AUTH,
                     headers={"HX-Request": "true", "HX-Target": "doc-results"}).text
    assert "<html" not in fragment and "4 documents" in fragment
    doc = next(r for r in store.list_documents()[0] if r.path.endswith("facture-acme.pdf"))
    detail = c.get(f"/admin/documents/{doc.source}/{doc.doc_id}", auth=AUTH).text
    assert "page 2" in detail and "compta" in detail and "direction" in detail


def test_sandbox_respects_groups_and_is_not_counted(connector, store, embedder):
    sync_source(connector, store, embedder)
    c, _ = client_for(connector, store, embedder)
    html = c.post("/admin/sandbox", data={"question": "grille des salaires", "groups": "tous", "k": 8}, auth=AUTH).text
    assert "confidentielle" not in html and "Public/horaires.txt" in html
    html = c.post("/admin/sandbox", data={"question": "grille des salaires", "groups": "rh"}, auth=AUTH).text
    assert "RH/salaires.txt" in html
    assert store.question_activity(1)[-1][1] == 0  # le bac à sable n'entre pas dans les stats
    c.post("/ask", json={"question": "horaires de la mairie"}, headers={"X-User-Groups": "tous"})
    assert store.question_activity(1)[-1][1] == 1


def test_sandbox_reports_engine_failure(connector, store, embedder):
    sync_source(connector, store, embedder)

    class DownLLM:
        def complete(self, system, user):
            raise ConnectionError("serveur LLM injoignable")

    app = create_app(store, embedder, DownLLM(), {connector.name: connector}, admin_password="secret")
    html = TestClient(app).post("/admin/sandbox", data={"question": "horaires", "groups": "tous", "formuler": "true"},
                                     auth=AUTH).text
    assert "serveur LLM injoignable" in html


def test_shared_bandeau_on_console_and_user_pages(connector, store, embedder):
    """Charte Alambic : même bandeau (marque, compte et son menu) partout, sans portail."""
    c, _ = client_for(connector, store, embedder)
    html = c.get("/admin/documents", auth=AUTH).text
    assert 'class="al-bar"' in html and "<b>alambic</b> <span>search</span>" in html
    assert 'class="al-nav"' in html and 'aria-current="page"' in html
    assert "Applications" not in html and "Portail" not in html
    assert "Administrateur" in html and 'href="/admin/utilisateurs"' in html  # compte admin (HTTP Basic)
    assert "alambic-ui/alambic.css" in html


def test_document_id_with_slashes_opens_detail(connector, store, embedder):
    """Sources dont l'identifiant est un chemin (S3, FTP…) : « __out__/auto/doc 1#a.pdf »."""
    from ged.models import Acl, DocumentRef, Status

    doc_id = "__out__/auto/doc 1#a.pdf"
    ref = DocumentRef("partage", doc_id, doc_id, 0.0, 10)
    store.upsert_document(ref, "h", Acl(frozenset({"compta"})), Status.ERROR, [], [], error="illisible")
    c, _ = client_for(connector, store, embedder)
    table = c.get("/admin/documents", auth=AUTH).text
    assert 'hx-get="/admin/documents/partage/__out__/auto/doc%201%23a.pdf"' in table
    detail = c.get("/admin/documents/partage/__out__/auto/doc%201%23a.pdf", auth=AUTH)
    assert detail.status_code == 200 and "illisible" in detail.text
