"""Index : documents (métadonnées, droits, pointeur), extraits, plein texte et vecteurs.

`PgStore`     : PostgreSQL + pgvector (cible du POC).
`MemoryStore` : même contrat, en mémoire, pour les tests.

La recherche est hybride : plein texte + vecteurs, fusionnés par Reciprocal Rank
Fusion (RRF). Le filtrage par droits se fait AVANT le classement : un document
non autorisé ne peut jamais entrer dans le contexte du LLM.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import math
import re
import threading
import time
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Protocol

from ged.models import Acl, Chunk, DocMeta, DocRow, DocState, DocumentRef, Hit, Match, RunStatus, Status, SyncRun

RRF_K = 60


def _day(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def _last_days(days: int) -> list[str]:
    today = dt.date.today()
    return [(today - dt.timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]


class Store(Protocol):
    def init(self) -> None: ...
    def get_state(self, source: str) -> dict[str, DocState]: ...
    def get_document(self, source: str, doc_id: str) -> DocState | None: ...
    def upsert_document(
        self,
        ref: DocumentRef,
        content_hash: str,
        acl: Acl,
        status: str,
        chunks: list[Chunk],
        vectors: list[list[float]],
        error: str | None = None,
        meta: DocMeta | None = None,
    ) -> None: ...
    def update_metadata(self, ref: DocumentRef, acl: Acl) -> None: ...
    def delete_document(self, source: str, doc_id: str) -> None: ...
    def search(
        self, query: str, vector: list[float], groups: frozenset[str], k: int = 8,
        doc_types: frozenset[str] | None = None,
    ) -> list[Hit]: ...

    # --- Alambic Light -------------------------------------------------------------
    def pending_reviews(self, source: str, limit: int = 200) -> list[tuple[str, DocMeta]]: ...
    def update_meta(
        self, source: str, doc_id: str, meta: DocMeta, fiche: str | None, vector: list[float] | None
    ) -> None: ...
    def type_counts(self) -> list[tuple[str, str, int]]: ...
    def strip_light(self, source: str) -> int: ...
    def sample_chunks(self, n: int, source: str | None = None) -> list[dict]: ...
    def mark_for_analysis(self, source: str) -> int: ...

    # --- Recherche par critères (liste exhaustive, filtrée par droits) ---------------
    def find_by_fields(
        self, groups: frozenset[str] | None, value: str, doc_type: str | None = None,
        field: str | None = None, limit: int = 500,
    ) -> list[Match]: ...
    def find_mentions(
        self, groups: frozenset[str] | None, value: str, doc_type: str | None = None, limit: int = 200,
    ) -> list[Match]: ...
    def field_names(self, doc_type: str | None = None) -> list[str]: ...

    # --- Sources ajoutées depuis la console (celles de sources.yaml n'y sont pas) ----
    def list_source_configs(self) -> list[dict]: ...
    def save_source_config(self, config: dict) -> None: ...
    def delete_source_config(self, name: str) -> None: ...
    def get_source_secrets(self, source: str) -> dict[str, str]: ...
    def set_source_secret(self, source: str, field: str, token: str | None) -> None: ...
    def list_all_source_secrets(self) -> list[tuple[str, str, str]]: ...
    def purge_source(self, name: str) -> int: ...
    def get_setting(self, key: str): ...
    def set_setting(self, key: str, value) -> None: ...

    # --- Console d'administration --------------------------------------------------
    def stats(self) -> dict[str, Counter]: ...
    def list_documents(
        self, source: str | None = None, status: str | None = None, q: str | None = None,
        offset: int = 0, limit: int = 50, doc_type: str | None = None,
    ) -> tuple[list[DocRow], int]: ...
    def document_row(self, source: str, doc_id: str) -> DocRow | None: ...
    def document_chunks(self, source: str, doc_id: str, limit: int = 20) -> list[Chunk]: ...
    def start_run(self, source: str) -> int: ...
    def finish_run(self, run_id: int, status: str, report: dict | None = None, message: str | None = None) -> None: ...
    def list_runs(self, source: str | None = None, limit: int = 20) -> list[SyncRun]: ...
    def interrupt_stale_runs(self) -> int: ...
    def log_question(
        self, groups: frozenset[str], question: str, hits: int, citations: int,
        duration_ms: int, sandbox: bool = False,
    ) -> None: ...
    def question_activity(self, days: int = 14) -> list[tuple[str, int, int]]: ...
    def purge_questions(self, older_than_days: int) -> int: ...


# --------------------------------------------------------------------------------------
# PostgreSQL + pgvector
# --------------------------------------------------------------------------------------

SCHEMA = """
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS unaccent;  -- recherche par critères : « Gruwé » = « GRUWE »

CREATE TABLE IF NOT EXISTS documents (
    source        text NOT NULL,
    doc_id        text NOT NULL,
    path          text NOT NULL,
    mtime         double precision NOT NULL,
    size          bigint NOT NULL,
    content_hash  text NOT NULL,
    acl_groups    text[] NOT NULL DEFAULT '{{}}',
    status        text NOT NULL,
    error         text,
    indexed_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source, doc_id)
);
-- Refus explicites (ACE « deny » NTFS) : ajoutés après coup, d'où l'ALTER
ALTER TABLE documents ADD COLUMN IF NOT EXISTS acl_deny text[] NOT NULL DEFAULT '{{}}';
-- Alambic Light : type, données extraites (filtres, fiche) et suivi de leur validation
ALTER TABLE documents ADD COLUMN IF NOT EXISTS doc_type        text;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS doc_label       text;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS doc_fields      jsonb;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS fields_to_check text[] NOT NULL DEFAULT '{{}}';
ALTER TABLE documents ADD COLUMN IF NOT EXISTS doc_confidence  real;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS light_uuid      text;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS light_status    text;
CREATE INDEX IF NOT EXISTS documents_type_idx ON documents (doc_type);
CREATE INDEX IF NOT EXISTS documents_acl_idx  ON documents USING gin (acl_groups);
CREATE INDEX IF NOT EXISTS documents_hash_idx ON documents (content_hash);

