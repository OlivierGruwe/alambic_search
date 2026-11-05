"""Recherche par critères : « toutes les factures de M. GRUWE », et aucune autre."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ged.api import create_app
from ged.connectors import FileSystemConnector
from ged.criteria import search_by_criteria
from ged.sync import sync_source

from .conftest import ACL, FakeLLM, make_pdf
from .test_alambic_light import FakeLight, champ, client

INVOICES = {
    # fichier : (client extrait, à vérifier, texte du PDF)
    "facture-001.pdf": ("M. Olivier GRUWE", False, "Facture 001 pour M. Olivier GRUWE"),
    "facture-002.pdf": ("Gruwé Olivier", True, "Facture 002 Gruwé"),
    "facture-003.pdf": ("Mme DURAND", False, "Facture 003 Mme DURAND"),
    "facture-004.pdf": ("M. GRUWEL", False, "Facture 004 M. GRUWEL"),  # autre personne
    "facture-005.pdf": ("SARL MARTIN", False, "Facture 005 SARL MARTIN, livraison chez M. Gruwe"),
}


class InvoiceLight(FakeLight):
    def decide(self, filename: str, form: dict) -> dict:
        if filename in INVOICES:
            name, check, _ = INVOICES[filename]
            return {"typeDocument": "FACTURE", "statut": "RECONNU", "confiance": 0.95,
                    "donnees": {"client": champ(name, a_verifier=check), "montantTTC": champ("100.00")},
                    "donneesAVerifier": ["client"] if check else []}
        if filename == "contrat-gruwe.pdf":
            return {"typeDocument": "CONTRAT", "statut": "RECONNU", "confiance": 0.9,
                    "donnees": {"cocontractant": champ("Olivier GRUWE")}}
        return super().decide(filename, form)


@pytest.fixture
def indexed(share: Path, store, embedder):
    for name, (_, _, text) in INVOICES.items():
        make_pdf(share / "Compta" / "Factures" / name, [text])
    make_pdf(share / "Compta" / "contrat-gruwe.pdf", ["Contrat avec Olivier GRUWE"])
    make_pdf(share / "RH" / "facture-rh.pdf", ["Facture 006 pour M. Olivier GRUWE"])  # pas visible de compta
    (share / "Compta" / "note-gruwe.txt").write_text("Rappeler M. Gruwe pour sa facture", encoding="utf-8")
    connector = FileSystemConnector("partage", share, ACL)
    sync_source(connector, store, embedder, analyzer=client(InvoiceLight(pending_polls=0)))
    return connector


def paths(matches) -> list[str]:
    return [m.path.rsplit("/", 1)[-1] for m in matches]


def test_all_invoices_of_gruwe_and_no_other(indexed, store):
    r = search_by_criteria(store, frozenset({"compta"}), "GRUWE", doc_type="FACTURE")
    assert paths(r.documents) == ["facture-001.pdf", "facture-002.pdf"]  # accents, casse, mot entier
    assert r.doubtful == 1 and r.documents[1].fields == (("client", "Gruwé Olivier", True),)
    # mentions dans le texte, à part : facture d'un autre client citant M. Gruwe, note non typée
    assert paths(r.mentions) == ["facture-005.pdf", "note-gruwe.txt"]
    assert "contrat-gruwe.pdf" not in paths(r.documents) + paths(r.mentions)  # autre type
    assert r.complete


def test_rights_apply(indexed, store):
    assert "facture-rh.pdf" not in paths(search_by_criteria(store, frozenset({"compta"}), "GRUWE").documents)
    rh = search_by_criteria(store, frozenset({"rh"}), "GRUWE", doc_type="FACTURE")
    assert paths(rh.documents) == [] and paths(rh.mentions) == ["facture-rh.pdf"]  # non typée (txt de la fake)
    assert search_by_criteria(store, frozenset(), "GRUWE").documents == []


def test_without_type_and_with_field(indexed, store):
    every = search_by_criteria(store, frozenset({"compta"}), "gruwe")
    assert set(paths(every.documents)) == {"facture-001.pdf", "facture-002.pdf", "contrat-gruwe.pdf"}
    only_client = search_by_criteria(store, frozenset({"compta"}), "gruwe", field="client")
    assert set(paths(only_client.documents)) == {"facture-001.pdf", "facture-002.pdf"}
    assert "client" in store.field_names("FACTURE") and "cocontractant" not in store.field_names("FACTURE")


def test_truncation_is_reported(indexed, store):
    r = search_by_criteria(store, frozenset({"compta"}), "GRUWE", doc_type="FACTURE", limit=1)
    assert len(r.documents) == 1 and not r.complete


def test_api_recherche(indexed, store, embedder):
    http = TestClient(create_app(store, embedder, FakeLLM(), {"partage": indexed}))
    body = http.get("/recherche", params={"type": "FACTURE", "valeur": "Gruwe"},
                    headers={"X-User-Groups": "compta"}).json()
    assert [d["path"].rsplit("/", 1)[-1] for d in body["documents"]] == ["facture-001.pdf", "facture-002.pdf"]
    assert body["documents"][1]["champs"][0] == {"champ": "client", "valeur": "Gruwé Olivier", "aVerifier": True}
    assert body["complet"] and all("#page=" in m["url"] for m in body["mentions"])
    assert http.get("/recherche", params={"valeur": "x"}).status_code == 422


def test_sandbox_criteria_mode(indexed, store, embedder):
    http = TestClient(create_app(store, embedder, FakeLLM(), {"partage": indexed}, admin_password="pw"))
    page = http.get("/admin/sandbox", auth=("admin", "pw")).text
    assert "Liste par critères" in page and '<option value="client">' in page
    html = http.post("/admin/sandbox", auth=("admin", "pw"), data={
        "mode": "criteres", "valeur": "GRUWE", "doc_type": "FACTURE", "groups": "compta"}).text
    assert "2 documents de type Facture" in html and "facture-003.pdf" not in html
    assert "à vérifier" in html and "À regarder" in html
    assert "au moins 2" in http.post("/admin/sandbox", auth=("admin", "pw"),
                                     data={"mode": "criteres", "valeur": "", "groups": "compta"}).text


# --- Question de liste traduite en critères par le LLM -------------------------------------

class RouterLLM:
    """Répond à la traduction en critères par `route`, et aux autres prompts par `reply`."""

    def __init__(self, route: str, reply: str = "Réponse [1]."):
        self.route = route
        self.reply = reply
        self.prompts: list[str] = []

    def complete(self, system: str, user: str) -> str:
        self.prompts.append(user)
        return self.route if "Types et champs" in user else self.reply


def test_list_question_uses_criteria_not_ranking(indexed, store, embedder):
    from ged.answer import ask

    llm = RouterLLM('Voici : {"liste": true, "type": "FACTURE", "champ": null, "valeur": "GRUWE"}')
    answer = ask("Donne-moi toutes les factures de M. GRUWE", frozenset({"compta"}), store, embedder, llm)
    assert answer.criteria is not None and not answer.generated
    assert [c.path.rsplit("/", 1)[-1] for c in answer.citations] == ["facture-001.pdf", "facture-002.pdf"]
    assert answer.text.startswith("2 factures dont un champ contient « GRUWE »") and "Dont 1 à vérifier" in answer.text
    assert "FACTURE (Facture) : champs client, montantTTC" in llm.prompts[0]
    assert len(llm.prompts) == 1  # pas de génération ensuite


def test_list_question_without_writing_still_uses_criteria(indexed, store, embedder):
    """Sans rédaction, une question de liste rend la même liste exacte (pas des passages)."""
    from ged.answer import ask

    llm = RouterLLM('{"liste": true, "type": "FACTURE", "champ": null, "valeur": "GRUWE"}')
    answer = ask("Donne-moi toutes les factures de M. GRUWE", frozenset({"compta"}), store, embedder, llm,
                 generate=False)
    assert answer.criteria is not None
    assert [c.path.rsplit("/", 1)[-1] for c in answer.citations] == ["facture-001.pdf", "facture-002.pdf"]


@pytest.mark.parametrize("route", [
    '{"liste": false}',                                                   # pas une liste
    '{"liste": true, "type": "FACTURE", "valeur": "DUPONT"}',             # valeur absente de la question
    "je ne sais pas",                                                      # réponse illisible
])
def test_router_output_is_checked(indexed, store, embedder, route):
    from ged.answer import ask

    llm = RouterLLM(route)
    answer = ask("Liste les factures de M. GRUWE", frozenset({"compta"}), store, embedder, llm)
    assert answer.criteria is None and answer.generated  # recherche ordinaire


def test_invented_type_or_field_is_dropped(indexed, store):
    from ged.answer import translate

    llm = RouterLLM('{"liste": true, "type": "PASSEPORT", "champ": "nom", "valeur": "Gruwe"}')
    assert translate("toutes les pièces de Gruwe", store, llm) == {"type": None, "champ": None, "valeur": "Gruwe"}


def test_content_questions_skip_the_router(indexed, store, embedder):
    from ged.answer import ask

    llm = RouterLLM('{"liste": true, "valeur": "GRUWE"}')
    ask("Quel est le montant de la facture 001 ?", frozenset({"compta"}), store, embedder, llm)
    assert all("Types et champs" not in p for p in llm.prompts)  # pas d'appel de traduction


def test_list_answer_in_api_and_sandbox(indexed, store, embedder):
    llm = RouterLLM('{"liste": true, "type": "FACTURE", "champ": "client", "valeur": "Gruwe"}')
    http = TestClient(create_app(store, embedder, llm, {"partage": indexed}, admin_password="pw"))
    body = http.post("/ask", json={"question": "Combien de factures pour Gruwe ?"},
                     headers={"X-User-Groups": "compta"}).json()
    assert body["liste"]["filtre"] == {"type": "FACTURE", "champ": "client", "valeur": "Gruwe"}
    assert len(body["liste"]["documents"]) == 2 and len(body["citations"]) == 2
    html = http.post("/admin/sandbox", auth=("admin", "pw"), data={
        "question": "Combien de factures pour Gruwe ?", "groups": "compta", "formuler": "true"}).text
    assert "Question comprise comme une liste par critères" in html and "facture-002.pdf" in html
    assert "facture-003.pdf" not in html


def test_llm_time_is_measured(indexed, store, embedder):
    from ged.answer import ask

    llm = RouterLLM('{"liste": false}')
    answer = ask("Quel est le montant de la facture 001 ?", frozenset({"compta"}), store, embedder, llm)
    assert answer.generated and answer.llm_ms >= 0
    assert ask("Quel montant ?", frozenset({"compta"}), store, embedder, None, generate=False).llm_ms == 0
