"""Déploiement en Swarm : secrets en fichiers, verrou de synchro partagé, planification débrayable."""
from __future__ import annotations

import threading

from ged.envfiles import load_env_files, with_password
from ged.store import MemoryStore


def test_env_files(tmp_path):
    secret = tmp_path / "admin"
    secret.write_text("s3cret\n", encoding="utf-8")
    other = tmp_path.parent / "pip.conf"
    env = {"ADMIN_PASSWORD_FILE": str(secret), "SESSION_SECRET": "deja", "SESSION_SECRET_FILE": str(secret),
           "SOURCES_FILE": str(other), "OUTIL_FILE": "1", "PIP_CONFIG_FILE": "/absent/pip.conf"}
    assert load_env_files(env, secrets_dir=str(tmp_path) + "/") == ["ADMIN_PASSWORD"]
    assert env["ADMIN_PASSWORD"] == "s3cret" and env["SESSION_SECRET"] == "deja"  # la variable l'emporte
    assert "SOURCES" not in env and "OUTIL" not in env  # configuration et valeurs non-chemins ignorées


def test_missing_secret_file_stops_startup(tmp_path):
    import pytest

    with pytest.raises(SystemExit, match="ADMIN_PASSWORD_FILE"):
        load_env_files({"ADMIN_PASSWORD_FILE": str(tmp_path / "absent")}, secrets_dir=str(tmp_path) + "/")


def test_database_password_injected():
    assert with_password("postgresql://ged@db:5432/ged", "p@ss/w") == "postgresql://ged:p%40ss%2Fw@db:5432/ged"
    assert with_password("postgresql://ged:x@db/ged", "autre") == "postgresql://ged:x@db/ged"
    assert with_password("postgresql://ged@db/ged", None) == "postgresql://ged@db/ged"


def test_sync_lock_is_exclusive():
    store = MemoryStore()
    a = store.sync_lock("partage")
    assert a is not None and store.sync_lock("partage") is None and store.sync_lock("autre") is not None
    a.release()
    assert store.sync_lock("partage") is not None


def test_manager_skips_source_locked_elsewhere(connector, store, embedder):
    """Une autre instance synchronise déjà la source : rien n'est lancé ici."""
    from ged.admin import SyncManager

    held = store.sync_lock(connector.name)  # « l'autre instance »
    m = SyncManager(store, embedder, {connector.name: connector})
    assert m.start(connector.name) is None and store.list_runs(connector.name) == []
    held.release()
    run_id = m.start(connector.name)
    assert run_id is not None
    m.wait(connector.name, 10)
    assert store.sync_lock(connector.name) is not None  # verrou rendu après la synchro


def test_api_sync_conflict_and_scheduler_switch(connector, store, embedder):
    from fastapi.testclient import TestClient

    from ged.api import create_app

    held = store.sync_lock(connector.name)
    app = create_app(store, embedder, None, {connector.name: connector}, admin_password="pw", scheduler=False)
    with TestClient(app) as c:
        assert c.post(f"/sources/{connector.name}/sync", auth=("admin", "pw")).status_code == 409
        held.release()
        assert c.post(f"/sources/{connector.name}/sync", auth=("admin", "pw")).status_code == 200
        assert app.state.catalog.manager._scheduler is None  # SCHEDULER_ENABLED=false : pas de planificateur
    assert threading.active_count() >= 1


def test_pg_lock_shared_between_instances():
    """Deux PgStore (deux instances) sur la même base : un seul obtient le verrou."""
    import os

    import pytest

    dsn = os.environ.get("GED_TEST_DSN")
    if not dsn:
        pytest.skip("GED_TEST_DSN non défini")
    from ged.store import PgStore

    a, b = PgStore(dsn, 256, pool_size=1), PgStore(dsn, 256, pool_size=1)
    lock = a.sync_lock("swarm-test")
    try:
        assert lock is not None and b.sync_lock("swarm-test") is None
    finally:
        lock.release()
    again = b.sync_lock("swarm-test")
    assert again is not None
    again.release()


