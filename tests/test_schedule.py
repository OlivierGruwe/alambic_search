"""Synchros planifiées (synchro_minutes)."""
from __future__ import annotations

import pytest

from ged.admin import SyncManager
from ged.config import ConfigError, load_schedule


class Clock:
    def __init__(self, t: float):
        self.t = t

    def __call__(self) -> float:
        return self.t


def test_tick_runs_due_sources_only(connector, store, embedder):
    clock = Clock(1_000_000.0)
    m = SyncManager(store, embedder, {connector.name: connector}, schedule={connector.name: 60}, clock=clock)
    assert m.tick() == [connector.name]  # jamais synchronisée : tout de suite
    m.wait(connector.name, 10)
    assert store.list_runs(connector.name)[0].status == "ok"

    last = store.list_runs(connector.name)[0].started_at
    clock.t = last + 59 * 60
    assert m.tick() == []
    clock.t = last + 60 * 60
    assert m.tick() == [connector.name]
    m.wait(connector.name, 10)


def test_unscheduled_source_never_runs(connector, store, embedder):
    m = SyncManager(store, embedder, {connector.name: connector}, schedule={"autre": 60})
    assert m.schedule == {} and m.tick() == [] and m.next_run(connector.name) is None


def test_load_schedule(tmp_path):
    cfg = tmp_path / "s.yaml"
    src = "type: filesystem, root: /tmp"
    cfg.write_text(f"sources:\n  - {{name: a, {src}, synchro_minutes: 30}}\n  - {{name: b, {src}}}\n"
                   f"  - {{name: c, {src}, synchro_minutes: 0}}\n")
    assert load_schedule(cfg) == {"a": 30.0}
    cfg.write_text("sources:\n  - {name: a, type: filesystem, root: /tmp, synchro_minutes: 1}\n")
    with pytest.raises(ConfigError, match="au moins 5"):
        load_schedule(cfg)


def test_sources_page_shows_schedule(connector, store, embedder, llm):
    from fastapi.testclient import TestClient

    from ged.api import create_app

    app = create_app(store, embedder, llm, {connector.name: connector}, admin_password="pw",
                     schedule={connector.name: 60})
    with TestClient(app) as http:  # lance aussi le planificateur (lifespan)
        page = http.get("/admin/sources", auth=("admin", "pw")).text
    assert "Automatique, toutes les 60 min" in page