CREATE TABLE IF NOT EXISTS chunks (
    id         bigserial PRIMARY KEY,
    source     text NOT NULL,
    doc_id     text NOT NULL,
    chunk_no   int  NOT NULL,
    page       int  NOT NULL,
    text       text NOT NULL,
    tsv        tsvector GENERATED ALWAYS AS (to_tsvector('french', text)) STORED,
    embedding  vector({dim}) NOT NULL,
    FOREIGN KEY (source, doc_id) REFERENCES documents (source, doc_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS chunks_doc_idx ON chunks (source, doc_id);
CREATE INDEX IF NOT EXISTS chunks_tsv_idx ON chunks USING gin (tsv);
CREATE INDEX IF NOT EXISTS chunks_vec_idx ON chunks USING hnsw (embedding vector_cosine_ops);

CREATE TABLE IF NOT EXISTS sync_runs (
    id           bigserial PRIMARY KEY,
    source       text NOT NULL,
    started_at   timestamptz NOT NULL DEFAULT now(),
    finished_at  timestamptz,
    status       text NOT NULL,
    report       jsonb,
    message      text
);
CREATE INDEX IF NOT EXISTS sync_runs_source_idx ON sync_runs (source, started_at DESC);

-- Journal des questions : sert aux statistiques de la console.
-- Contient le texte des questions : rétention à purger (purge_questions).
CREATE TABLE IF NOT EXISTS question_log (
    id           bigserial PRIMARY KEY,
    at           timestamptz NOT NULL DEFAULT now(),
    groups       text[] NOT NULL,
    question     text NOT NULL,
    hits         int NOT NULL,
    citations    int NOT NULL,
    duration_ms  int NOT NULL,
    sandbox      boolean NOT NULL DEFAULT false
);
CREATE INDEX IF NOT EXISTS question_log_at_idx ON question_log (at);

-- Sources ajoutées depuis la console. Les secrets n'y sont jamais : soit le nom de la
-- variable d'environnement (password_env), soit un secret chiffré dans source_secrets.
-- Réglages de l'application (ex. date du premier import des sources de sources.yaml)
CREATE TABLE IF NOT EXISTS app_settings (
    key    text PRIMARY KEY,
    value  jsonb NOT NULL
);

CREATE TABLE IF NOT EXISTS source_configs (
    name        text PRIMARY KEY,
    config      jsonb NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- Secrets saisis dans la console, chiffrés (AES-256-GCM, clé SECRETS_KEY hors de la base)
CREATE TABLE IF NOT EXISTS source_secrets (
    source      text NOT NULL,
    field       text NOT NULL,
    value       text NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source, field)
);
"""

HYBRID_SEARCH = """
WITH allowed AS (
    SELECT source, doc_id FROM documents
    WHERE status = 'indexed' AND acl_groups && %(groups)s::text[]
      AND NOT acl_deny && %(groups)s::text[]   -- un refus explicite l'emporte
      AND (%(types)s::text[] IS NULL OR doc_type = ANY(%(types)s::text[]))
),
-- Question en langage naturel : OU entre les termes (un ET exigerait tous les mots),
-- ts_rank_cd favorise ensuite les extraits qui en couvrent le plus.
q AS (
    SELECT NULLIF(replace(plainto_tsquery('french', %(q)s)::text, ' & ', ' | '), '')::tsquery AS tsq
),
fts AS (
    SELECT c.id, row_number() OVER (ORDER BY ts_rank_cd(c.tsv, q.tsq) DESC) AS r
    FROM chunks c JOIN allowed a USING (source, doc_id), q
    WHERE c.tsv @@ q.tsq
    ORDER BY ts_rank_cd(c.tsv, q.tsq) DESC
    LIMIT %(pool)s
),
vec AS (
    SELECT c.id, row_number() OVER (ORDER BY c.embedding <=> %(v)s::vector) AS r
    FROM chunks c JOIN allowed a USING (source, doc_id)
    ORDER BY c.embedding <=> %(v)s::vector
    LIMIT %(pool)s
)
SELECT c.source, c.doc_id, d.path, c.page, c.text, d.content_hash,
       COALESCE(1.0 / (%(rrf)s + fts.r), 0) + COALESCE(1.0 / (%(rrf)s + vec.r), 0) AS score,
       d.doc_label
FROM (SELECT id FROM fts UNION SELECT id FROM vec) ids
JOIN chunks c ON c.id = ids.id
JOIN documents d ON d.source = c.source AND d.doc_id = c.doc_id
LEFT JOIN fts ON fts.id = c.id
LEFT JOIN vec ON vec.id = c.id
ORDER BY score DESC
LIMIT %(k)s
"""


_META_COLS = ("d.doc_type, d.doc_label, d.doc_fields, d.fields_to_check, d.doc_confidence, "
              "d.light_uuid, d.light_status")


def _meta_params(meta: DocMeta | None) -> tuple:
    if meta is None:
        return (None, None, None, [], None, None, None)
    return (meta.doc_type, meta.label, json.dumps(meta.fields, ensure_ascii=False, default=str),
            list(meta.to_check), meta.confidence, meta.light_uuid, meta.light_status)


def _meta_from(r) -> DocMeta | None:
    doc_type, label, fields, to_check, confidence, light_uuid, light_status = r
    if light_uuid is None and doc_type is None:
        return None
    return DocMeta(doc_type, label, fields or {}, tuple(to_check or ()), light_uuid, light_status, confidence)


def normalize(text: str) -> str:
    """Minuscules, sans accents : « M. Gruwé » → « m. gruwe » (comme unaccent(lower()) en SQL)."""
    import unicodedata

    decomposed = unicodedata.normalize("NFKD", str(text).lower())
    return "".join(c for c in decomposed if not unicodedata.combining(c)).strip()


def word_pattern(value: str) -> str:
    """Expression régulière : la valeur comme mot(s) entier(s) — « gruwe » ne trouve pas « gruwel »."""
    return r"\m" + re.escape(normalize(value)) + r"\M"


def _group_matches(rows, limit: int) -> list[Match]:
    out: dict[tuple[str, str], Match] = {}
    for source, doc_id, path, doc_type, label, key, val, check in rows:
        m = out.get((source, doc_id))
        if m is None:
            if len(out) >= limit:
                break
            m = Match(source, doc_id, path, doc_type, label)
        out[(source, doc_id)] = Match(
            m.source, m.doc_id, m.path, m.doc_type, m.label, (*m.fields, (key, val or "", bool(check)))
        )
    return list(out.values())


def _around(text: str, value: str, width: int = 120) -> str:
    """Passage du texte autour de la mention."""
    norm = normalize(text)
    hit = re.search(re.escape(normalize(value)), norm)
    if not hit:
        return text[: 2 * width]
    start, end = max(0, hit.start() - width), min(len(text), hit.end() + width)
    return ("…" if start else "") + text[start:end].replace("\n", " ") + ("…" if end < len(text) else "")


def _vec_literal(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.7g}" for x in v) + "]"


class PgStore:
    def __init__(self, dsn: str, dim: int, *, pool_size: int = 10):
        self.dsn = dsn
        self.dim = dim
        self.pool_size = pool_size
        self._pool = None
        self._pool_lock = threading.Lock()

    def _get_pool(self):
        """Pool de connexions ouvert à la première utilisation.

        Ouvrir une connexion coûte cher (de quelques ms en local à plusieurs centaines via le
        réseau de Docker Desktop) et une page de la console enchaîne plusieurs requêtes.
        """
        if self._pool is None:
            with self._pool_lock:
                if self._pool is None:
                    from psycopg_pool import ConnectionPool

                    pool = ConnectionPool(
                        self.dsn, min_size=1, max_size=self.pool_size, open=False,
                        kwargs={"connect_timeout": 10}, timeout=15, name="alambic-search",
                    )
                    # échoue vite (et clairement) si la base est injoignable
                    pool.open(wait=True, timeout=12)
                    self._pool = pool
        return self._pool

    @contextmanager
    def _conn(self) -> Iterator:
        # commit à la sortie, rollback sur exception (comportement du pool)
        with self._get_pool().connection() as conn:
            yield conn

    def close(self) -> None:
        if self._pool is not None:
            self._pool.close()
            self._pool = None

    def init(self) -> None:
        with self._conn() as conn:
            conn.execute(SCHEMA.format(dim=int(self.dim)))

    def get_state(self, source: str) -> dict[str, DocState]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT doc_id, path, mtime, size, content_hash, acl_groups, acl_deny FROM documents "
                "WHERE source = %s AND (status <> %s OR content_hash <> '')",
                # erreurs passagères (sans empreinte) retentées à chaque passage ;
                # refus définitif d'un contenu (avec empreinte) : retenté s'il change
                (source, Status.ERROR),
            ).fetchall()
        return {r[0]: DocState(r[1], r[2], r[3], r[4], Acl(frozenset(r[5]), frozenset(r[6]))) for r in rows}

    def get_document(self, source: str, doc_id: str) -> DocState | None:
        with self._conn() as conn:
            r = conn.execute(
                "SELECT path, mtime, size, content_hash, acl_groups, acl_deny FROM documents "
                "WHERE source=%s AND doc_id=%s",
                (source, doc_id),
            ).fetchone()
        return DocState(r[0], r[1], r[2], r[3], Acl(frozenset(r[4]), frozenset(r[5]))) if r else None

    def upsert_document(self, ref, content_hash, acl, status, chunks, vectors, error=None, meta=None) -> None:
        if len(chunks) != len(vectors):
            raise ValueError("chunks et vectors doivent avoir la même longueur")
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO documents (source, doc_id, path, mtime, size, content_hash, acl_groups, acl_deny,
                                       status, error, doc_type, doc_label, doc_fields, fields_to_check,
                                       doc_confidence, light_uuid, light_status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (source, doc_id) DO UPDATE SET
                    path = EXCLUDED.path, mtime = EXCLUDED.mtime, size = EXCLUDED.size,
                    content_hash = EXCLUDED.content_hash, acl_groups = EXCLUDED.acl_groups,
                    acl_deny = EXCLUDED.acl_deny, status = EXCLUDED.status, error = EXCLUDED.error,
                    doc_type = EXCLUDED.doc_type, doc_label = EXCLUDED.doc_label,
                    doc_fields = EXCLUDED.doc_fields, fields_to_check = EXCLUDED.fields_to_check,
                    doc_confidence = EXCLUDED.doc_confidence, light_uuid = EXCLUDED.light_uuid,
                    light_status = EXCLUDED.light_status, indexed_at = now()
                """,
                (ref.source, ref.doc_id, ref.path, ref.mtime, ref.size, content_hash,
                 sorted(acl.allow), sorted(acl.deny), status, error, *_meta_params(meta)),
            )
            conn.execute("DELETE FROM chunks WHERE source = %s AND doc_id = %s", (ref.source, ref.doc_id))
            if chunks:
                with conn.cursor() as cur:
                    cur.executemany(
                        "INSERT INTO chunks (source, doc_id, chunk_no, page, text, embedding) "
                        "VALUES (%s, %s, %s, %s, %s, %s::vector)",
                        [
                            (ref.source, ref.doc_id, c.chunk_no, c.page, c.text, _vec_literal(v))
                            for c, v in zip(chunks, vectors, strict=True)
                        ],
                    )

    def update_metadata(self, ref, acl) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE documents SET path=%s, mtime=%s, size=%s, acl_groups=%s, acl_deny=%s "
                "WHERE source=%s AND doc_id=%s",
                (ref.path, ref.mtime, ref.size, sorted(acl.allow), sorted(acl.deny), ref.source, ref.doc_id),
            )

    def delete_document(self, source, doc_id) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM documents WHERE source = %s AND doc_id = %s", (source, doc_id))

    def search(self, query, vector, groups, k=8, doc_types=None) -> list[Hit]:
        if not groups:
            return []
        with self._conn() as conn:
            rows = conn.execute(
                HYBRID_SEARCH,
                {"q": query, "v": _vec_literal(vector), "groups": sorted(groups), "pool": k * 5, "k": k, "rrf": RRF_K,
                 "types": sorted(doc_types) if doc_types else None},
            ).fetchall()
        return [Hit(*r) for r in rows]

    # --- Alambic Light -------------------------------------------------------------

    def pending_reviews(self, source, limit=200):
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT d.doc_id, {_META_COLS} FROM documents d WHERE d.source = %s AND d.light_uuid IS NOT NULL "
                "AND cardinality(d.fields_to_check) > 0 AND d.light_status NOT IN ('VALIDE', 'EXPIRE', 'EFFACE') "
                "ORDER BY d.indexed_at LIMIT %s",
                (source, limit),
            ).fetchall()
        return [(r[0], _meta_from(r[1:])) for r in rows]

    def update_meta(self, source, doc_id, meta, fiche, vector):
        from ged.alambic_light import FICHE_CHUNK_NO

        with self._conn() as conn:
            conn.execute(
                "UPDATE documents SET doc_type=%s, doc_label=%s, doc_fields=%s, fields_to_check=%s, "
                "doc_confidence=%s, light_uuid=%s, light_status=%s WHERE source=%s AND doc_id=%s",
                (*_meta_params(meta), source, doc_id),
            )
            conn.execute("DELETE FROM chunks WHERE source=%s AND doc_id=%s AND chunk_no=%s",
                         (source, doc_id, FICHE_CHUNK_NO))
            if fiche and vector is not None:
                conn.execute(
                    "INSERT INTO chunks (source, doc_id, chunk_no, page, text, embedding) "
                    "VALUES (%s, %s, %s, 1, %s, %s::vector)",
                    (source, doc_id, FICHE_CHUNK_NO, fiche, _vec_literal(vector)),
                )

    def strip_light(self, source):
        """Source passée en « texte seul » : retire type, données et fiche. Un scan qui n'avait
        que sa fiche est repris à la synchro (mtime -1 : relu, OCR seul)."""
        from ged.alambic_light import FICHE_CHUNK_NO

        with self._conn() as conn:
            n = conn.execute(
                "UPDATE documents SET doc_type=NULL, doc_label=NULL, doc_fields=NULL, fields_to_check='{}', "
                "doc_confidence=NULL, light_uuid=NULL, light_status=NULL "
                "WHERE source=%s AND (doc_type IS NOT NULL OR light_uuid IS NOT NULL)", (source,)).rowcount
            conn.execute("DELETE FROM chunks WHERE source=%s AND chunk_no=%s", (source, FICHE_CHUNK_NO))
            conn.execute(
                "UPDATE documents d SET mtime=-1, content_hash='' WHERE d.source=%s AND d.status=%s "
                "AND NOT EXISTS (SELECT 1 FROM chunks c WHERE c.source=d.source AND c.doc_id=d.doc_id)",
                (source, Status.INDEXED))
        return n

    def sample_chunks(self, n, source=None):
        """Passages tirés au hasard (un par document au plus), pour générer des questions de test."""
        from ged.alambic_light import FICHE_CHUNK_NO

        sql = f"""
            SELECT * FROM (
                SELECT DISTINCT ON (c.source, c.doc_id) c.source, c.doc_id, d.path, c.page, c.text, d.acl_groups
                FROM chunks c JOIN documents d ON d.source = c.source AND d.doc_id = c.doc_id
                WHERE d.status = 'indexed' AND c.chunk_no <> {FICHE_CHUNK_NO} AND length(c.text) >= 200
                  AND (%(src)s::text IS NULL OR c.source = %(src)s)
                ORDER BY c.source, c.doc_id, random()
            ) t ORDER BY random() LIMIT %(n)s"""
        with self._conn() as conn:
            rows = conn.execute(sql, {"src": source, "n": n}).fetchall()
        return [{"source": r[0], "doc_id": r[1], "path": r[2], "page": r[3], "text": r[4], "groups": list(r[5])}
                for r in rows]

    def mark_for_analysis(self, source):
        """Analyse réactivée : documents jamais analysés repris à la synchro suivante."""
        with self._conn() as conn:
            return conn.execute(
                "UPDATE documents SET mtime=-1, content_hash='' WHERE source=%s AND light_uuid IS NULL "
                "AND status IN (%s, %s)", (source, Status.INDEXED, Status.OCR_REQUIRED)).rowcount

    def type_counts(self):
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT doc_type, max(doc_label), count(*) FROM documents WHERE doc_type IS NOT NULL "
                "GROUP BY doc_type ORDER BY count(*) DESC"
            ).fetchall()
        return [(r[0], r[1] or r[0], r[2]) for r in rows]

    # --- Recherche par critères --------------------------------------------------------

    _ACL = "(%(all)s OR (d.acl_groups && %(g)s::text[] AND NOT d.acl_deny && %(g)s::text[]))"

    @staticmethod
    def _criteria(groups, value, doc_type, field=None) -> dict:
        return {"all": groups is None, "g": sorted(groups or ()), "re": word_pattern(value),
                "type": doc_type, "field": field}

    def find_by_fields(self, groups, value, doc_type=None, field=None, limit=500):
        if groups is not None and not groups or not normalize(value):
            return []
        sql = f"""
            SELECT d.source, d.doc_id, d.path, d.doc_type, d.doc_label, f.key,
                   coalesce(f.value->'valeur', f.value) #>> '{{}}', f.key = ANY(d.fields_to_check)
            FROM documents d CROSS JOIN LATERAL jsonb_each(d.doc_fields) f
            WHERE jsonb_typeof(d.doc_fields) = 'object' AND {self._ACL}
              AND (%(type)s::text IS NULL OR d.doc_type = %(type)s)
              AND (%(field)s::text IS NULL OR lower(f.key) = lower(%(field)s))
              AND unaccent(lower(coalesce(f.value->'valeur', f.value) #>> '{{}}')) ~ %(re)s
            ORDER BY d.path, d.source, d.doc_id, f.key
        """
        with self._conn() as conn:
            rows = conn.execute(sql, self._criteria(groups, value, doc_type, field)).fetchall()
        return _group_matches(rows, limit)

    def find_mentions(self, groups, value, doc_type=None, limit=200):
        if groups is not None and not groups or not normalize(value):
            return []
        from ged.alambic_light import FICHE_CHUNK_NO

        sql = f"""
            SELECT * FROM (
                SELECT DISTINCT ON (d.source, d.doc_id) d.source, d.doc_id, d.path, d.doc_type, d.doc_label,
                       c.page, c.text
                FROM documents d JOIN chunks c ON c.source = d.source AND c.doc_id = d.doc_id
                WHERE d.status = 'indexed' AND {self._ACL} AND c.chunk_no <> {FICHE_CHUNK_NO}
                  -- type demandé, ou document non typé (une facture non reconnue reste une facture)
                  AND (%(type)s::text IS NULL OR d.doc_type = %(type)s OR d.doc_type IS NULL)
                  AND unaccent(lower(c.text)) ~ %(re)s
                ORDER BY d.source, d.doc_id, c.chunk_no
            ) m ORDER BY path LIMIT %(limit)s
        """
        with self._conn() as conn:
            rows = conn.execute(sql, {**self._criteria(groups, value, doc_type), "limit": limit}).fetchall()
        return [Match(r[0], r[1], r[2], r[3], r[4], (), r[5], _around(r[6], value)) for r in rows]

    def list_source_configs(self):
        with self._conn() as conn:
            rows = conn.execute("SELECT config FROM source_configs ORDER BY name").fetchall()
        return [r[0] for r in rows]

    def save_source_config(self, config):
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO source_configs (name, config) VALUES (%s, %s) ON CONFLICT (name) DO UPDATE "
                "SET config = EXCLUDED.config, updated_at = now()",
                (config["name"], json.dumps(config, ensure_ascii=False)),
            )

    def delete_source_config(self, name):
        with self._conn() as conn:
            conn.execute("DELETE FROM source_configs WHERE name = %s", (name,))
            conn.execute("DELETE FROM source_secrets WHERE source = %s", (name,))

    def get_source_secrets(self, source):
        with self._conn() as conn:
            rows = conn.execute("SELECT field, value FROM source_secrets WHERE source = %s", (source,)).fetchall()
        return {r[0]: r[1] for r in rows}

    def set_source_secret(self, source, field, token):
        with self._conn() as conn:
            if token is None:
                conn.execute("DELETE FROM source_secrets WHERE source = %s AND field = %s", (source, field))
            else:
                conn.execute(
                    "INSERT INTO source_secrets (source, field, value) VALUES (%s, %s, %s) "
                    "ON CONFLICT (source, field) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
                    (source, field, token),
                )

    def list_all_source_secrets(self):
        with self._conn() as conn:
            rows = conn.execute("SELECT source, field, value FROM source_secrets ORDER BY source, field").fetchall()
        return [tuple(r) for r in rows]

    def get_setting(self, key):
        with self._conn() as conn:
            row = conn.execute("SELECT value FROM app_settings WHERE key = %s", (key,)).fetchone()
        return row[0] if row else None

    def set_setting(self, key, value):
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO app_settings (key, value) VALUES (%s, %s) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                (key, json.dumps(value, ensure_ascii=False)),
            )

    def purge_source(self, name):
        """Retire de l'index tout ce qui vient de la source (les fichiers ne sont pas touchés)."""
        with self._conn() as conn:
            n = conn.execute("DELETE FROM documents WHERE source = %s", (name,)).rowcount
            conn.execute("DELETE FROM sync_runs WHERE source = %s", (name,))
        return n

    def field_names(self, doc_type=None):
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT f.key FROM documents d CROSS JOIN LATERAL jsonb_each(d.doc_fields) f "
                "WHERE jsonb_typeof(d.doc_fields) = 'object' AND (%(t)s::text IS NULL OR d.doc_type = %(t)s) "
                "ORDER BY 1", {"t": doc_type},
            ).fetchall()
        return [r[0] for r in rows]

    # --- Console d'administration --------------------------------------------------

    _DOC_COLS = """d.source, d.doc_id, d.path, d.status, d.size, d.acl_groups,
        (SELECT count(*) FROM chunks c WHERE c.source = d.source AND c.doc_id = d.doc_id),
        extract(epoch FROM d.indexed_at), d.error, d.acl_deny, """ + _META_COLS

    @staticmethod
    def _doc_row(r) -> DocRow:
        return DocRow(r[0], r[1], r[2], r[3], r[4], Acl(frozenset(r[5]), frozenset(r[9])), r[6], float(r[7]), r[8],
                      _meta_from(r[10:]))

    def stats(self) -> dict[str, Counter]:
        with self._conn() as conn:
            rows = conn.execute("SELECT source, status, count(*) FROM documents GROUP BY source, status").fetchall()
        out: dict[str, Counter] = {}
        for source, status, n in rows:
            out.setdefault(source, Counter())[status] = n
        return out

    def list_documents(self, source=None, status=None, q=None, offset=0, limit=50, doc_type=None):
        where, params = [], []
        if doc_type:
            where.append("d.doc_type = %s")
            params.append(doc_type)
        if source:
            where.append("d.source = %s")
            params.append(source)
        if status:
            where.append("d.status = %s")
            params.append(status)
        if q:
            where.append("d.path ILIKE %s")
            params.append(f"%{q}%")
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        with self._conn() as conn:
            total = conn.execute(f"SELECT count(*) FROM documents d {clause}", params).fetchone()[0]
            rows = conn.execute(
                f"SELECT {self._DOC_COLS} FROM documents d {clause} ORDER BY d.path LIMIT %s OFFSET %s",
                [*params, limit, offset],
            ).fetchall()
        return [self._doc_row(r) for r in rows], total

    def document_row(self, source, doc_id):
        with self._conn() as conn:
            r = conn.execute(
                f"SELECT {self._DOC_COLS} FROM documents d WHERE d.source = %s AND d.doc_id = %s", (source, doc_id)
            ).fetchone()
        return self._doc_row(r) if r else None

    def document_chunks(self, source, doc_id, limit=20):
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT chunk_no, page, text FROM chunks WHERE source = %s AND doc_id = %s ORDER BY chunk_no LIMIT %s",
                (source, doc_id, limit),
            ).fetchall()
        return [Chunk(*r) for r in rows]

    def start_run(self, source):
        with self._conn() as conn:
            return conn.execute(
                "INSERT INTO sync_runs (source, status) VALUES (%s, %s) RETURNING id", (source, RunStatus.RUNNING)
            ).fetchone()[0]

    def finish_run(self, run_id, status, report=None, message=None):
        with self._conn() as conn:
            conn.execute(
                "UPDATE sync_runs SET finished_at = now(), status = %s, report = %s, message = %s WHERE id = %s",
                (status, json.dumps(report) if report is not None else None, message, run_id),
            )

    def list_runs(self, source=None, limit=20):
        sql = ("SELECT id, source, extract(epoch FROM started_at), extract(epoch FROM finished_at), "
               "status, report, message FROM sync_runs")
        params: list = []
        if source:
            sql += " WHERE source = %s"
            params.append(source)
        sql += " ORDER BY started_at DESC, id DESC LIMIT %s"
        params.append(limit)
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            SyncRun(r[0], r[1], float(r[2]), float(r[3]) if r[3] is not None else None, r[4], r[5], r[6])
            for r in rows
        ]

    def interrupt_stale_runs(self):
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE sync_runs SET status = %s, finished_at = now(), message = %s WHERE status = %s",
                (RunStatus.INTERRUPTED, "Service arrêté pendant la synchronisation", RunStatus.RUNNING),
            )
            return cur.rowcount

    def log_question(self, groups, question, hits, citations, duration_ms, sandbox=False):
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO question_log (groups, question, hits, citations, duration_ms, sandbox) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (sorted(groups), question, hits, citations, duration_ms, sandbox),
            )

    def question_activity(self, days=14):
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT to_char(at, 'YYYY-MM-DD'), count(*), count(*) FILTER (WHERE citations = 0) "
                "FROM question_log WHERE NOT sandbox AND at >= current_date - %s::int + 1 GROUP BY 1",
                (days,),
            ).fetchall()
        by_day = {r[0]: (r[1], r[2]) for r in rows}
        return [(d, *by_day.get(d, (0, 0))) for d in _last_days(days)]

    def purge_questions(self, older_than_days):
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM question_log WHERE at < now() - %s * interval '1 day'", (older_than_days,))
            return cur.rowcount


