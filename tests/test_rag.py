"""RAG : reclassement, questions de suivi, contexte des passages vectorisés."""
from __future__ import annotations

import json

import httpx
from fastapi.testclient import TestClient

from ged.answer import ask, standalone_question
from ged.api import create_app
from ged.chunking import embedding_inputs, is_heading, sections
from ged.rerank import HttpReranker, load_reranker
from ged.sync import reembed, sync_source
from tests.conftest import FakeLLM

# --- Reclassement ---------------------------------------------------------------------------


def fake_service(api: str, prefer: str, calls: list | None = None, fail: bool = False):
    """Service de reclassement : score élevé pour les passages qui contiennent `prefer`."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if calls is not None:
            calls.append(body)
        if fail:
            return httpx.Response(503, json={"error": "modèle en chargement"})
        texts = body["texts"] if api == "tei" else body["documents"]
        scored = [{"index": i, ("score" if api == "tei" else "relevance_score"): 0.9 if prefer in t else 0.1}
                  for i, t in enumerate(texts)]
        return httpx.Response(200, json=scored if api == "tei" else {"results": scored})

    return httpx.MockTransport(handler)


def test_rerank_both_contracts_reorder(connector, store, embedder):
    sync_source(connector, store, embedder)
    for api in ("tei", "jina"):
        calls: list = []
        rr = HttpReranker("http://rerank", api=api, transport=fake_service(api, "Pénalité", calls))
        answer = ask("contrat société", frozenset({"compta"}), store, embedder, None, generate=False, reranker=rr, k=3)
        assert "Pénalité" in answer.hits[0].text, api  # le reclassement l'emporte sur l'ordre hybride
        assert answer.hits[0].score == 0.9
        assert calls and calls[0]["query"] == "contrat société"
        assert ("texts" in calls[0]) == (api == "tei")


def test_rerank_failure_keeps_hybrid_order(connector, store, embedder):
    sync_source(connector, store, embedder)
    plain = ask("pénalité de retard", frozenset({"compta"}), store, embedder, None, generate=False)
    calls: list = []
    rr = HttpReranker("http://rerank", transport=fake_service("tei", "x", calls, fail=True))
    first = ask("pénalité de retard", frozenset({"compta"}), store, embedder, None, generate=False, reranker=rr)
    assert [h.text for h in first.hits] == [h.text for h in plain.hits]
    ask("pénalité de retard", frozenset({"compta"}), store, embedder, None, generate=False, reranker=rr)
    assert len(calls) == 1  # service en panne : pas de nouvel essai avant 60 s


def test_rerank_never_sees_forbidden_passages(connector, store, embedder):
    sync_source(connector, store, embedder)
    calls: list = []
    rr = HttpReranker("http://rerank", transport=fake_service("tei", "x", calls))
    ask("grille des salaires", frozenset({"tous"}), store, embedder, None, generate=False, reranker=rr)
    assert all("confidentielle" not in t for t in calls[0]["texts"])  # droits appliqués AVANT


def test_load_reranker_from_env():
    assert load_reranker({}) is None
    rr = load_reranker({"RERANK_URL": "http://tei:80/", "RERANK_API": "jina", "RERANK_CANDIDATES": "25"})
    assert rr.url == "http://tei:80" and rr.api == "jina" and rr.candidates == 25


def test_api_and_sandbox_use_reranker(connector, store, embedder):
    sync_source(connector, store, embedder)
    rr = HttpReranker("http://rerank", transport=fake_service("tei", "Pénalité"))
    c = TestClient(create_app(store, embedder, None, {connector.name: connector}, admin_password="pw", reranker=rr))
    h = {"X-User-Groups": "compta"}
    assert "Pénalité" in c.get("/search", params={"q": "contrat société", "k": 2}, headers=h).json()[0]["excerpt"]
    page = c.get("/admin/sandbox", auth=("admin", "pw")).text
    assert 'name="reclasser"' in page and "bge-reranker" in page


# --- Questions de suivi ---------------------------------------------------------------------

def test_follow_up_question_is_made_standalone(connector, store, embedder):
    sync_source(connector, store, embedder)
    history = [{"question": "Quelle est la pénalité de retard du contrat Durand ?", "reponse": "2 % par semaine [1]."}]

    class Llm(FakeLLM):
        def complete(self, system, user):
            if "réécris" in system:
                self.seen = user
                return "« Quelle est la durée du contrat de maintenance Durand ? »"
            return super().complete(system, user)

    llm = Llm("Voir [1].")
    answer = ask("et sa durée ?", frozenset({"compta"}), store, embedder, llm, history=history)
    assert answer.reformulated == "Quelle est la durée du contrat de maintenance Durand ?"
    assert "pénalité de retard du contrat Durand" in llm.seen and "et sa durée ?" in llm.seen
    assert "Question : Quelle est la durée du contrat de maintenance Durand ?" in llm.last_user


def test_standalone_question_fallbacks():
    assert standalone_question("et lui ?", [], FakeLLM("x")) == "et lui ?"  # pas d'historique
    assert standalone_question("et lui ?", [{"question": "a", "reponse": "b"}], None) == "et lui ?"  # pas de LLM

    class Down:
        def complete(self, s, u):
            raise ConnectionError

    assert standalone_question("et lui ?", [{"question": "a"}], Down()) == "et lui ?"


def test_api_accepts_history(connector, store, embedder):
    sync_source(connector, store, embedder)
    c = TestClient(create_app(store, embedder, FakeLLM("Question autonome sur la pénalité"), {connector.name: connector}))
    r = c.post("/ask", headers={"X-User-Groups": "compta"}, json={
        "question": "et pour Durand ?", "formuler": False,
        "historique": [{"question": "pénalité de retard ?", "reponse": "2 %"}]}).json()
    assert r["question_comprise"] == "Question autonome sur la pénalité"
    r = c.post("/ask", headers={"X-User-Groups": "compta"}, json={"question": "pénalité de retard", "formuler": False}).json()
    assert r["question_comprise"] is None


def test_search_page_carries_history(connector, store, embedder):
    from ged.accounts import Accounts
    from ged.models import Role

    sync_source(connector, store, embedder)
    c = TestClient(create_app(store, embedder, FakeLLM("Réponse [1]."), {connector.name: connector}, session_secret="k"))
    Accounts(store).create("alice", "Alice", Role.USER, "alice-mdp-123", groups=["compta"])
    c.post("/connexion", data={"identifiant": "alice", "mot_de_passe": "alice-mdp-123"})
    html = c.post("/rechercher", data={"mode": "question", "question": "pénalité de retard", "formuler": "true"}).text
    assert 'name="historique" form="search-form"' in html and "Nouvelle conversation" in html
    value = html.split('name="historique" form="search-form" value="', 1)[1].split('"', 1)[0]
    import html as h

    assert json.loads(h.unescape(value))[0]["question"] == "pénalité de retard"
    assert c.post("/rechercher", data={"mode": "question", "question": "et le délai ?", "formuler": "true",
                                       "historique": "pas du json"}).status_code == 200  # historique invalide : ignoré


# --- Contexte des passages ------------------------------------------------------------------

def test_headings_and_sections():
    assert is_heading("ARTICLE 3 - PÉNALITÉS") and is_heading("2.3 Délais de paiement") and is_heading("Annexe B")
    assert not is_heading("Le délai est de 30 jours.") and not is_heading("Montant total : 4 850 euros HT")
    assert not is_heading("1. Le prestataire intervient sous 48 heures ouvrées après signalement.")
    texts = ["ARTICLE 1 - OBJET\nMaintenance.", "Suite de l'objet.", "Texte\nARTICLE 2 - PRIX\n1 000 €", "Fin."]
    assert sections(texts) == ["ARTICLE 1 - OBJET", "ARTICLE 1 - OBJET", "ARTICLE 1 - OBJET", "ARTICLE 2 - PRIX"]
    inputs = embedding_inputs("Compta/contrat_durand.pdf", "Contrat", texts[:1])
    assert inputs[0].startswith("Document : Compta / contrat durand (Contrat) — ARTICLE 1 - OBJET\n")


class Recorder:
    """Embedder qui retient les textes vectorisés."""

    def __init__(self, inner):
        self.inner, self.seen = inner, []
        self.dim = inner.dim

    def embed(self, texts):
        self.seen += texts
        return self.inner.embed(texts)


def test_sync_embeds_with_context_but_stores_plain_text(connector, store, embedder):
    rec = Recorder(embedder)
    sync_source(connector, store, rec)
    assert any(t.startswith("Document : Compta / Factures / facture-acme\n") for t in rec.seen)
    doc = next(r for r in store.list_documents(limit=50)[0] if r.path.endswith("facture-acme.pdf"))
    assert not store.document_chunks("partage", doc.doc_id)[0].text.startswith("Document :")


def test_reembed_updates_vectors_without_reading_files(connector, store, embedder, share):
    sync_source(connector, store, embedder)
    for f in share.rglob("*"):
        if f.is_file():
            f.unlink()  # les fichiers ne sont plus là : le recalcul ne les lit pas
    rec = Recorder(embedder)
    n = reembed(store, rec, source="partage")
    assert n >= 4 and all(t.startswith("Document : ") for t in rec.seen)
    assert ask("pénalité de retard", frozenset({"compta"}), store, embedder, None, generate=False).hits


def test_grouped_citations_are_split():
    from ged.answer import split_cites

    assert split_cites("Olivier [3, 4, 5] et Joseph [1].") == "Olivier [3], [4], [5] et Joseph [1]."
    assert split_cites("Voir [2-4] ou [6 et 7] ; [8;9]") == "Voir [2], [3], [4] ou [6], [7] ; [8], [9]"
    assert split_cites("Tableau [12] et [a, b]") == "Tableau [12] et [a, b]"
