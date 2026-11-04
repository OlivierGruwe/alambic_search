"""Alambic Light : OCR des scans, type et données des documents, reprise des validations.

Alambic Light est simulé (httpx.MockTransport) selon son contrat : POST /v1/documents/typage
→ 200 ou 202 + Location, GET /v1/documents/{uuid}, /v1/token, /v1/referentiel.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from ged.alambic_light import FICHE_CHUNK_NO, AlambicLight, fiche_text
from ged.answer import ask, build_context
from ged.config import ConfigError, load_analyzer, load_connectors
from ged.connectors import FileSystemConnector
from ged.models import DocMeta, Status
from ged.store import MemoryStore
from ged.sync import NO_TEXT, sync_source

from .conftest import ACL, make_pdf

SCAN_TEXT = "FACTURE DURAND n° F-2026-031\nMontant TTC : 1 200,00 euros\nÉchéance : 30/11/2026"


def make_scan(path: Path) -> None:
    """PDF sans couche texte (un rectangle) : ce que produit un scanner."""
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(path))
    c.rect(50, 500, 300, 200, fill=1)
    c.showPage()
    c.save()


def champ(valeur, confiance: float = 0.95, *, a_verifier: bool = False, source: str = "ia", brut=None) -> dict:
    """ChampExtrait d'Alambic Light (schemas.py)."""
    return {"valeur": valeur, "confiance": confiance, "source": source, "aVerifier": a_verifier, "brut": brut}