# --------------------------------------------------------------------------------------
# En mémoire (tests)
# --------------------------------------------------------------------------------------

_WORD = re.compile(r"\w+", re.UNICODE)
_STOP = frozenset("le la les de des du un une et en au aux a à est que qui quoi pour par sur dans avec ce se".split())


def _terms(text: str) -> list[str]:
    return [t for t in _WORD.findall(text.lower()) if len(t) > 1 and t not in _STOP]


class MemoryStore:
    def __init__(self) -> None:
        self.docs: dict[tuple[str, str], dict] = {}
        self.chunks: dict[tuple[str, str], list[tuple[Chunk, list[float]]]] = {}
        self.runs: list[SyncRun] = []
        self.questions: list[dict] = []
        self.source_configs: dict[str, dict] = {}
        self.source_secrets: dict[tuple[str, str], str] = {}
        self.settings: dict = {}
        self._lock = threading.Lock()  # les synchros tournent dans des threads

    def init(self) -> None:
        pass

    def get_state(self, source):
        return {
            doc_id: d["state"]
            for (src, doc_id), d in self.docs.items()
            if src == source and (d["status"] != Status.ERROR or d["state"].content_hash)
        }

    def get_document(self, source, doc_id):
        d = self.docs.get((source, doc_id))
        return d["state"] if d else None

    def upsert_document(self, ref, content_hash, acl, status, chunks, vectors, error=None, meta=None):
        if len(chunks) != len(vectors):
            raise ValueError("chunks et vectors doivent avoir la même longueur")
        key = (ref.source, ref.doc_id)
        self.docs[key] = {
            "state": DocState(ref.path, ref.mtime, ref.size, content_hash, acl),
            "status": status,
            "error": error,
            "indexed_at": time.time(),
            "meta": meta,
        }
        self.chunks[key] = list(zip(chunks, vectors, strict=True))

    def pending_reviews(self, source, limit=200):
        out = [
            (doc_id, d["meta"]) for (src, doc_id), d in list(self.docs.items())
            if src == source and d.get("meta") and d["meta"].light_uuid and d["meta"].to_check
            and d["meta"].light_status not in ("VALIDE", "EXPIRE", "EFFACE")
        ]
        return out[:limit]

    def update_meta(self, source, doc_id, meta, fiche, vector):
        from ged.alambic_light import FICHE_CHUNK_NO

        key = (source, doc_id)
        self.docs[key]["meta"] = meta
        rest = [(c, v) for c, v in self.chunks.get(key, []) if c.chunk_no != FICHE_CHUNK_NO]
        if fiche and vector is not None:
            rest.insert(0, (Chunk(FICHE_CHUNK_NO, 1, fiche), vector))
        self.chunks[key] = rest

    def _reset(self, key) -> None:
        st = self.docs[key]["state"]
        self.docs[key]["state"] = DocState(st.path, -1, st.size, "", st.acl)

    def strip_light(self, source):
        from ged.alambic_light import FICHE_CHUNK_NO

        n = 0
        for key, d in self.docs.items():
            if key[0] != source:
                continue
            if d.get("meta") is not None:
                d["meta"] = None
                n += 1
            self.chunks[key] = [(c, v) for c, v in self.chunks.get(key, []) if c.chunk_no != FICHE_CHUNK_NO]
            if d["status"] == Status.INDEXED and not self.chunks[key]:
                self._reset(key)
        return n

    def sample_chunks(self, n, source=None):
        import random

        from ged.alambic_light import FICHE_CHUNK_NO

        out = []
        for (src, doc_id), d in self.docs.items():
            if d["status"] != Status.INDEXED or (source and src != source):
                continue
            chunks = [c for c, _ in self.chunks.get((src, doc_id), []) if c.chunk_no != FICHE_CHUNK_NO
                      and len(c.text) >= 200]
            if chunks:
                c = random.choice(chunks)
                out.append({"source": src, "doc_id": doc_id, "path": d["state"].path, "page": c.page, "text": c.text,
                            "groups": sorted(d["state"].acl.allow)})
        random.shuffle(out)
        return out[:n]

    def mark_for_analysis(self, source):
        n = 0
        for key, d in self.docs.items():
            if key[0] == source and not (d.get("meta") and d["meta"].light_uuid) \
                    and d["status"] in (Status.INDEXED, Status.OCR_REQUIRED):
                self._reset(key)
                n += 1
        return n

    def _visible(self, d, groups) -> bool:
        return groups is None or d["state"].acl.permits(groups)

    def find_by_fields(self, groups, value, doc_type=None, field=None, limit=500):
        if groups is not None and not groups or not normalize(value):
            return []
        pattern = re.compile(r"\b" + re.escape(normalize(value)) + r"\b")
        out = []
        for (src, doc_id), d in sorted(self.docs.items(), key=lambda kv: kv[1]["state"].path):
            m = d.get("meta")
            if not m or not m.fields or not self._visible(d, groups) or (doc_type and m.doc_type != doc_type):
                continue
            found = []
            for key, raw in sorted(m.fields.items()):
                if field and key.lower() != field.lower():
                    continue
                v = raw.get("valeur", raw) if isinstance(raw, dict) else raw
                text = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
                if pattern.search(normalize(text)):
                    found.append((key, text, key in m.to_check))
            if found:
                out.append(Match(src, doc_id, d["state"].path, m.doc_type, m.label, tuple(found)))
                if len(out) >= limit:
                    break
        return out

    def find_mentions(self, groups, value, doc_type=None, limit=200):
        if groups is not None and not groups or not normalize(value):
            return []
        from ged.alambic_light import FICHE_CHUNK_NO

        pattern = re.compile(r"\b" + re.escape(normalize(value)) + r"\b")
        out = []
        for (src, doc_id), d in sorted(self.docs.items(), key=lambda kv: kv[1]["state"].path):
            m = d.get("meta")
            if d["status"] != Status.INDEXED or not self._visible(d, groups):
                continue
            if doc_type and m and m.doc_type and m.doc_type != doc_type:
                continue
            for c, _ in sorted(self.chunks.get((src, doc_id), []), key=lambda cv: cv[0].chunk_no):
                if c.chunk_no != FICHE_CHUNK_NO and pattern.search(normalize(c.text)):
                    out.append(Match(src, doc_id, d["state"].path, m.doc_type if m else None,
                                     m.label if m else None, (), c.page, _around(c.text, value)))
                    break
            if len(out) >= limit:
                break
        return out

    def list_source_configs(self):
        return [dict(c) for _, c in sorted(self.source_configs.items())]

    def save_source_config(self, config):
        self.source_configs[config["name"]] = dict(config)

    def delete_source_config(self, name):
        self.source_configs.pop(name, None)
        self.source_secrets = {k: v for k, v in self.source_secrets.items() if k[0] != name}

    def get_source_secrets(self, source):
        return {f: v for (s, f), v in self.source_secrets.items() if s == source}

    def set_source_secret(self, source, field, token):
        if token is None:
            self.source_secrets.pop((source, field), None)
        else:
            self.source_secrets[(source, field)] = token

    def list_all_source_secrets(self):
        return [(s, f, v) for (s, f), v in sorted(self.source_secrets.items())]

    def get_setting(self, key):
        return self.settings.get(key)

    def set_setting(self, key, value):
        self.settings[key] = value

    def purge_source(self, name):
        keys = [k for k in self.docs if k[0] == name]
        for k in keys:
            self.delete_document(*k)
        with self._lock:  # les id des passages sont leur position : on les rend orphelins sans les retirer
            self.runs = [dataclasses.replace(r, source="") if r.source == name else r for r in self.runs]
        return len(keys)

    def field_names(self, doc_type=None):
        return sorted({
            k for d in list(self.docs.values()) if (m := d.get("meta")) and (not doc_type or m.doc_type == doc_type)
            for k in m.fields
        })

    def type_counts(self):
        counts: Counter = Counter()
        labels = {}
        for d in list(self.docs.values()):
            m = d.get("meta")
            if m and m.doc_type:
                counts[m.doc_type] += 1
                labels[m.doc_type] = m.label or m.doc_type
        return [(t, labels[t], n) for t, n in counts.most_common()]

    def update_metadata(self, ref, acl):
        d = self.docs[(ref.source, ref.doc_id)]
        d["state"] = DocState(ref.path, ref.mtime, ref.size, d["state"].content_hash, acl)

    def delete_document(self, source, doc_id):
        self.docs.pop((source, doc_id), None)
        self.chunks.pop((source, doc_id), None)

    def search(self, query, vector, groups, k=8, doc_types=None):
        if not groups:
            return []
        q_terms = set(_terms(query))
        candidates = []
        for key, d in self.docs.items():
            if d["status"] != Status.INDEXED or not d["state"].acl.permits(groups):
                continue
            meta = d.get("meta")
            if doc_types and not (meta and meta.doc_type in doc_types):
                continue
            for chunk, vec in self.chunks.get(key, []):
                tf = sum(1 for t in _terms(chunk.text) if t in q_terms)
                cos = sum(a * b for a, b in zip(vector, vec, strict=True)) / (
                    (math.sqrt(sum(a * a for a in vector)) or 1) * (math.sqrt(sum(b * b for b in vec)) or 1)
                )
                candidates.append((key, d, chunk, tf, cos))
        pool = k * 5
        fts_rank = {
            id(c): r
            for r, c in enumerate(sorted((c for c in candidates if c[3] > 0), key=lambda c: -c[3])[:pool], 1)
        }
        vec_rank = {id(c): r for r, c in enumerate(sorted(candidates, key=lambda c: -c[4])[:pool], 1)}
        scored = []
        for c in candidates:
            s = sum(1.0 / (RRF_K + rank[id(c)]) for rank in (fts_rank, vec_rank) if id(c) in rank)
            if s > 0:
                (src, doc_id), d, chunk, _, _ = c
                label = d["meta"].label if d.get("meta") else None
                scored.append(Hit(src, doc_id, d["state"].path, chunk.page, chunk.text, d["state"].content_hash, s,
                                  label))
        scored.sort(key=lambda h: -h.score)
        return scored[:k]

    # --- Console d'administration --------------------------------------------------

    def _row(self, key, d) -> DocRow:
        s = d["state"]
        return DocRow(key[0], key[1], s.path, d["status"], s.size, s.acl, len(self.chunks.get(key, [])),
                      d["indexed_at"], d["error"], d.get("meta"))

    def stats(self):
        out: dict[str, Counter] = {}
        for (src, _), d in list(self.docs.items()):
            out.setdefault(src, Counter())[d["status"]] += 1
        return out

    def list_documents(self, source=None, status=None, q=None, offset=0, limit=50, doc_type=None):
        rows = [
            self._row(k, d) for k, d in list(self.docs.items())
            if (not source or k[0] == source)
            and (not doc_type or (d.get("meta") and d["meta"].doc_type == doc_type))
            and (not status or d["status"] == status)
            and (not q or q.lower() in d["state"].path.lower())
        ]
        rows.sort(key=lambda r: r.path)
        return rows[offset : offset + limit], len(rows)

    def document_row(self, source, doc_id):
        d = self.docs.get((source, doc_id))
        return self._row((source, doc_id), d) if d else None

    def document_chunks(self, source, doc_id, limit=20):
        return sorted((c for c, _ in self.chunks.get((source, doc_id), [])), key=lambda c: c.chunk_no)[:limit]

    def start_run(self, source):
        with self._lock:
            run_id = len(self.runs) + 1
            self.runs.append(SyncRun(run_id, source, time.time(), None, RunStatus.RUNNING, None, None))
            return run_id

    def finish_run(self, run_id, status, report=None, message=None):
        with self._lock:
            r = self.runs[run_id - 1]
            self.runs[run_id - 1] = SyncRun(r.id, r.source, r.started_at, time.time(), status, report, message)

    def list_runs(self, source=None, limit=20):
        runs = [r for r in reversed(self.runs) if not source or r.source == source]
        return runs[:limit]

    def interrupt_stale_runs(self):
        n = 0
        for r in list(self.runs):
            if r.status == RunStatus.RUNNING:
                self.finish_run(r.id, RunStatus.INTERRUPTED, message="Service arrêté pendant la synchronisation")
                n += 1
        return n

    def log_question(self, groups, question, hits, citations, duration_ms, sandbox=False):
        self.questions.append({"at": time.time(), "groups": sorted(groups), "question": question, "hits": hits,
                               "citations": citations, "duration_ms": duration_ms, "sandbox": sandbox})

    def question_activity(self, days=14):
        totals: Counter = Counter()
        unanswered: Counter = Counter()
        for q in self.questions:
            if q["sandbox"]:
                continue
            day = _day(q["at"])
            totals[day] += 1
            unanswered[day] += q["citations"] == 0
        return [(d, totals[d], unanswered[d]) for d in _last_days(days)]

    def purge_questions(self, older_than_days):
        limit = time.time() - older_than_days * 86400
        before = len(self.questions)
        self.questions = [q for q in self.questions if q["at"] >= limit]
        return before - len(self.questions)
