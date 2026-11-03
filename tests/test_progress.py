"""Avancement des synchros : pourcentage de documents traités, affiché dans la console."""
from __future__ import annotations

import threading

from fastapi.testclient import TestClient

from ged.api import create_app
from ged.sync import Progress, sync_source
from tests.conftest import FakeLLM

AUTH = ("admin", "pw")


def test_progress_counts_every_document(connector, store, embedder):
    p = Progress()
    sync_source(connector, store, embedder, progress=p)
    snap = p.snapshot()
    assert snap["phase"] == "fin" and snap["total"] == snap["found"] == 4
    assert snap["done"] == 4 and snap["pct"] == 100

    p = Progress()  # deuxième passage : tout inchangé, compté aussi
    sync_source(connector, store, embedder, progress=p)
    assert p.snapshot()["done"] == 4


def test_listing_failure_stops_before_any_write(connector, store, embedder):
    def broken():
        yield from []
        raise OSError("partage démonté")

    connector.list_documents = broken
    p = Progress()
    try:
        sync_source(connector, store, embedder, progress=p)
    except OSError:
        pass
    assert p.snapshot()["phase"] == "parcours" and store.list_documents()[1] == 0


class SlowEmbedder:
    """Bloque l'indexation jusqu'à ce que le test la libère : la synchro reste « en cours »."""

    def __init__(self, inner):
        self.inner = inner
        self.dim = inner.dim
        self.gate = threading.Event()
        self.entered = threading.Event()

    def embed(self, texts):
        self.entered.set()
        self.gate.wait(10)
        return self.inner.embed(texts)


def test_console_shows_percentage_while_running(connector, store, embedder):
    slow = SlowEmbedder(embedder)
    app = create_app(store, slow, FakeLLM(), {connector.name: connector}, admin_password="pw")
    with TestClient(app) as http:
        http.post(f"/admin/sources/{connector.name}/sync", auth=AUTH)
        assert slow.entered.wait(5)
        frag = http.get(f"/admin/sources/{connector.name}/runs", auth=AUTH).text
        assert 'role="progressbar"' in frag and " / 4 documents" in frag and "%" in frag
        assert 'hx-trigger="every 2s"' in frag  # rafraîchi tant que ça tourne
        dash = http.get("/admin", auth=AUTH).text
        assert "progress compact" in dash
        slow.gate.set()
        for _ in range(100):
            frag = http.get(f"/admin/sources/{connector.name}/runs", auth=AUTH).text
            if 'role="progressbar"' not in frag:
                break
            threading.Event().wait(0.05)
        assert 'role="progressbar"' not in frag and "Terminée" in frag