class FakeLight:
    """Imite Alambic Light. `results` : réponse finale par nom de fichier."""

    def __init__(self, *, text_route: bool = True, pending_polls: int = 1, oauth: bool = False):
        self.text_route = text_route
        self.pending_polls = pending_polls
        self.oauth = oauth
        self.docs: dict[str, dict] = {}
        self.polls: dict[str, int] = {}
        self.calls: list[str] = []
        self.received: list[dict] = []
        self.tokens = 0
        self.expire_next_token = False
        self.down = False
        self.scopes = ["lecture", "typage"]

    # réponse selon le fichier reçu
    def decide(self, filename: str, form: dict) -> dict:
        if "scan" in filename:
            return {"typeDocument": "FACTURE", "statut": "RECONNU", "confiance": 0.93, "ocr": "tesseract",
                    "donnees": {
                        "fournisseur": champ("Durand SA"), "montantTTC": champ("1200.00", 0.4, a_verifier=True),
                        "dateEcheance": champ("2026-11-30"),
                        "lignes": champ([{"designation": "Maintenance", "montant": "1000.00"}])},
                    "donneesAVerifier": ["montantTTC"],
                    "verification": {"statut": "A_VERIFIER", "aVerifier": ["montantTTC"], "champs": {}},
                    "_texte": SCAN_TEXT}
        if "rib" in filename:
            return {"typeDocument": "RIB", "statut": "RECONNU", "confiance": 0.99,
                    "ocr": "natif", "donnees": {"iban": champ("FR76 3000 6000 0112 3456 7890 189", 1.0)},
                    "verification": {"statut": "AUTO", "aVerifier": [], "champs": {}}, "_texte": None}
        if "illisible" in filename:
            return {"typeDocument": None, "statut": "ERREUR", "erreur": "Fichier abîmé"}
        return {"typeDocument": "INCONNU", "statut": "NON_RECONNU", "confiance": 0.2}

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(f"{request.method} {path}")
        if self.down:
            raise httpx.ConnectError("refused", request=request)
        if path == "/health":
            return httpx.Response(200, json={"status": "ok", "version": "1.4.0", "build": "x"})
        if path == "/v1/token":
            self.tokens += 1
            return httpx.Response(200, json={"access_token": f"jwt-{self.tokens}", "expires_in": 3600})
        if self.oauth:
            if request.headers.get("authorization") != f"Bearer jwt-{self.tokens}" or self.expire_next_token:
                self.expire_next_token = False
                return httpx.Response(401, json={"detail": "Jeton expiré"})
        elif request.headers.get("x-api-key") != "cle-test":
            return httpx.Response(401, json={"detail": "Clé invalide"})

        if path == "/v1/moi":
            return httpx.Response(200, json={"client": "alambic-search", "scopes": self.scopes, "via": "cle"})
        if path == "/v1/referentiel":
            return httpx.Response(200, json=[{"code": "FACTURE", "libelle": "Facture", "description": ""},
                                             {"code": "RIB", "libelle": "Relevé d'identité bancaire",
                                              "description": ""}])
        if request.method == "POST" and path == "/v1/documents/typage":
            body = request.content.decode("latin-1")
            filename = body.split('filename="', 1)[1].split('"', 1)[0]
            if filename.endswith(".docx"):
                return httpx.Response(415, json={"detail": "Formats acceptés : PDF, PNG, JPEG, TIFF, WEBP"})
            form = {k: body.split(f'name="{k}"', 1)[1].split("\r\n\r\n", 1)[1].split("\r\n", 1)[0]
                    for k in ("source", "identifiant")}
            self.received.append({"filename": filename, **form, "ia": request.url.params.get("ia"),
                                  "ocr_only": request.url.params.get("ocr_only"),
                                  "ocr": request.url.params.get("ocr")})
            uid = f"uuid-{len(self.docs) + 1}"
            self.docs[uid] = {"uuid": uid, **self.decide(filename, form)}
            self.polls[uid] = self.pending_polls
            if self.pending_polls:
                return httpx.Response(202, json={"uuid": uid, "statut": "EN_COURS"},
                                      headers={"Location": f"/v1/documents/{uid}"})
            return httpx.Response(200, json=self.public(uid))
        if path.endswith("/texte"):
            if not self.text_route:
                return httpx.Response(404, json={"detail": "Not Found"})
            uid = path.split("/")[3]
            texte = self.docs.get(uid, {}).get("_texte")
            if uid not in self.docs:
                return httpx.Response(404, json={"detail": "uuid inconnu ou expiré"})
            return httpx.Response(200, json={"uuid": uid, "pages": [{"numero": 1, "texte": texte or ""}]})
        if request.method == "GET" and path.startswith("/v1/documents/"):
            uid = path.rsplit("/", 1)[1]
            if uid not in self.docs:
                return httpx.Response(404, json={"detail": "uuid inconnu ou expiré"})
            if self.polls.get(uid):
                self.polls[uid] -= 1
                return httpx.Response(200, json={"uuid": uid, "statut": "EN_COURS"})
            return httpx.Response(200, json=self.public(uid))
        return httpx.Response(404, json={"detail": "Not Found"})

    def public(self, uid: str) -> dict:
        return {k: v for k, v in self.docs[uid].items() if not k.startswith("_") and v is not None}

    def validate(self, uid: str, donnees: dict) -> None:
        """Un gestionnaire valide le document dans Alambic Light."""
        self.docs[uid].update(
            statut="VALIDE", donnees={k: champ(v, 1.0, source="regle") for k, v in donnees.items()},
            donneesAVerifier=[], verification={"statut": "VALIDE", "aVerifier": [], "champs": {}},
            validation={"par": "gestionnaire", "le": "2026-11-03T10:00:00+00:00", "typeCorrige": False,
                        "champsCorriges": ["montantTTC"]})


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr("ged.alambic_light.time.sleep", lambda s: None)


def client(fake: FakeLight, **kw) -> AlambicLight:
    auth = {"client_id": "search", "client_secret": "secret"} if fake.oauth else {"api_key": "cle-test"}
    return AlambicLight("http://light.test", transport=httpx.MockTransport(fake.handler), **auth, **kw)


@pytest.fixture
def scans(share: Path) -> Path:
    make_scan(share / "Compta" / "Factures" / "scan-durand.pdf")
    make_scan(share / "Compta" / "Factures" / "illisible.pdf")
    return share


def by_path(store, path_part: str):
    rows, _ = store.list_documents(q=path_part)
    assert len(rows) == 1, rows
    return rows[0]


# --- Scans : le texte vient de l'OCR d'Alambic Light ------------------------------------