def test_pg_restart_does_not_interrupt_a_sync_running_elsewhere():
    """Une instance qui redémarre ne marque pas « interrompue » la synchro d'une autre instance."""
    import os

    import pytest

    dsn = os.environ.get("GED_TEST_DSN")
    if not dsn:
        pytest.skip("GED_TEST_DSN non défini")
    from ged.models import RunStatus
    from ged.store import PgStore

    a, b = PgStore(dsn, 256, pool_size=1), PgStore(dsn, 256, pool_size=1)
    a.init()
    lock = a.sync_lock("swarm-run")
    run_id = a.start_run("swarm-run")
    try:
        b.interrupt_stale_runs()  # « b » redémarre pendant que « a » synchronise
        assert next(r for r in b.list_runs("swarm-run") if r.id == run_id).status == RunStatus.RUNNING
    finally:
        lock.release()
    b.interrupt_stale_runs()  # « a » a disparu (verrou libéré) : passage interrompu
    assert next(r for r in b.list_runs("swarm-run") if r.id == run_id).status == RunStatus.INTERRUPTED
    for s in (a, b):
        s.close()


def test_empty_index_triggers_full_sync(connector, store, embedder):
    """Base perdue sans sauvegarde : au démarrage, l'instance qui planifie resynchronise tout."""
    from ged.admin import SyncManager

    m = SyncManager(store, embedder, {connector.name: connector}, auto_sync_empty=True)
    assert m.sync_if_empty() == [connector.name]
    m.wait(connector.name, 10)
    assert store.stats()[connector.name]
    assert m.sync_if_empty() == []  # index rempli, historique présent : rien


def test_auto_sync_only_on_scheduler_instance(connector, store, embedder):
    from fastapi.testclient import TestClient

    from ged.api import create_app

    web = create_app(store, embedder, None, {connector.name: connector}, scheduler=False, auto_sync_empty=True)
    with TestClient(web):
        assert store.list_runs(connector.name) == []  # instance web : jamais de synchro automatique
    worker = create_app(store, embedder, None, {connector.name: connector}, auto_sync_empty=True)
    with TestClient(worker):
        worker.state.catalog.manager.wait(connector.name, 10)
        assert store.list_runs(connector.name)


def test_api_waits_for_database(monkeypatch, tmp_path):
    """Swarm : la base démarre après l'API ; l'API patiente au lieu de planter en boucle."""
    import psycopg
    import pytest

    from ged import api

    calls = {"init": 0}

    class SlowStore:
        def __init__(self, *a, **k):
            pass

        def init(self):
            calls["init"] += 1
            if calls["init"] < 3:
                raise psycopg.OperationalError("failed to resolve host 'db'")
            raise RuntimeError("base prête")  # arrête le test ici : la suite n'est pas testée

        def close(self):
            pass

    monkeypatch.setattr("ged.store.PgStore", SlowStore)
    monkeypatch.setattr(api.time, "sleep", lambda s: None)
    monkeypatch.setenv("SOURCES_FILE", str(tmp_path / "absent.yaml"))
    with pytest.raises(RuntimeError, match="base prête"):
        api.app_from_env()
    assert calls["init"] == 3

    calls["init"] = -100  # toujours injoignable
    monkeypatch.setenv("DB_WAIT", "0")
    with pytest.raises(SystemExit, match="Swarm"):
        api.app_from_env()


def test_shutdown_interrupts_sync_without_errors(connector, store, embedder):
    """Arrêt pendant une synchro : passage « interrompu », aucun document en erreur, rien supprimé."""
    import threading

    from ged.admin import SyncManager
    from ged.models import RunStatus, Status
    from ged.sync import SyncInterrupted, sync_source

    sync_source(connector, store, embedder)
    before = {r.doc_id for r in store.list_documents(limit=100)[0]}

    class ClosingEmbedder:
        """Le client se ferme pendant la synchro (arrêt du service)."""
        dim = embedder.dim

        def __init__(self, cancel):
            self.cancel = cancel

        def embed(self, texts):
            self.cancel.set()
            raise RuntimeError("Cannot send a request, as the client has been closed.")

    for f in connector.root.rglob("*.txt"):
        f.write_text(f.read_text(encoding="utf-8") + " modifié", encoding="utf-8")
    cancel = threading.Event()
    import pytest

    with pytest.raises(SyncInterrupted):
        sync_source(connector, store, ClosingEmbedder(cancel), cancel=cancel)
    rows = store.list_documents(limit=100)[0]
    assert {r.doc_id for r in rows} == before and not any(r.status == Status.ERROR for r in rows)

    m = SyncManager(store, embedder, {connector.name: connector})
    m._cancel.set()  # arrêt déjà demandé : le passage s'arrête tout de suite
    m.start(connector.name)
    m.wait(connector.name, 10)
    assert store.list_runs(connector.name)[0].status == RunStatus.INTERRUPTED
    m.shutdown(1)
