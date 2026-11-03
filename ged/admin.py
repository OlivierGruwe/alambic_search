"""Exécution des synchronisations en tâche de fond, avec historique et planification.

Une seule synchro à la fois par source. Les sources avec `synchro_minutes` sont resynchronisées
automatiquement (nouveaux fichiers, droits, validations faites dans Alambic Light). Au démarrage, les synchros restées
« en cours » (service arrêté brutalement) sont marquées « interrompues ».
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import asdict

from ged.embeddings import Embedder
from ged.models import RunStatus
from ged.store import Store
from ged.sync import MassDeletionGuard, Progress, sync_source

log = logging.getLogger(__name__)


class SyncManager:
    def __init__(self, store: Store, embedder: Embedder, connectors: dict, analyzer=None, converter=None,
                 schedule: dict[str, float] | None = None, clock=time.time):
        self.schedule = {k: v for k, v in (schedule or {}).items() if k in connectors and v and v > 0}
        self._clock = clock
        self._stop = threading.Event()
        self._scheduler: threading.Thread | None = None
        self.converter = converter  # conversion PDF des fichiers bureautiques, ou None
        self.store = store
        self.embedder = embedder
        self.connectors = connectors
        self.analyzer = analyzer  # Alambic Light (OCR, type, données), ou None
        self._threads: dict[str, threading.Thread] = {}
        self._progress: dict[str, Progress] = {}
        self._guard = threading.Lock()
        store.interrupt_stale_runs()

    def is_running(self, source: str) -> bool:
        t = self._threads.get(source)
        return t is not None and t.is_alive()

    def start(self, source: str, *, max_delete_ratio: float = 0.5, full: bool = False) -> int | None:
        """Lance la synchro en arrière-plan. Renvoie l'id du passage, ou None si déjà en cours.

        `full` : relit aussi tous les droits (sinon seuls les droits modifiés ou anciens sont relus).
        """
        if source not in self.connectors:
            raise KeyError(source)
        if full and hasattr(self.connectors[source], "invalidate_acls"):
            self.connectors[source].invalidate_acls()
        with self._guard:
            if self.is_running(source):
                return None
            run_id = self.store.start_run(source)
            self._progress[source] = Progress()
            t = threading.Thread(
                target=self._run, args=(source, run_id, max_delete_ratio), name=f"sync-{source}", daemon=True
            )
            self._threads[source] = t
            t.start()
            return run_id

    # --- Planification -----------------------------------------------------------------

    def next_run(self, source: str) -> float | None:
        """Heure (epoch) de la prochaine synchro automatique, None si la source n'est pas planifiée."""
        minutes = self.schedule.get(source)
        if not minutes:
            return None
        runs = self.store.list_runs(source, limit=1)
        return runs[0].started_at + minutes * 60 if runs else self._clock()

    def tick(self) -> list[str]:
        """Lance les synchros planifiées arrivées à échéance. Renvoie les sources lancées."""
        started = []
        for source in self.schedule:
            nxt = self.next_run(source)
            if nxt is not None and nxt <= self._clock() and not self.is_running(source):
                if self.start(source) is not None:
                    log.info("Synchronisation planifiée de %s", source)
                    started.append(source)
        return started

    def start_scheduler(self, every: float = 30.0) -> None:
        self._every = every
        self._wanted = True  # l'application tourne : une source planifiée ajoutée plus tard démarre aussi
        if not self.schedule or self._scheduler is not None:
            return

        def loop() -> None:
            while not self._stop.wait(every):
                try:
                    self.tick()
                except Exception:  # base indisponible un instant : on réessaie au tour suivant
                    log.exception("Planification des synchronisations")

        self._stop.clear()
        self._scheduler = threading.Thread(target=loop, name="sync-scheduler", daemon=True)
        self._scheduler.start()
        log.info("Synchronisations planifiées : %s",
                 ", ".join(f"{s} toutes les {m:g} min" for s, m in self.schedule.items()))

    def ensure_scheduler(self) -> None:
        """Après l'ajout d'une source planifiée depuis la console."""
        if getattr(self, "_wanted", False) and self._scheduler is None and self.schedule:
            self.start_scheduler(self._every)

    def stop_scheduler(self) -> None:
        self._wanted = False
        self._stop.set()
        self._scheduler = None

    def progress(self, source: str) -> dict | None:
        """Avancement de la synchro en cours de la source, ou None."""
        p = self._progress.get(source)
        return p.snapshot() if p is not None and self.is_running(source) else None

    def wait(self, source: str, timeout: float | None = None) -> None:
        t = self._threads.get(source)
        if t:
            t.join(timeout)

    def _run(self, source: str, run_id: int, max_delete_ratio: float) -> None:
        try:
            report = sync_source(self.connectors[source], self.store, self.embedder,
                                 analyzer=self.analyzer, converter=self.converter,
                                 max_delete_ratio=max_delete_ratio, progress=self._progress[source])
            status = RunStatus.PARTIAL if report.errors else RunStatus.OK
            self.store.finish_run(run_id, status, asdict(report))
        except MassDeletionGuard as exc:
            self.store.finish_run(run_id, RunStatus.BLOCKED, message=str(exc))
        except Exception as exc:  # source injoignable, base indisponible...
            log.exception("Synchronisation %s en échec", source)
            self.store.finish_run(run_id, RunStatus.FAILED, message=f"{type(exc).__name__} : {exc}")