def test_scan_becomes_searchable_with_type_and_fields(scans, store, embedder):
    fake = FakeLight()
    connector = FileSystemConnector("partage", scans, ACL)
    report = sync_source(connector, store, embedder, analyzer=client(fake, send_all=False))

    doc = by_path(store, "scan-durand")
    assert doc.status == Status.INDEXED
    assert doc.meta.doc_type == "FACTURE" and doc.meta.label == "Facture"
    assert doc.meta.fields["fournisseur"]["valeur"] == "Durand SA"
    assert doc.meta.to_check == ("montantTTC",)
    assert report.analysed == 2  # le scan et l'illisible ; pas les PDF qui ont du texte (envoi « scans »)

    chunks = store.document_chunks("partage", doc.doc_id)
    assert chunks[0].chunk_no == FICHE_CHUNK_NO
    assert "Montant TTC : 1200.00 (à vérifier)" in chunks[0].text
    assert any("F-2026-031" in c.text for c in chunks[1:])  # texte OCR indexé

    hits = store.search("facture Durand F-2026-031", embedder.embed(["facture Durand F-2026-031"])[0],
                        frozenset({"compta"}), k=5)
    assert hits and hits[0].path.endswith("scan-durand.pdf") and hits[0].doc_label == "Facture"


def test_pointer_and_options_sent_to_light(scans, store, embedder):
    fake = FakeLight()
    connector = FileSystemConnector("partage", scans, ACL)
    sync_source(connector, store, embedder, analyzer=client(fake, send_all=False, ia=False))
    sent = {r["filename"]: r for r in fake.received}
    assert sent["scan-durand.pdf"]["source"] == "alambic-search:partage"
    assert sent["scan-durand.pdf"]["identifiant"] == "Compta/Factures/scan-durand.pdf"
    assert sent["scan-durand.pdf"]["ia"] == "false"


def test_light_error_on_scan_marks_document_in_error(scans, store, embedder):
    connector = FileSystemConnector("partage", scans, ACL)
    report = sync_source(connector, store, embedder, analyzer=client(FakeLight(), send_all=False))
    doc = by_path(store, "illisible")
    assert doc.status == Status.ERROR and "Fichier abîmé" in doc.error
    assert any("illisible" in e for e in report.errors)
    # refus définitif pour ce contenu : pas renvoyé tant que le fichier ne change pas
    report = sync_source(connector, store, embedder, analyzer=client(FakeLight(), send_all=False))
    assert report.analysed == 0 and not report.errors


def test_without_light_scans_stay_ocr_required(scans, store, embedder):
    connector = FileSystemConnector("partage", scans, ACL)
    sync_source(connector, store, embedder)
    assert by_path(store, "scan-durand").status == Status.OCR_REQUIRED


def test_fiche_only_when_light_does_not_expose_text(scans, store, embedder):
    fake = FakeLight(text_route=False)
    connector = FileSystemConnector("partage", scans, ACL)
    sync_source(connector, store, embedder, analyzer=client(fake, send_all=False))
    doc = by_path(store, "scan-durand")
    assert doc.status == Status.INDEXED and doc.error == NO_TEXT
    chunks = store.document_chunks("partage", doc.doc_id)
    assert [c.chunk_no for c in chunks] == [FICHE_CHUNK_NO]
    # route absente : plus demandée pour les documents suivants
    assert sum(c.endswith("/texte") for c in fake.calls) == 1


# --- Documents avec texte : type et données en plus du texte -----------------------------

def test_send_all_types_text_pdfs_and_keeps_local_text(share, store, embedder):
    make_pdf(share / "Compta" / "rib-durand.pdf", ["Relevé d'identité bancaire Durand SA", "IBAN FR76 3000 6000"])
    connector = FileSystemConnector("partage", share, ACL)
    report = sync_source(connector, store, embedder, analyzer=client(FakeLight(pending_polls=0)))

    rib = by_path(store, "rib-durand")
    assert rib.meta.doc_type == "RIB" and rib.meta.label == "Relevé d'identité bancaire"
    texts = [c.text for c in store.document_chunks("partage", rib.doc_id)]
    assert texts[0].startswith("Fiche Alambic : Relevé d'identité bancaire")
    assert any("Relevé d'identité bancaire Durand SA" in t for t in texts[1:])  # texte du PDF, pas l'OCR

    facture = by_path(store, "facture-acme")  # non reconnu : pas de type, texte indexé normalement
    assert facture.meta.doc_type is None and facture.status == Status.INDEXED
    # .docx refusé (415) par Alambic Light : seulement le texte local ; .txt jamais envoyé
    contrat = by_path(store, "contrat-fournisseur")
    assert contrat.status == Status.INDEXED and contrat.meta is None
    assert report.analysed == 2 and not report.errors


