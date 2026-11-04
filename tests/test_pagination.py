"""Pagination de la liste des documents et de l'historique des synchronisations."""
from __future__ import annotations

from fastapi.testclient import TestClient

from ged.api import create_app
from ged.sync import sync_source


def client(connector, store, embedder):
    return TestClient(create_app(store, embedder, None, {connector.name: connector}, admin_password="pw"))


def test_documents_page_size_and_numbers(connector, store, embedder, share):
    for i in range(60):
        (share / "Compta" / f"note-{i:02}.txt").write_text(f"Note numéro {i}", encoding="utf-8")
    sync_source(connector, store, embedder)
    c = client(connector, store, embedder)
    page = c.get("/admin/documents", params={"n": 25}, auth=("admin", "pw")).text
    assert 'aria-current="page">1<' in page and "p=3" in page and "1 à 25" in page
    assert '<option value="25" selected>' in page
    last = c.get("/admin/documents", params={"n": 25, "p": 99}, auth=("admin", "pw")).text  # au-delà : dernière
    assert 'aria-current="page">3<' in last
    assert '<option value="50" selected>' in c.get("/admin/documents", params={"n": 7}, auth=("admin", "pw")).text
    frag = c.get("/admin/documents", params={"n": 25, "p": 2}, auth=("admin", "pw"),
                 headers={"HX-Request": "true", "HX-Target": "doc-results"}).text
    assert frag.lstrip().startswith('<section class="section" style="margin: 0" id="doc-results">')
    assert "26 à 50" in frag


def test_runs_are_paginated(connector, store, embedder):
    for _ in range(23):
        store.finish_run(store.start_run(connector.name), "ok", {"added": 0})
    assert store.count_runs(connector.name) == 23
    assert len(store.list_runs(connector.name, limit=10, offset=20)) == 3
    c = client(connector, store, embedder)
    html = c.get("/admin/sources", auth=("admin", "pw")).text
    assert "23 synchronisations" in html and f"/admin/sources/{connector.name}/runs?p=3" in html
    frag = c.get(f"/admin/sources/{connector.name}/runs", params={"p": 3}, auth=("admin", "pw")).text
    assert 'aria-current="page">3<' in frag and frag.count("<tr>") == 1 + 3  # en-tête + 3 lignes
