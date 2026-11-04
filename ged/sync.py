"""Synchronisation source -> index : scan complet comparé à l'état connu (delta).

- Nouveau ou modifié  -> conversion PDF des fichiers bureautiques (si configurée), extraction
                         (Alambic Light si configuré), découpage, vecteurs, indexation
- Déplacé / renommé   -> mise à jour du chemin seulement (identité stable)
- Droits changés      -> mise à jour des droits seulement
- Absent de la source -> suppression de l'index (avec garde-fou)
- Validé depuis       -> données validées dans Alambic Light reprises dans la fiche

Le parcours et la lecture des fichiers restent dans le fil principal (un seul accès à la
source) ; l'analyse et les vecteurs, lents, tournent en parallèle (`concurrence`).
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace

from ged.alambic_light import FICHE_CHUNK_NO, AlambicLight, LightRejected, fiche_text
from ged.chunking import chunk_pages, embedding_inputs
from ged.connectors.base import Connector
from ged.convert import OfficeConverter
from ged.embeddings import Embedder
from ged.extract import UnsupportedFormat, extract_pages, has_text
from ged.models import Acl, Chunk, DocState, DocumentRef, Status
from ged.store import Store

log = logging.getLogger(__name__)

NO_TEXT = ("Texte OCR non transmis par Alambic Light : seule la fiche (type et données) est "
           "interrogeable pour ce document.")
REVIEWS_PER_SYNC = 200  # résultats relus par passage, pour reprendre les validations


class DocumentRejected(Exception):
    """Échec définitif pour ce contenu (fichier abîmé selon Alambic Light) : pas de nouvel essai
    tant que le fichier ne change pas. Les autres erreurs sont retentées à chaque passage."""


class MassDeletionGuard(Exception):
    """Trop de documents ont disparu d'un coup : probablement un partage démonté, pas une vraie suppression."""


@dataclass
class SyncReport:
    added: int = 0
    updated: int = 0
    moved: int = 0
    acl_changed: int = 0
    unchanged: int = 0
    deleted: int = 0
    ocr_required: int = 0
    converted: int = 0  # fichiers bureautiques convertis en PDF
    analysed: int = 0  # envoyés à Alambic Light
    validated: int = 0  # données validées reprises depuis Alambic Light
    errors: list[str] = field(default_factory=list)