def test_type_filter_in_search_and_ask(scans, store, embedder, llm):
    connector = FileSystemConnector("partage", scans, ACL)
    sync_source(connector, store, embedder, analyzer=client(FakeLight(), send_all=False))
    q = "montant facture"
    only = store.search(q, embedder.embed([q])[0], frozenset({"compta"}), k=10, doc_types=frozenset({"FACTURE"}))
    assert only and all(h.path.endswith("scan-durand.pdf") for h in only)
    assert store.type_counts() == [("FACTURE", "Facture", 1)]

    answer = ask("Quel est le montant de la facture Durand ?", frozenset({"compta"}), store, embedder, llm,
                 doc_types=frozenset({"FACTURE"}))
    assert "Facture, page 1" in llm.last_user
    assert "(à vérifier)" in llm.last_user
    assert answer.citations[0].path.endswith("scan-durand.pdf")


def test_acl_still_applies_to_light_documents(scans, store, embedder):
    connector = FileSystemConnector("partage", scans, ACL)
    sync_source(connector, store, embedder, analyzer=client(FakeLight(), send_all=False))
    q = "facture Durand"
    hits = store.search(q, embedder.embed([q])[0], frozenset({"rh"}), k=10)
    assert hits and not any("Compta/" in h.path for h in hits)


# --- Reprise des validations faites dans Alambic Light -----------------------------------

def test_validation_in_light_is_picked_up_on_next_sync(scans, store, embedder):
    fake = FakeLight()
    analyzer = client(fake, send_all=False)
    connector = FileSystemConnector("partage", scans, ACL)
    sync_source(connector, store, embedder, analyzer=analyzer)
    doc = by_path(store, "scan-durand")

    report = sync_source(connector, store, embedder, analyzer=analyzer)  # rien de neuf
    assert report.validated == 0 and by_path(store, "scan-durand").meta.to_check == ("montantTTC",)

    fake.validate(doc.meta.light_uuid, {"fournisseur": "Durand SA", "montantTTC": "1250.00"})
    report = sync_source(connector, store, embedder, analyzer=analyzer)
    assert report.validated == 1
    meta = by_path(store, "scan-durand").meta
    assert meta.validated and meta.to_check == () and meta.fields["montantTTC"]["valeur"] == "1250.00"
    fiche = store.document_chunks("partage", doc.doc_id)[0].text
    assert "(données validées)" in fiche and "1250.00" in fiche and "à vérifier" not in fiche
    assert len(store.document_chunks("partage", doc.doc_id)) == 2  # fiche remplacée, texte inchangé

    calls = len(fake.calls)
    sync_source(connector, store, embedder, analyzer=analyzer)
    assert len(fake.calls) == calls  # validé : plus relu


def test_expired_result_stops_being_polled(scans, store, embedder):
    fake = FakeLight()
    analyzer = client(fake, send_all=False)
    connector = FileSystemConnector("partage", scans, ACL)
    sync_source(connector, store, embedder, analyzer=analyzer)
    fake.docs.clear()  # résultats purgés côté Alambic Light
    sync_source(connector, store, embedder, analyzer=analyzer)
    assert by_path(store, "scan-durand").meta.light_status == "EXPIRE"
    assert store.pending_reviews("partage") == []


# --- Robustesse et authentification -------------------------------------------------------

def test_light_down_marks_errors_then_retries(scans, store, embedder):
    fake = FakeLight()
    fake.down = True
    analyzer = client(fake, send_all=False)
    connector = FileSystemConnector("partage", scans, ACL)
    report = sync_source(connector, store, embedder, analyzer=analyzer)
    assert by_path(store, "scan-durand").status == Status.ERROR
    assert any("injoignable" in e for e in report.errors)

    fake.down = False
    report = sync_source(connector, store, embedder, analyzer=analyzer)
    assert by_path(store, "scan-durand").status == Status.INDEXED


def test_oauth_token_obtained_and_renewed(scans, store, embedder):
    fake = FakeLight(oauth=True)
    analyzer = client(fake, send_all=False)
    connector = FileSystemConnector("partage", scans, ACL)
    sync_source(connector, store, embedder, analyzer=analyzer)
    assert fake.tokens == 1
    fake.expire_next_token = True
    analyzer.result(by_path(store, "scan-durand").meta.light_uuid)
    assert fake.tokens == 2


