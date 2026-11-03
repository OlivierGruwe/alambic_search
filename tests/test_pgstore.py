"""Tests d'intégration PostgreSQL + pgvector.

Lancés seulement si GED_TEST_DSN est défini, par ex. :
    GED_TEST_DSN=postgresql://postgres@localhost:5433/ged pytest tests/test_pgstore.py
"""
import os
import shutil

import pytest

from ged.answer import ask
from ged.store import PgStore
from ged.sync import sync_source
from tests.conftest import FakeLLM

DSN = os.environ.get("GED_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="GED_TEST_DSN non défini")


@pytest.fixture
def pg(embedder):
    import psycopg

    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute("DROP TABLE IF EXISTS chunks; DROP TABLE IF EXISTS documents;")
    s = PgStore(DSN, embedder.dim)
    s.init()
    s.init()  # idempotent
    yield s
    s.close()


def test_sync_search_and_acl(connector, pg, embedder):
    r = sync_source(connector, pg, embedder)
    assert (r.added, r.errors) == (4, [])
    assert sync_source(connector, pg, embedder).unchanged == 4

    q = "pénalités de retard du contrat"  # pluriel : le stemming français doit retrouver "pénalité"
    hits = pg.search(q, embedder.embed([q])[0], frozenset({"compta"}))
    assert hits[0].path == "Compta/contrat-fournisseur.docx"

    q = "grille des salaires"
    assert all(not h.path.startswith("RH/") for h in pg.search(q, embedder.embed([q])[0], frozenset({"compta"})))
    assert pg.search(q, embedder.embed([q])[0], frozenset({"rh"}))[0].path == "RH/salaires.txt"
    assert pg.search(q, embedder.embed([q])[0], frozenset()) == []


def test_move_and_delete(connector, pg, embedder, share):
    sync_source(connector, pg, embedder)
    shutil.move(share / "RH/salaires.txt", share / "Public/salaires.txt")
    (share / "Public/horaires.txt").unlink()
    r = sync_source(connector, pg, embedder)
    assert (r.moved, r.acl_changed, r.deleted) == (1, 1, 1)
    state = pg.get_state("partage")
    assert sorted(s.path for s in state.values()) == [
        "Compta/Factures/facture-acme.pdf",
        "Compta/contrat-fournisseur.docx",
        "Public/salaires.txt",
    ]


def test_ask_cites_page(connector, pg, embedder):
    sync_source(connector, pg, embedder)
    answer = ask("conditions de paiement à 30 jours", frozenset({"compta"}), pg, embedder, FakeLLM())
    assert (answer.citations[0].path, answer.citations[0].page) == ("Compta/Factures/facture-acme.pdf", 2)


def test_admin_queries(connector, pg, embedder, share):
    import psycopg

    from ged.models import RunStatus, Status

    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute("TRUNCATE sync_runs, question_log")
    (share / "Public/casse.pdf").write_bytes(b"%PDF-1.4 pas un pdf")
    run_id = pg.start_run("partage")
    sync_source(connector, pg, embedder)
    pg.finish_run(run_id, RunStatus.PARTIAL, {"added": 4, "errors": ["casse"]})

    stats = pg.stats()["partage"]
    assert stats[Status.INDEXED] == 4 and stats[Status.ERROR] == 1

    rows, total = pg.list_documents(source="partage", q="FACTURE")  # insensible à la casse
    assert total == 1 and rows[0].chunks == 2 and rows[0].acl.allow == {"compta", "direction"}
    rows, total = pg.list_documents(status=Status.ERROR)
    assert total == 1 and rows[0].error
    assert len(pg.list_documents(limit=2)[0]) == 2

    doc = pg.list_documents(q="facture")[0][0]
    assert pg.document_row(doc.source, doc.doc_id) == doc
    assert [c.page for c in pg.document_chunks(doc.source, doc.doc_id)] == [1, 2]

    runs = pg.list_runs("partage")
    assert runs[0].status == RunStatus.PARTIAL and runs[0].report["added"] == 4 and runs[0].finished_at

    stale = pg.start_run("partage")
    assert pg.interrupt_stale_runs() == 1
    assert pg.list_runs("partage")[0].id == stale and pg.list_runs("partage")[0].status == RunStatus.INTERRUPTED

    pg.log_question(frozenset({"compta"}), "q1", 3, 1, 120)
    pg.log_question(frozenset({"compta"}), "q2", 0, 0, 80)
    pg.log_question(frozenset({"rh"}), "bac à sable", 2, 1, 90, sandbox=True)
    activity = pg.question_activity(14)
    assert len(activity) == 14 and activity[-1][1:] == (2, 1)
    assert pg.purge_questions(0) == 3


def test_explicit_deny_is_applied_in_sql(pg, embedder):
    from ged.models import Acl, Chunk, DocumentRef, Status

    ref = DocumentRef("partage", "x1", "Compta/contrat.txt", 1.0, 10)
    pg.upsert_document(ref, "h", Acl(frozenset({"compta"}), frozenset({"stagiaires"})), Status.INDEXED,
                       [Chunk(0, 1, "contrat pénalité de retard")], embedder.embed(["contrat pénalité de retard"]))
    v = embedder.embed(["pénalité"])[0]
    assert [h.doc_id for h in pg.search("pénalité", v, frozenset({"compta"}))] == ["x1"]
    assert pg.search("pénalité", v, frozenset({"compta", "stagiaires"})) == []
    assert pg.get_document("partage", "x1").acl.deny == {"stagiaires"}


def test_alambic_light_metadata_roundtrip(pg, share, embedder):
    """Type, données, champs à vérifier, fiche et reprise de validation, dans PostgreSQL."""
    from ged.connectors import FileSystemConnector
    from tests.conftest import ACL
    from tests.test_alambic_light import FakeLight, client, make_scan

    make_scan(share / "Compta" / "scan-durand.pdf")
    fake = FakeLight(pending_polls=0)
    analyzer = client(fake, send_all=False)
    connector = FileSystemConnector("partage", share, ACL)
    sync_source(connector, pg, embedder, analyzer=analyzer)

    rows, total = pg.list_documents(doc_type="FACTURE")
    assert total == 1
    doc = rows[0]
    assert doc.meta.label == "Facture" and doc.meta.to_check == ("montantTTC",)
    assert doc.meta.fields["lignes"]["valeur"][0]["designation"] == "Maintenance"  # jsonb relu tel quel
    assert pg.type_counts() == [("FACTURE", "Facture", 1)]
    q = "facture Durand"
    hits = pg.search(q, embedder.embed([q])[0], frozenset({"compta"}), k=5, doc_types=frozenset({"FACTURE"}))
    assert hits and {h.doc_label for h in hits} == {"Facture"}
    assert pg.search(q, embedder.embed([q])[0], frozenset({"compta"}), k=5, doc_types=frozenset({"RIB"})) == []

    fake.validate(doc.meta.light_uuid, {"fournisseur": "Durand SA", "montantTTC": "1250.00"})
    report = sync_source(connector, pg, embedder, analyzer=analyzer)
    assert report.validated == 1
    doc = pg.document_row("partage", doc.doc_id)
    assert doc.meta.validated and doc.meta.to_check == ()
    chunks = pg.document_chunks("partage", doc.doc_id)
    assert chunks[0].chunk_no == -1 and "1250.00" in chunks[0].text
    assert pg.pending_reviews("partage") == []


def test_criteria_search_in_postgres(pg, share, embedder):
    """Même scénario que tests/test_criteria.py, sur PostgreSQL (unaccent, regex mot entier, jsonb)."""
    from ged.connectors import FileSystemConnector
    from ged.criteria import search_by_criteria
    from tests.conftest import ACL, make_pdf
    from tests.test_alambic_light import client
    from tests.test_criteria import INVOICES, InvoiceLight

    for name, (_, _, text) in INVOICES.items():
        make_pdf(share / "Compta" / "Factures" / name, [text])
    make_pdf(share / "Compta" / "contrat-gruwe.pdf", ["Contrat avec Olivier GRUWE"])
    (share / "Compta" / "note-gruwe.txt").write_text("Rappeler M. Gruwe pour sa facture", encoding="utf-8")
    sync_source(FileSystemConnector("partage", share, ACL), pg, embedder,
                analyzer=client(InvoiceLight(pending_polls=0)))

    r = search_by_criteria(pg, frozenset({"compta"}), "GRUWE", doc_type="FACTURE")
    assert [m.path.rsplit("/", 1)[-1] for m in r.documents] == ["facture-001.pdf", "facture-002.pdf"]
    assert r.documents[1].fields == (("client", "Gruwé Olivier", True),)
    assert [m.path.rsplit("/", 1)[-1] for m in r.mentions] == ["facture-005.pdf", "note-gruwe.txt"]
    assert "Gruwe" in r.mentions[1].excerpt
    assert search_by_criteria(pg, frozenset({"rh"}), "GRUWE", doc_type="FACTURE").documents == []
    every = search_by_criteria(pg, None, "gruwe", field="cocontractant")  # None = console admin, sans filtre
    assert [m.doc_type for m in every.documents] == ["CONTRAT"]
    assert pg.field_names("FACTURE") == ["client", "montantTTC"]


def test_source_configs_and_purge(pg, share, embedder):
    import psycopg

    from ged.connectors import FileSystemConnector

    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute("DELETE FROM source_configs")
    cfg = {"name": "compta", "type": "filesystem", "root": "/x", "acl": {"": ["compta"]}, "synchro_minutes": 30.0}
    pg.save_source_config(cfg)
    pg.save_source_config({**cfg, "acl": {"": ["direction"]}})  # mise à jour
    assert pg.list_source_configs() == [{**cfg, "acl": {"": ["direction"]}}]

    run = pg.start_run("compta")
    sync_source(FileSystemConnector("compta", share / "Compta", {"": ["compta"]}), pg, embedder)
    pg.finish_run(run, "ok", {})
    assert pg.purge_source("compta") == 2
    assert pg.list_documents(source="compta")[1] == 0 and pg.list_runs("compta") == []
    pg.delete_source_config("compta")
    assert pg.list_source_configs() == []


def test_settings_and_bootstrap_once(pg, share):
    import psycopg

    from ged.sources import IMPORTED, bootstrap

    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute("DELETE FROM source_configs; DELETE FROM app_settings")
    src = [{"name": "p", "type": "filesystem", "root": str(share), "acl": {"": ["direction"]}}]
    assert bootstrap(pg, src) == ["p"]
    assert pg.get_setting(IMPORTED)["sources"] == ["p"]
    pg.delete_source_config("p")
    assert bootstrap(pg, src) == []  # une seule fois : une source supprimée ne revient pas