class Progress:
    """Avancement d'une synchro, lu par la console pendant qu'elle tourne.

    Phases : parcours (liste des fichiers, total encore inconnu), traitement (total connu),
    validations (reprise des données validées dans Alambic Light), fin.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.phase = "parcours"
        self.found = 0  # fichiers trouvés pendant le parcours
        self.total = 0
        self.done = 0
        self.current: str | None = None
        self.started = time.time()
        self.processing_started: float | None = None

    def step(self, current: str | None = None) -> None:
        with self._lock:
            self.done += 1
            if current is not None:
                self.current = current

    def snapshot(self) -> dict:
        with self._lock:
            pct = (100 * self.done / self.total) if self.total else 0
            eta = None
            if self.phase == "traitement" and self.processing_started and self.done >= 5:
                elapsed = time.time() - self.processing_started
                eta = elapsed / self.done * (self.total - self.done)
            return {"phase": self.phase, "found": self.found, "total": self.total, "done": self.done,
                    "pct": pct, "current": self.current, "eta": eta}


class _Indexer:
    def __init__(self, connector, store, embedder, analyzer, report, max_chars, overlap, converter=None,
                 analyse: bool = True, ocr_ia: bool = False):
        self.converter: OfficeConverter | None = converter
        self.connector = connector
        self.store = store
        self.embedder = embedder
        self.analyzer: AlambicLight | None = analyzer
        self.analyse = analyse  # False : source « texte seul », Alambic Light ne sert qu'à l'OCR des scans
        self.ocr_ia = ocr_ia  # texte seul : lecture des scans par l'IA (ocr=ia)
        self.report = report
        self.max_chars = max_chars
        self.overlap = overlap
        self._lock = threading.Lock()

    def count(self, attr: str, n: int = 1) -> None:
        with self._lock:
            setattr(self.report, attr, getattr(self.report, attr) + n)

    def fail(self, ref: DocumentRef, acl: Acl, exc: Exception, content_hash: str = "") -> None:
        log.error("Échec d'indexation : %s (%s)", ref.path, exc)
        with self._lock:
            self.report.errors.append(f"{ref.path}: {exc}")
        # empreinte connue = échec définitif pour ce contenu (voir Store.get_state)
        self.store.upsert_document(ref, content_hash, acl, Status.ERROR, [], [], error=str(exc))

    def index(self, ref: DocumentRef, acl: Acl, prev: DocState | None, data: bytes, content_hash: str) -> None:
        try:
            self._index(ref, acl, prev, data, content_hash)
        except DocumentRejected as exc:
            self.fail(ref, acl, exc, content_hash)
        except Exception as exc:  # un document en échec ne bloque pas les autres
            self.fail(ref, acl, exc)

    def _index(self, ref, acl, prev, data, content_hash) -> None:
        # Fichier bureautique → PDF : vraies pages, et document accepté par Alambic Light.
        # Échec de conversion = erreur retentée au passage suivant (pas de repli sans pagination).
        name = ref.path
        if self.converter and self.converter.accepts(ref.path):
            data = self.converter.to_pdf(ref.path, data)
            name = f"{ref.path}.pdf"
            self.count("converted")
        try:
            pages = extract_pages(name, data)
        except UnsupportedFormat:
            self.store.upsert_document(ref, content_hash, acl, Status.UNSUPPORTED, [], [])
            return

        meta, note = None, None
        if self.analyzer and not self.analyse:
            if not has_text(pages) and self.analyzer.accepts(name):
                try:
                    ocr_pages, error = self.analyzer.ocr(
                        data, name, source=f"alambic-search:{ref.source}", identifiant=ref.path, ia_ocr=self.ocr_ia)
                except LightRejected as exc:
                    log.info("Alambic Light refuse %s : %s", ref.path, exc)
                    ocr_pages, error = None, None
                self.count("analysed")
                if error:
                    raise DocumentRejected(f"Alambic Light (OCR) : {error}")
                if ocr_pages:
                    pages = ocr_pages
        elif self.analyzer and self.analyzer.wanted(name, has_text(pages)):
            try:
                analysis = self.analyzer.analyze(
                    data, name, source=f"alambic-search:{ref.source}", identifiant=ref.path
                )
            except LightRejected as exc:  # format ou taille refusés : on garde le texte local
                log.info("Alambic Light refuse %s : %s", ref.path, exc)
                analysis = None
            if analysis is not None:
                self.count("analysed")
                if analysis.failed and not has_text(pages):
                    raise DocumentRejected(f"Alambic Light : {analysis.error or 'lecture impossible'}")
                meta = analysis.meta
                if not has_text(pages) and analysis.pages:
                    pages = analysis.pages  # scan : le texte vient de l'OCR d'Alambic Light

        chunks = chunk_pages(pages, max_chars=self.max_chars, overlap=self.overlap) if has_text(pages) else []
        fiche = fiche_text(meta) if meta else None
        if fiche:
            chunks = [Chunk(FICHE_CHUNK_NO, 1, fiche), *chunks]
            if not has_text(pages):
                note = NO_TEXT
        if not chunks:
            self.store.upsert_document(ref, content_hash, acl, Status.OCR_REQUIRED, [], [], meta=meta)
            self.count("ocr_required")
            return

        vectors = self.embedder.embed(embedding_inputs(ref.path, meta.label if meta else None, [c.text for c in chunks]))
        self.store.upsert_document(ref, content_hash, acl, Status.INDEXED, chunks, vectors, error=note, meta=meta)
        self.count("updated" if prev else "added")

    def refresh_reviews(self, seen: set[str]) -> None:
        """Documents dont des données étaient « à vérifier » : relit leur résultat dans Alambic Light.
        Une fois validées par un gestionnaire, les données validées remplacent la fiche."""
        for doc_id, meta in self.store.pending_reviews(self.connector.name, limit=REVIEWS_PER_SYNC):
            if doc_id not in seen or not meta.light_uuid:
                continue
            try:
                new = refresh_review(self.store, self.embedder, self.analyzer, self.connector.name, doc_id, meta)
            except Exception as exc:  # Alambic Light indisponible : on réessaiera
                log.warning("Relecture du résultat %s impossible : %s", meta.light_uuid, exc)
                return
            if new is not None and new.validated:
                self.count("validated")


def refresh_review(store: Store, embedder: Embedder, analyzer: AlambicLight, source: str, doc_id: str, meta):
    """Relit dans Alambic Light le résultat d'un document et met sa fiche à jour.
    Renvoie la nouvelle fiche, ou None si rien n'a changé. Lève si Alambic Light ne répond pas."""
    fresh = analyzer.result(meta.light_uuid)
    if fresh is None:  # résultat expiré côté Alambic Light : on cesse de le relire
        new = replace(meta, light_status="EXPIRE")
    elif fresh.meta.validated and not fresh.meta.fields and meta.fields:
        # Validé, mais données déjà effacées (export acquitté) : on garde les valeurs
        # connues, toujours « à vérifier », plutôt qu'une fiche vide ; plus de relecture.
        new = replace(meta, light_status="EFFACE")
    elif (fresh.meta.light_status, fresh.meta.to_check, fresh.meta.fields) != (
        meta.light_status, meta.to_check, meta.fields
    ):
        new = replace(fresh.meta, label=fresh.meta.label or meta.label)
    else:
        return None
    if new == meta:
        return None
    fiche = fiche_text(new)
    state = store.get_document(source, doc_id)
    path = state.path if state is not None else doc_id
    vector = embedder.embed(embedding_inputs(path, new.label, [fiche]))[0] if fiche else None
    store.update_meta(source, doc_id, new, fiche, vector)
    return new