def test_wrong_key_is_reported_clearly(scans, store, embedder):
    analyzer = AlambicLight("http://light.test", api_key="mauvaise", transport=httpx.MockTransport(FakeLight().handler),
                            send_all=False)
    connector = FileSystemConnector("partage", scans, ACL)
    report = sync_source(connector, store, embedder, analyzer=analyzer)
    assert any("refuse l'accès (401" in e for e in report.errors)


def test_parallel_analysis_indexes_everything(share, store, embedder):
    for i in range(12):
        make_scan(share / "Compta" / f"scan-{i:02d}.pdf")
    connector = FileSystemConnector("partage", share, ACL)
    report = sync_source(connector, store, embedder, analyzer=client(FakeLight(), send_all=False, concurrency=4))
    assert report.analysed == 12 and not report.errors
    rows, total = store.list_documents(doc_type="FACTURE", limit=100)
    assert total == 12


# --- Fiche ---------------------------------------------------------------------------------

def test_fiche_text_renders_nested_values_and_flags():
    meta = DocMeta("FACTURE", "Facture", {
        "numeroFacture": "F-1", "montant_ttc": {"valeur": "10.00", "confiance": 0.4}, "vide": None,
        "lignes": [{"designation": "A", "qte": 2}, {"designation": "B", "qte": 1}], "payee": False,
    }, to_check=("montant_ttc",))
    text = fiche_text(meta)
    assert text.splitlines()[0] == "Fiche Alambic : Facture"
    assert "Numero Facture : F-1" in text
    assert "Montant ttc : 10.00 (à vérifier)" in text
    assert "Lignes : 2 éléments : Designation A, Qte 2 ; Designation B, Qte 1" in text
    assert "Payee : non" in text and "Vide" not in text
    assert fiche_text(DocMeta(None, None)) is None


def test_context_shows_document_type(store):
    from ged.models import Hit

    ctx = build_context([Hit("s", "1", "a/f.pdf", 2, "texte", "h", 1.0, "Facture")])
    assert ctx.startswith("[1] a/f.pdf (Facture, page 2)")


# --- Configuration -------------------------------------------------------------------------

def write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "sources.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_config_loads_light_and_adds_image_formats(tmp_path, share, monkeypatch):
    monkeypatch.setenv("LIGHT_URL", "http://light:8000")
    monkeypatch.setenv("LIGHT_KEY", "k")
    cfg = write(tmp_path, f"""
alambic_light:
  url: ${{LIGHT_URL}}
  api_key_env: LIGHT_KEY
  envoyer: scans
  ia: false
  concurrence: 3
sources:
  - name: p
    type: filesystem
    root: {share.as_posix()}
""")
    analyzer = load_analyzer(cfg)
    assert analyzer.url == "http://light:8000" and not analyzer.send_all and not analyzer.ia
    assert analyzer.concurrency == 3
    assert ".tiff" in load_connectors(cfg)["p"].extensions


def test_config_without_light(tmp_path, share):
    cfg = write(tmp_path, f"sources:\n  - name: p\n    type: filesystem\n    root: {share.as_posix()}\n")
    assert load_analyzer(cfg) is None
    assert ".png" not in load_connectors(cfg)["p"].extensions


@pytest.mark.parametrize("body, message", [
    ("alambic_light:\n  url: http://x\n  api_key: en-clair\n", "api_key_env"),
    ("alambic_light:\n  url: http://x\n", "requis"),
    ("alambic_light:\n  url: http://x\n  api_key_env: K\n  envoyer: parfois\n", "tous"),
])
def test_config_errors(tmp_path, monkeypatch, body, message):
    monkeypatch.setenv("K", "k")
    with pytest.raises(ConfigError, match=message):
        load_analyzer(write(tmp_path, body))


# --- Console --------------------------------------------------------------------------------

