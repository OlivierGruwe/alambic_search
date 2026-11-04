"""Modèles de données partagés par les connecteurs, l'index et le moteur de questions."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Acl:
    """Droits de lecture d'un document : principaux autorisés et refusés.

    Un principal est un nom de groupe (règles par dossier) ou un SID Windows (ACL NTFS).
    Comme sous Windows, un refus explicite l'emporte sur toute autorisation.
    """

    allow: frozenset[str] = frozenset()
    deny: frozenset[str] = frozenset()

    def permits(self, principals: frozenset[str]) -> bool:
        return bool(self.allow & principals) and not (self.deny & principals)


@dataclass(frozen=True)
class DocumentRef:
    """Un document tel que vu dans sa source. Le fichier reste dans la source."""

    source: str  # nom de la source configurée (ex. "partage-commun")
    doc_id: str  # identifiant STABLE dans la source (pas le chemin : survit aux renommages)
    path: str  # emplacement lisible, relatif à la source
    mtime: float
    size: int


@dataclass(frozen=True)
class Page:
    number: int  # 1-based
    text: str


@dataclass(frozen=True)
class Chunk:
    chunk_no: int
    page: int
    text: str


@dataclass(frozen=True)
class DocState:
    """Ce que l'index sait d'un document, pour calculer le delta."""

    path: str
    mtime: float
    size: int
    content_hash: str
    acl: Acl


@dataclass(frozen=True)
class DocMeta:
    """Ce qu'Alambic Light a reconnu dans un document : type et données extraites.

    `to_check` : champs dont la valeur est douteuse (à vérifier par un gestionnaire dans
    Alambic Light). Ils restent affichés et signalés comme tels, jamais présentés comme sûrs.
    """

    doc_type: str | None  # code du référentiel Alambic (ex. FACTURE), None si non reconnu
    label: str | None  # libellé lisible (ex. « Facture »)
    fields: dict = field(default_factory=dict)
    to_check: tuple[str, ...] = ()
    light_uuid: str | None = None
    light_status: str | None = None  # RECONNU, NON_RECONNU, VALIDE…
    confidence: float | None = None

    @property
    def validated(self) -> bool:
        return self.light_status == "VALIDE"


@dataclass(frozen=True)
class Hit:
    source: str
    doc_id: str
    path: str
    page: int
    text: str
    content_hash: str
    score: float
    doc_label: str | None = None  # type du document (Alambic Light), pour le contexte du LLM


@dataclass
class Citation:
    n: int
    source: str
    doc_id: str
    path: str
    page: int
    url: str


@dataclass
class DocResult:
    """Un document retrouvé (mode « un résultat par document ») : ses passages pertinents regroupés."""

    n: int
    source: str
    doc_id: str
    path: str
    label: str | None
    url: str  # page du meilleur passage
    hits: list[Hit]  # passages du document, du plus pertinent au moins pertinent
    pages: list[tuple[int, str]]  # (page, lien) des pages où la recherche a trouvé quelque chose

    @property
    def best(self) -> Hit:
        return self.hits[0]


@dataclass
class Answer:
    text: str
    citations: list[Citation] = field(default_factory=list)
    hits: list[Hit] = field(default_factory=list)  # extraits retrouvés (envoyés au LLM s'il formule)
    generated: bool = True  # False : pas de formulation, les extraits sont la réponse
    criteria: object | None = None  # CriteriaResult : question traduite en liste par critères
    documents: list[DocResult] | None = None  # mode « un résultat par document » : [n] = document n
    identifiers: tuple[str, ...] = ()  # identifiants de la question : seuls les documents qui les contiennent
    identifiers_absent: tuple[str, ...] = ()  # identifiants trouvés dans aucun document : résultats par ressemblance
    reformulated: str | None = None  # question de suivi rendue autonome (conversation)


@dataclass(frozen=True)
class Match:
    """Document retrouvé par une recherche par critères (liste exhaustive, pas un classement).

    `fields` : champs extraits dont la valeur correspond, (champ, valeur, à vérifier).
    `excerpt` : pour une simple mention dans le texte (aucun champ ne correspond).
    """

    source: str
    doc_id: str
    path: str
    doc_type: str | None
    label: str | None
    fields: tuple[tuple[str, str, bool], ...] = ()
    page: int | None = None
    excerpt: str | None = None

    @property
    def doubtful(self) -> bool:
        return bool(self.fields) and all(check for _, _, check in self.fields)


@dataclass(frozen=True)
class DocRow:
    """Un document vu depuis la console d'administration."""

    source: str
    doc_id: str
    path: str
    status: str
    size: int
    acl: Acl
    chunks: int
    indexed_at: float  # epoch
    error: str | None
    meta: DocMeta | None = None


@dataclass(frozen=True)
class SyncRun:
    id: int
    source: str
    started_at: float
    finished_at: float | None
    status: str  # running | ok | partial | blocked | failed | interrupted
    report: dict | None
    message: str | None


class RunStatus:
    RUNNING = "running"
    OK = "ok"
    PARTIAL = "partial"  # terminé avec des documents en erreur
    BLOCKED = "blocked"  # garde-fou de suppression massive
    FAILED = "failed"
    INTERRUPTED = "interrupted"  # service arrêté pendant la synchro


class Status:
    INDEXED = "indexed"
    OCR_REQUIRED = "ocr_required"  # aucun texte extractible et pas d'Alambic Light configuré
    UNSUPPORTED = "unsupported"
    ERROR = "error"