ANALYSIS_SETTING = "analyse_light:"


def _apply_analysis_mode(store: Store, source: str, analyse: bool) -> None:
    """Changement de réglage « type et données » d'une source, appliqué à la synchro :
    désactivé → type, données et fiches retirés ; réactivé → documents non analysés repris."""
    before = store.get_setting(ANALYSIS_SETTING + source)
    if not analyse:
        n = store.strip_light(source)
        if n:
            log.info("Source %s en texte seul : type et données retirés de %d document(s)", source, n)
    elif before is False:
        n = store.mark_for_analysis(source)
        log.info("Source %s : analyse Alambic Light réactivée, %d document(s) à analyser", source, n)
    if before is not analyse:
        store.set_setting(ANALYSIS_SETTING + source, analyse)


def sync_source(
    connector: Connector,
    store: Store,
    embedder: Embedder,
    *,
    analyzer: AlambicLight | None = None,
    converter: OfficeConverter | None = None,
    max_chars: int = 1200,
    overlap: int = 200,
    max_delete_ratio: float = 0.5,
    progress: Progress | None = None,
    analyse: bool | None = None,
    ocr_ia: bool | None = None,
) -> SyncReport:
    """`analyse` : type et données par Alambic Light (défaut : réglage de la source, analyse_light).
    False : texte seul, Alambic Light ne sert qu'à l'OCR des scans (ocr_only)."""
    report = SyncReport()
    if analyse is None:
        analyse = getattr(connector, "analyse_light", True)
    if ocr_ia is None:
        ocr_ia = getattr(connector, "ocr_ia", False)
    if analyzer is not None:
        _apply_analysis_mode(store, connector.name, analyse)
    progress = progress or Progress()
    known = store.get_state(connector.name)
    seen: set[str] = set()
    ix = _Indexer(connector, store, embedder, analyzer, report, max_chars, overlap, converter, analyse, ocr_ia)

    workers = analyzer.concurrency if analyzer else 1
    slots = threading.BoundedSemaphore(workers * 2)  # fichiers lus en attente d'analyse : mémoire bornée
    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix=f"index-{connector.name}")
    # Parcours complet d'abord : donne le total (pourcentage), et un listing qui échoue arrête
    # tout AVANT la moindre écriture ou suppression.
    refs = []
    for ref in connector.list_documents():
        refs.append(ref)
        progress.found += 1
    progress.total = len(refs)
    progress.phase = "traitement"
    progress.processing_started = time.time()

    def finished(_f, path: str) -> None:
        slots.release()
        progress.step(path)

    try:
        for ref in refs:
            seen.add(ref.doc_id)
            acl = connector.acl(ref)
            prev = known.get(ref.doc_id)

            if prev and prev.mtime == ref.mtime and prev.size == ref.size:
                if prev.path != ref.path or prev.acl != acl:
                    store.update_metadata(ref, acl)
                    ix.count("moved", prev.path != ref.path)
                    ix.count("acl_changed", prev.acl != acl)
                else:
                    ix.count("unchanged")
                progress.step()
                continue

            try:
                data = connector.read_bytes(ref)
            except Exception as exc:
                ix.fail(ref, acl, exc)
                progress.step(ref.path)
                continue
            content_hash = hashlib.sha256(data).hexdigest()
            if prev and prev.content_hash == content_hash:
                store.update_metadata(ref, acl)  # simple "touch" : contenu identique
                ix.count("unchanged")
                progress.step()
                continue

            slots.acquire()
            progress.current = ref.path
            future = pool.submit(ix.index, ref, acl, prev, data, content_hash)
            future.add_done_callback(lambda f, p=ref.path: finished(f, p))
    finally:
        pool.shutdown(wait=True)

    if analyzer and analyse:
        progress.phase = "validations"
        ix.refresh_reviews(seen)
    progress.phase = "fin"

    gone = set(known) - seen
    if known and len(gone) / len(known) > max_delete_ratio:
        raise MassDeletionGuard(
            f"{len(gone)}/{len(known)} documents absents de '{connector.name}' : suppression bloquée. "
            "Vérifier que la source est bien accessible, puis relancer avec un seuil plus élevé si c'est voulu."
        )
    for doc_id in gone:
        store.delete_document(connector.name, doc_id)
        report.deleted += 1
    return report


def reembed(store: Store, embedder: Embedder, source: str | None = None, progress=None) -> int:
    """Recalcule les vecteurs des passages déjà indexés (contexte : document, type, section),
    sans relire les fichiers ni rappeler Alambic Light. Renvoie le nombre de documents traités."""
    n = 0
    for src, doc_id, path, label, chunks in store.iter_document_chunks(source):
        if not chunks:
            continue
        vectors = embedder.embed(embedding_inputs(path, label, [c.text for c in chunks]))
        store.update_vectors(src, doc_id, [(c.chunk_no, v) for c, v in zip(chunks, vectors, strict=True)])
        n += 1
        if progress:
            progress(n, path)
    return n