def test_console_shows_type_fields_and_checks(scans, store, embedder, llm):
    from fastapi.testclient import TestClient

    from ged.api import create_app

    connector = FileSystemConnector("partage", scans, ACL)
    analyzer = client(FakeLight(), send_all=False)
    sync_source(connector, store, embedder, analyzer=analyzer)
    app = create_app(store, embedder, llm, {"partage": connector}, admin_password="pw", analyzer=analyzer)
    with TestClient(app) as http:
        auth = ("admin", "pw")
        page = http.get("/admin/documents?type=FACTURE", auth=auth).text
        assert "scan-durand.pdf" in page and "facture-acme.pdf" not in page
        assert "1 à vérifier" in page and "Facture (1)" in page
        doc = by_path(store, "scan-durand")
        detail = http.get(f"/admin/documents/partage/{doc.doc_id}", auth=auth).text
        assert "Durand SA" in detail and "à vérifier" in detail and "Fiche (type et données" in detail
        assert "Alambic Light (http://light.test)" in http.get("/admin/sources", auth=auth).text
        sb = http.post("/admin/sandbox", auth=auth,
                       data={"question": "montant facture Durand", "groups": "compta", "doc_type": "FACTURE"})
        assert sb.status_code == 200 and "scan-durand.pdf" in sb.text
        api = http.get("/search", params={"q": "facture Durand", "types": "FACTURE"},
                       headers={"X-User-Groups": "compta"}).json()
        assert api and all(h["type"] == "Facture" for h in api)
        assert json.loads(http.post("/ask", json={"question": "montant ?", "types": ["RIB"]},
                                    headers={"X-User-Groups": "compta"}).text)["citations"] == []


def test_to_check_merges_all_light_signals_and_shows_invalid_raw_value():
    from ged.alambic_light import _to_check

    rec = {"donneesAVerifier": ["iban"], "verification": {"aVerifier": ["lignes.2.montant", "iban"]}}
    fields = {"iban": {"valeur": None, "brut": "FR76 1234", "aVerifier": True},
              "date": {"valeur": "2026-11-03", "aVerifier": True}, "lignes": {"valeur": []}}
    assert _to_check(rec, fields) == ("iban", "date", "lignes")
    text = fiche_text(DocMeta("RIB", "RIB", fields, to_check=_to_check(rec, fields)))
    assert "Iban : FR76 1234 (format invalide) (à vérifier)" in text


# --- Contrôle de connexion (démarrage de l'API, make light-check) -------------------------

def test_check_all_good():
    result = client(FakeLight()).check()
    assert result["ok"]
    labels = {label: ok for label, ok, _ in result["etapes"]}
    assert labels == {"Service joignable": True, "Identifiants": True, "Droit « typage »": True,
                      "Droit « lecture »": True, "Texte OCR": True}


def test_check_warnings_do_not_block():
    fake = FakeLight(text_route=False)
    fake.scopes = ["typage"]
    result = client(fake).check()
    assert result["ok"]
    assert [ok for _, ok, _ in result["etapes"]][-2:] == [None, None]


def test_check_missing_typage_scope_fails():
    fake = FakeLight()
    fake.scopes = ["lecture"]
    assert not client(fake).check()["ok"]


def test_check_wrong_key_and_unreachable():
    bad = AlambicLight("http://light.test", api_key="mauvaise", transport=httpx.MockTransport(FakeLight().handler))
    result = bad.check()
    assert not result["ok"] and "401" in result["etapes"][-1][2]
    fake = FakeLight()
    fake.down = True
    result = client(fake).check()
    assert not result["ok"] and result["etapes"][0][0] == "Service joignable"


def test_api_startup_logs_light_check(tmp_path, share, monkeypatch, caplog):
    import ged.alambic_light as al

    fake = FakeLight()
    fake.scopes = ["lecture"]  # sans « typage »
    real = al.AlambicLight

    def patched(*a, **kw):
        return real(*a, transport=httpx.MockTransport(fake.handler), **kw)

    monkeypatch.setattr(al, "AlambicLight", patched)
    from ged.store import MemoryStore

    monkeypatch.setattr("ged.store.PgStore", lambda dsn, dim: MemoryStore())  # pas de base pour ce test
    cfg = tmp_path / "sources.yaml"
    cfg.write_text(f"""
alambic_light:
  url: http://light.test
  api_key_env: K
sources:
  - name: p
    type: filesystem
    root: {share.as_posix()}
""", encoding="utf-8")
    monkeypatch.setenv("K", "cle-test")
    monkeypatch.setenv("SOURCES_FILE", str(cfg))
    from ged.api import app_from_env

    with caplog.at_level("INFO"):
        app_from_env()
    text = caplog.text
    assert "service joignable : version 1.4.0" in text
    assert "droit « typage » : absent" in text
    assert "Alambic Light inutilisable en l'état" in text


