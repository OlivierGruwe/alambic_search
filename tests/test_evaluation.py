"""Jeu de questions de référence : mesure, fuites de droits, comparaison, génération de candidates."""
from __future__ import annotations

import json

import pytest
import yaml

from ged import evaluation as ev
from ged.sync import sync_source
from tests.conftest import FakeLLM

JEU = {
    "defaut": {"groupes": ["compta"], "k": 5},
    "questions": [
        {"id": "penalite", "question": "Quelle est la pénalité de retard du contrat de maintenance ?",
         "documents": ["contrat-fournisseur.docx"], "pages": [1], "mots": ["2 %"]},
        {"id": "facture", "question": "Montant de la facture ACME", "documents": ["partage:Compta/Factures/facture-acme.pdf"],
         "pages": [1]},
        {"id": "salaires", "question": "grille des salaires", "documents": ["RH/salaires.txt"],
         "interdits": ["RH/salaires.txt"]},  # compta n'y a pas droit : jamais retrouvé
        {"id": "or", "question": "Quel est le cours de l'or ?", "sans_reponse": True},
    ],
}


@pytest.fixture
def jeu(tmp_path):
    path = tmp_path / "jeu.yaml"
    path.write_text(yaml.safe_dump(JEU, allow_unicode=True), encoding="utf-8")
    return path


def test_load_and_validate(jeu, tmp_path):
    cases = ev.load_cases(jeu)
    assert [c.id for c in cases] == ["penalite", "facture", "salaires", "or"]
    assert cases[0].groupes == ["compta"] and cases[0].k == 5
    bad = tmp_path / "bad.yaml"
    bad.write_text("questions:\n  - question: sans attendu\n    groupes: [a]\n", encoding="utf-8")
    with pytest.raises(ev.EvalError, match="documents"):
        ev.load_cases(bad)
    bad.write_text("questions:\n  - question: sans groupes\n    documents: [x]\n", encoding="utf-8")
    with pytest.raises(ev.EvalError, match="groupes"):
        ev.load_cases(bad)


def test_path_matching():
    assert ev.matches("contrat.docx", "s", "Compta/contrat.docx")
    assert ev.matches("Compta/contrat.docx", "s", "Compta/contrat.docx")
    assert ev.matches("s:Compta/contrat.docx", "s", "Compta/contrat.docx")
    assert not ev.matches("autre:Compta/contrat.docx", "s", "Compta/contrat.docx")
    assert not ev.matches("trat.docx", "s", "Compta/contrat.docx")


def test_measure_search_and_answers(connector, store, embedder, jeu):
    sync_source(connector, store, embedder)
    llm = FakeLLM("La pénalité est de 2 % par semaine [1].")
    result = ev.evaluate(ev.load_cases(jeu), store, embedder, llm, with_answers=True)
    q = {r["id"]: r for r in result["questions"]}
    assert q["penalite"]["trouve"] and q["penalite"]["rang"] >= 1 and q["penalite"]["page_ok"] is not None
    assert q["facture"]["trouve"]
    assert q["salaires"]["trouve"] is False and q["salaires"]["fuites"] == []  # droits respectés
    assert q["or"]["trouve"] is None
    assert q["penalite"]["mots_ok"] is True
    s = result["resume"]
    assert s["questions"] == 4 and s["fuites"] == 0 and 0 < s["trouve"] < 1 and s["mrr"] > 0
    report = ev.format_report(result)
    assert "Non retrouvés" in report and "salaires" in report and "Fuites" in report


def test_refusal_is_measured(connector, store, embedder, jeu):
    sync_source(connector, store, embedder)
    cases = [c for c in ev.load_cases(jeu) if c.id == "or"]
    ok = ev.evaluate(cases, store, embedder, FakeLLM("Les extraits ne permettent pas de répondre."), with_answers=True)
    ko = ev.evaluate(cases, store, embedder, FakeLLM("L'or vaut 2 000 € [1]."), with_answers=True)
    assert ok["resume"]["refus"] == 1 and ko["resume"]["refus"] == 0


def test_leak_is_reported(connector, store, embedder, tmp_path):
    """Un document lisible par le groupe mais déclaré interdit : signalé comme fuite."""
    sync_source(connector, store, embedder)
    path = tmp_path / "j.yaml"
    path.write_text(yaml.safe_dump({"questions": [
        {"id": "x", "question": "horaires de la mairie", "groupes": ["tous"], "documents": ["horaires.txt"],
         "interdits": ["horaires.txt"]}]}), encoding="utf-8")
    result = ev.evaluate(ev.load_cases(path), store, embedder)
    assert result["resume"]["fuites"] == 1 and "FUITES" in ev.format_report(result)


def test_compare_finds_regressions_and_progress():
    before = {"resume": {"trouve": 0.5, "mrr": 0.5}, "questions": [
        {"id": "a", "trouve": True, "rang": 1}, {"id": "b", "trouve": False, "rang": None}]}
    after = {"resume": {"trouve": 0.5, "mrr": 0.75}, "questions": [
        {"id": "a", "trouve": True, "rang": 3}, {"id": "b", "trouve": True, "rang": 1}]}
    cmp = ev.compare(after, before)
    assert cmp["regressions"] == [{"id": "a", "avant": 1, "apres": 3}]
    assert cmp["progres"] == [{"id": "b", "avant": None, "apres": 1}]
    assert cmp["ecarts"]["mrr"] == 0.25
    assert "Régressions" in ev.format_report({**after, "resume": {**after["resume"], "questions": 2, "erreurs": 0,
                                                                    "ms_moyen": 1, "fuites": 0}}, cmp)


def test_generate_candidates(connector, store, embedder, share):
    (share / "Compta" / "long.txt").write_text("Procédure de clôture comptable. " * 20, encoding="utf-8")
    sync_source(connector, store, embedder)
    llm = FakeLLM(json.dumps({"question": "Comment se déroule la clôture comptable ?", "reponse": "Procédure"}))
    items = ev.generate_candidates(store, llm, n=3)
    assert items and items[0]["documents"] == ["partage:Compta/long.txt"] and items[0]["groupes"]
    text = ev.candidates_yaml(items)
    assert "à relire" in text
    # le brouillon est un jeu valide
    assert ev.load_cases.__name__ and yaml.safe_load(text)["questions"][0]["question"]


def test_cli_eval(connector, store, embedder, jeu, tmp_path, monkeypatch):
    """La commande renvoie 0 sans fuite ni régression, et écrit la mesure."""
    from ged import cli

    sync_source(connector, store, embedder)
    monkeypatch.setattr("ged.store.PgStore", lambda *a, **k: store)
    monkeypatch.setattr(store, "init", lambda: None, raising=False)
    monkeypatch.setattr("ged.embeddings.OpenAICompatEmbedder", lambda *a, **k: embedder)
    monkeypatch.setenv("LLM_MODEL", "")
    monkeypatch.setenv("SOURCES_FILE", str(tmp_path / "absent.yaml"))
    out = tmp_path / "mesure.json"
    assert cli.main(["eval", str(jeu), "--sortie", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["resume"]["questions"] == 4
    assert cli.main(["eval", str(jeu), "--reference", str(out)]) == 0
