import os
import shutil

import pytest

from ged.connectors import FileSystemConnector
from ged.models import Status
from ged.sync import MassDeletionGuard, sync_source
from tests.conftest import ACL


def statuses(store):
    return {d["state"].path: d["status"] for d in store.docs.values()}


def test_initial_then_idempotent(connector, store, embedder):
    r1 = sync_source(connector, store, embedder)
    assert (r1.added, r1.errors) == (4, [])
    r2 = sync_source(connector, store, embedder)
    assert (r2.added, r2.updated, r2.unchanged) == (0, 0, 4)


def test_modification_reindexes(connector, store, embedder, share):
    sync_source(connector, store, embedder)
    f = share / "Public/horaires.txt"
    f.write_text("Nouveaux horaires : ouvert le samedi matin.", encoding="utf-8")
    os.utime(f, (1, 1_900_000_000))
    r = sync_source(connector, store, embedder)
    assert r.updated == 1
    texts = [c.text for chunks in store.chunks.values() for c, _ in chunks]
    assert any("samedi" in t for t in texts) and not any("lundi au vendredi" in t for t in texts)


def test_touch_without_change_is_not_reindexed(connector, store, embedder, share):
    sync_source(connector, store, embedder)
    os.utime(share / "Public/horaires.txt", (1, 1_900_000_000))
    r = sync_source(connector, store, embedder)
    assert (r.updated, r.unchanged) == (0, 4)


def test_move_updates_path_and_acl_only(connector, store, embedder, share):
    sync_source(connector, store, embedder)
    shutil.move(share / "RH/salaires.txt", share / "Public/salaires.txt")
    r = sync_source(connector, store, embedder)
    assert (r.moved, r.acl_changed, r.added, r.deleted) == (1, 1, 0, 0)
    state = next(d["state"] for d in store.docs.values() if d["state"].path == "Public/salaires.txt")
    assert state.acl.allow == {"tous"}


def test_deletion(connector, store, embedder, share):
    sync_source(connector, store, embedder)
    (share / "Public/horaires.txt").unlink()
    r = sync_source(connector, store, embedder)
    assert r.deleted == 1
    assert "Public/horaires.txt" not in statuses(store)


def test_mass_deletion_guard(connector, store, embedder, share):
    sync_source(connector, store, embedder)
    for sub in ("Compta", "RH", "Public"):
        shutil.rmtree(share / sub)
    with pytest.raises(MassDeletionGuard):
        sync_source(connector, store, embedder)
    assert len(store.docs) == 4  # rien n'a été supprimé


def test_scanned_pdf_marked_for_ocr(connector, store, embedder, share):
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(share / "Public/scan.pdf"))
    c.rect(50, 50, 200, 200)  # une "image", pas de texte
    c.showPage()
    c.save()
    r = sync_source(connector, store, embedder)
    assert r.ocr_required == 1
    assert statuses(store)["Public/scan.pdf"] == Status.OCR_REQUIRED


def test_broken_file_is_reported_and_retried(connector, store, embedder, share):
    (share / "Public/casse.pdf").write_bytes(b"%PDF-1.4 pas vraiment un pdf")
    r1 = sync_source(connector, store, embedder)
    assert len(r1.errors) == 1 and r1.added == 4
    r2 = sync_source(connector, store, embedder)
    assert len(r2.errors) == 1  # retenté au passage suivant, pas oublié


def test_listing_failure_midway_aborts_before_deleting(connector, store, embedder):
    sync_source(connector, store, embedder)

    class Flaky:
        name = connector.name

        def list_documents(self):
            yield next(iter(connector.list_documents()))
            raise OSError("Partage injoignable")

        def __getattr__(self, attr):
            return getattr(connector, attr)

    with pytest.raises(OSError):
        sync_source(Flaky(), store, embedder)
    assert len(store.docs) == 4


def test_unreadable_share_aborts_before_deleting(store, embedder, share):
    c = FileSystemConnector("partage", share, ACL)
    sync_source(c, store, embedder)
    os.chmod(share / "Compta", 0)
    try:
        if os.access(share / "Compta", os.R_OK):
            pytest.skip("exécuté en root : impossible de simuler un dossier illisible")
        with pytest.raises(PermissionError):
            sync_source(c, store, embedder)
        assert len(store.docs) == 4
    finally:
        os.chmod(share / "Compta", 0o755)