def test_validated_but_erased_data_keeps_known_values(scans, store, embedder):
    """Export acquitté côté Alambic Light : statut VALIDE mais données effacées."""
    fake = FakeLight()
    analyzer = client(fake, send_all=False)
    connector = FileSystemConnector("partage", scans, ACL)
    sync_source(connector, store, embedder, analyzer=analyzer)
    doc = by_path(store, "scan-durand")
    fake.docs[doc.meta.light_uuid].update(statut="VALIDE", donnees=None, donneesAVerifier=[])

    report = sync_source(connector, store, embedder, analyzer=analyzer)
    meta = by_path(store, "scan-durand").meta
    assert report.validated == 0
    assert meta.light_status == "EFFACE" and meta.fields["fournisseur"]["valeur"] == "Durand SA"
    assert meta.to_check == ("montantTTC",)
    assert "(à vérifier)" in store.document_chunks("partage", doc.doc_id)[0].text
    assert store.pending_reviews("partage") == []  # plus relu


# --- Lien console → écran de validation d'Alambic Light ---------------------------------

def _console(store, embedder, analyzer, connector):
    from fastapi.testclient import TestClient

    from ged.api import create_app

    app = create_app(store, embedder, None, {connector.name: connector}, admin_password="pw",
                     analyzer=analyzer, session_secret="cle-de-test")
    return TestClient(app)


def test_console_links_to_light_validation_and_rereads(scans, store, embedder):
    fake = FakeLight()
    analyzer = client(fake, send_all=False, validation_link="https://light.corp/validation?document={uuid}")
    connector = FileSystemConnector("partage", scans, ACL)
    sync_source(connector, store, embedder, analyzer=analyzer)
    doc = by_path(store, "scan-durand")
    url = f"/admin/documents/partage/{doc.doc_id}"
    http = _console(store, embedder, analyzer, connector)

    # Console par mot de passe : bouton affiché, Alambic Light vérifiera les droits
    html = http.get(url, auth=("admin", "pw")).text
    assert f'href="https://light.corp/validation?document={doc.meta.light_uuid}"' in html
    assert "Relire le résultat" in html and "vous demandera de vous connecter" in html

    # Pas encore validé : rien de nouveau
    form = {"source": "partage", "doc_id": doc.doc_id}
    origin = {"Origin": "http://testserver"}
    html = http.post("/admin/documents/relire", data=form, auth=("admin", "pw"), headers=origin).text
    assert "pas encore validé" in html

    # Validé dans Alambic Light : repris tout de suite, plus de bouton
    fake.validate(doc.meta.light_uuid, {"fournisseur": "Durand SA", "montantTTC": "1250.00"})
    html = http.post("/admin/documents/relire", data=form, auth=("admin", "pw"), headers=origin).text
    assert "Données validées reprises" in html and "1250.00" in html and "Valider dans Alambic Light" not in html
    assert by_path(store, "scan-durand").meta.validated

    fake.down = True
    html = http.post("/admin/documents/relire", data=form, auth=("admin", "pw"), headers=origin).text
    assert "Alambic Light ne répond pas" in html


def test_validation_link_default_and_disabled():
    a = AlambicLight("http://alambic-light:8000/", api_key="k")
    assert a.validation_url("u 1") == "http://alambic-light:8000/validation?document=u%201"
    assert AlambicLight("http://x", api_key="k", validation_link="").validation_url("u") is None


# --- Source « texte seul » : OCR des scans, ni type ni données ---------------------------

def test_text_only_source_sends_only_scans_with_ocr_only(scans, store, embedder):
    fake = FakeLight()
    analyzer = client(fake)  # envoyer: tous
    connector = FileSystemConnector("partage", scans, ACL)
    report = sync_source(connector, store, embedder, analyzer=analyzer, analyse=False)
    sent = {r["filename"]: r["ocr_only"] for r in fake.received}
    assert sent == {"scan-durand.pdf": "true", "illisible.pdf": "true"}  # les PDF avec texte ne partent pas
    doc = by_path(store, "scan-durand")
    assert doc.status == "indexed" and doc.meta is None
    texts = [c.text for c in store.document_chunks("partage", doc.doc_id)]
    assert any("FACTURE DURAND" in t for t in texts) and not any("Fiche" in t for t in texts)
    assert by_path(store, "illisible").status == "error" and "OCR" in by_path(store, "illisible").error
    assert report.validated == 0


def test_switching_analysis_off_then_on(scans, store, embedder):
    fake = FakeLight(text_route=False)  # le scan n'est interrogeable que par sa fiche
    analyzer = client(fake, send_all=False)
    connector = FileSystemConnector("partage", scans, ACL)
    sync_source(connector, store, embedder, analyzer=analyzer)
    assert by_path(store, "scan-durand").meta.doc_type == "FACTURE"

    connector.analyse_light = False  # réglage de la source (console)
    sync_source(connector, store, embedder, analyzer=analyzer)
    doc = by_path(store, "scan-durand")
    assert doc.meta is None and store.type_counts() == []
    assert doc.status == "ocr_required"  # repris en OCR seul ; sans texte renvoyé, reste à OCRiser
    assert fake.received[-1]["ocr_only"] == "true"

    connector.analyse_light = True
    before = len(fake.received)
    sync_source(connector, store, embedder, analyzer=analyzer)
    assert len(fake.received) > before and fake.received[-1]["ocr_only"] is None
    assert by_path(store, "scan-durand").meta.doc_type == "FACTURE"
    n = len(fake.received)
    sync_source(connector, store, embedder, analyzer=analyzer)
    assert len(fake.received) == n  # rien à refaire ensuite


def test_text_only_setting_in_console(share, store, embedder):
    from fastapi.testclient import TestClient

    from ged.api import create_app

    analyzer = client(FakeLight())
    http = TestClient(create_app(store, embedder, None, {}, admin_password="pw", analyzer=analyzer),
                      follow_redirects=False)
    assert 'name="analyse_light"' in http.get("/admin/sources/nouvelle", auth=("admin", "pw")).text
    data = {"name": "compta", "type": "filesystem", "root": str(share / "Compta"), "acl": "(racine) = compta",
            "analyse_light_field": "1"}
    assert http.post("/admin/sources/nouvelle", data=data, auth=("admin", "pw")).status_code == 303
    assert store.list_source_configs()[0]["analyse_light"] is False
    app_connectors = http.app.state.catalog.connectors
    assert app_connectors["compta"].analyse_light is False
    assert "Texte seul (OCR des scans)" in http.get("/admin/sources", auth=("admin", "pw")).text
    assert "analyse_light: false" in http.get("/admin/sources/export", auth=("admin", "pw")).text
    http.post("/admin/sources/compta/modifier", data={**data, "analyse_light": "true"}, auth=("admin", "pw"))
    assert "analyse_light" not in store.list_source_configs()[0] and app_connectors["compta"].analyse_light is True


def test_text_only_source_can_ask_ai_ocr(scans, store, embedder):
    fake = FakeLight()
    connector = FileSystemConnector("partage", scans, ACL)
    sync_source(connector, store, embedder, analyzer=client(fake), analyse=False)
    assert {r["ocr"] for r in fake.received} == {None}  # moteur par défaut d'Alambic Light

    fake2 = FakeLight()
    connector.analyse_light, connector.ocr_ia = False, True  # réglages de la source (console)
    sync_source(connector, MemoryStore(), embedder, analyzer=client(fake2))
    assert fake2.received and all(r["ocr_only"] == "true" and r["ocr"] == "ia" for r in fake2.received)


def test_ai_ocr_setting_in_console(share, store, embedder):
    from fastapi.testclient import TestClient

    from ged.api import create_app

    http = TestClient(create_app(store, embedder, None, {}, admin_password="pw", analyzer=client(FakeLight())),
                      follow_redirects=False)
    auth = ("admin", "pw")
    assert 'name="ocr_ia"' in http.get("/admin/sources/nouvelle", auth=auth).text
    data = {"name": "scans", "type": "filesystem", "root": str(share / "Compta"), "acl": "(racine) = compta",
            "analyse_light_field": "1", "ocr_ia": "true"}
    assert http.post("/admin/sources/nouvelle", data=data, auth=auth).status_code == 303
    cfg = store.list_source_configs()[0]
    assert cfg["analyse_light"] is False and cfg["ocr_ia"] is True
    assert http.app.state.catalog.connectors["scans"].ocr_ia is True
    assert "Texte seul (OCR des scans par l'IA)" in http.get("/admin/sources", auth=auth).text
    assert "ocr_ia: true" in http.get("/admin/sources/export", auth=auth).text
    http.post("/admin/sources/scans/modifier", data={**data, "ocr_ia": ""}, auth=auth)
    assert "ocr_ia" not in store.list_source_configs()[0]
