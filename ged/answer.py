"""Question -> recherche hybride filtrée par droits -> (LLM) -> réponse avec citations.

La formulation par le LLM est optionnelle : sans LLM configuré, ou si la question le demande
(`generate=False`), la réponse est la liste des extraits les plus pertinents, chacun cité.
Pas de modèle à héberger, pas de reformulation à vérifier, réponse en quelques dizaines de ms.
Une question de liste passe quand même par la traduction en critères si un LLM est configuré.

Question de liste (« toutes les factures de M. GRUWE ») : le LLM la traduit en critères
(type, champ, valeur), contrôlés, puis la liste vient de la recherche par critères (SQL,
exhaustive). Le LLM ne fait que comprendre la question : il ne choisit pas les documents."""
from __future__ import annotations

import logging
import re

from ged.embeddings import Embedder
from ged.llm import LLM
from ged.models import Answer, Citation, DocResult, Hit
from ged.store import Store, normalize

log = logging.getLogger("alambic_search.answer")

SYSTEM_PROMPT = """Tu es l'assistant documentaire de l'entreprise.
Règles :
- Réponds UNIQUEMENT à partir des extraits fournis, jamais de connaissances extérieures.
- Après chaque affirmation, cite la ou les sources entre crochets, une par crochet : [1], [2]... (jamais [1, 2]).
- Si les extraits ne permettent pas de répondre, dis-le clairement, sans inventer.
- Si deux extraits se contredisent, signale-le en citant les deux.
- Une « Fiche Alambic » résume le type et les données d'un document ; une valeur marquée
  « (à vérifier) » n'est pas sûre : si tu l'utilises, dis-le explicitement.
- Réponds en français, de façon concise."""

NO_RESULT = "Je n'ai trouvé aucun document accessible qui réponde à cette question."
EXTRACTS = "Extraits les plus pertinents des documents accessibles, sans reformulation."
EXTRACTS_BY_DOC = "Documents les plus pertinents parmi ceux accessibles, sans reformulation."

_CITE = re.compile(r"\[(\d+)\]")
# Références groupées écrites par certains modèles : [3, 4, 5], [3,4], [3-5], [3 et 4]
_CITE_GROUP = re.compile(r"\[(\d+(?:\s*(?:,|;|-|–|et)\s*\d+)+)\]")


def split_cites(text: str) -> str:
    """« [3, 4, 5] » ou « [3-5] » → « [3], [4], [5] » : chaque source est reconnue et listée."""
    def expand(m: re.Match) -> str:
        nums: list[int] = []
        for part in re.split(r"\s*(?:,|;|et)\s*", m.group(1)):
            a, _, b = re.sub(r"\s", "", part).replace("–", "-").partition("-")
            if b and a.isdigit() and b.isdigit() and 0 < int(b) - int(a) < 30:
                nums += range(int(a), int(b) + 1)
            elif a.isdigit():
                nums.append(int(a))
        return ", ".join(f"[{n}]" for n in nums)

    return _CITE_GROUP.sub(expand, text)

# Questions qui demandent une liste : seules celles-ci passent par la traduction en critères
# (un appel LLM de plus) ; les autres vont directement à la recherche par ressemblance.
LIST_QUESTION = re.compile(
    r"\b(tou(?:te)?s? les|liste[rz]?|quel(?:le)?s sont (?:les|tous|toutes)|combien (?:de|d')|"
    r"retrouve[rz]? les|affiche[rz]? les|donne[rz]?(?:-moi)? les)\b",
    re.IGNORECASE,
)

ROUTER_PROMPT = """Tu traduis une question en critères de recherche sur des documents typés.
Réponds UNIQUEMENT par un objet JSON, sans texte autour :
{"liste": true|false, "type": "CODE"|null, "champ": "nom"|null, "valeur": "texte"|null}
- liste : true si la question demande une liste ou un nombre de documents correspondant à une
  personne, une société, un numéro… ; false sinon (question sur le contenu d'un document).
- type : un code parmi ceux fournis, ou null si la question ne précise pas de type.
- champ : un nom parmi ceux fournis pour ce type, seulement si la question le désigne
  clairement (ex. « dont le fournisseur est… ») ; sinon null.
- valeur : le nom, la société ou le numéro cherché, recopié tel qu'il est écrit dans la
  question, sans civilité (M., Mme) ; null s'il n'y en a pas."""


def doc_path(doc_id: str) -> str:
    """Identifiant dans une URL : les / restent (route {doc_id:path}), le reste est encodé (espaces, #, ?)."""
    from urllib.parse import quote

    return quote(doc_id, safe="/:")


def build_context(hits: list[Hit]) -> str:
    return "\n\n".join(
        f"[{i}] {h.path} ({h.doc_label + ', ' if h.doc_label else ''}page {h.page})\n{h.text}"
        for i, h in enumerate(hits, 1)
    )


def dedupe(hits: list[Hit]) -> list[Hit]:
    """Un même contenu présent dans plusieurs fichiers (copies) n'occupe qu'une place."""
    seen: set[tuple[str, int, str]] = set()
    out = []
    for h in hits:
        key = (h.content_hash, h.page, h.text)
        if key not in seen:
            seen.add(key)
            out.append(h)
    return out


# Identifiant dans une question : au moins 5 caractères, un chiffre, et une lettre ou un séparateur
# (G6006-270173-05, 2026-114, FR763000…, AB-12/45). Une année seule (2026) n'en est pas un.
_TOKEN = re.compile(r"[\w][\w./-]*[\w]")


def identifiers(question: str) -> tuple[str, ...]:
    out = []
    for tok in _TOKEN.findall(question):
        if len(tok) >= 5 and any(c.isdigit() for c in tok) and (
            any(c.isalpha() for c in tok) or any(c in "-./" for c in tok)
        ) and tok not in out:
            out.append(tok)
    return tuple(out)


def exact_documents(store: Store, groups, idents: tuple[str, ...], doc_types) -> dict[tuple[str, str], Hit]:
    """Documents accessibles qui contiennent TOUS les identifiants (texte ou données extraites),
    avec un passage où l'identifiant apparaît. Vide : aucun document ne les contient."""
    doc_type = next(iter(doc_types)) if doc_types and len(doc_types) == 1 else None
    found: dict[tuple[str, str], Hit] | None = None
    for ident in idents:
        here: dict[tuple[str, str], Hit] = {}
        for m in store.find_mentions(groups, ident, doc_type, limit=200):
            if doc_types and m.doc_type not in doc_types and m.doc_type is not None:
                continue
            here[(m.source, m.doc_id)] = Hit(m.source, m.doc_id, m.path, m.page or 1, m.excerpt or ident, "", 0.0,
                                             m.label)
        for m in store.find_by_fields(groups, ident, doc_type, limit=200):
            if doc_types and m.doc_type not in doc_types:
                continue
            text = "Fiche Alambic : " + " ; ".join(f"{k} : {v}" for k, v, _ in m.fields)
            here.setdefault((m.source, m.doc_id), Hit(m.source, m.doc_id, m.path, 1, text, "", 0.0, m.label))
        found = here if found is None else {k: h for k, h in found.items() if k in here}
        if not found:
            return {}
    return found or {}


PASSAGES_PER_DOC = 3  # passages d'un même document transmis au LLM (mode par document)


def group_by_document(hits: list[Hit], k: int, url_for) -> list[DocResult]:
    """Un résultat par document, dans l'ordre de son meilleur passage ; k documents au plus."""
    groups: dict[tuple[str, str], list[Hit]] = {}
    for h in hits:
        key = (h.source, h.doc_id)
        if key in groups or len(groups) < k:
            groups.setdefault(key, []).append(h)
    out = []
    for n, ((source, doc_id), doc_hits) in enumerate(groups.items(), 1):
        best = doc_hits[0]
        pages: dict[int, str] = {}
        for h in sorted(doc_hits, key=lambda h: h.page):
            pages.setdefault(h.page, url_for(h))
        out.append(DocResult(n, source, doc_id, best.path, best.doc_label, url_for(best), doc_hits,
                             sorted(pages.items())))
    return out


def build_document_context(docs: list[DocResult]) -> str:
    parts = []
    for d in docs:
        pages = ", ".join(str(p) for p, _ in d.pages)
        passages = "\n…\n".join(f"(page {h.page}) {h.text}" for h in d.hits[:PASSAGES_PER_DOC])
        parts.append(f"[{d.n}] {d.path} ({d.label + ', ' if d.label else ''}pages {pages})\n{passages}")
    return "\n\n".join(parts)


CONDENSE_PROMPT = """Tu réécris la dernière question d'un utilisateur pour qu'elle se comprenne SEULE,
sans l'échange qui précède : remplace les pronoms et les sous-entendus (« et pour Durand ? »,
« son montant », « le même mois ») par ce qu'ils désignent dans l'échange.
Garde la langue, les noms, les numéros et les identifiants exactement comme écrits.
Si la question se comprend déjà seule, recopie-la telle quelle.
Réponds UNIQUEMENT par la question réécrite, sans guillemets ni explication."""

HISTORY_TURNS = 3  # échanges précédents pris en compte


def standalone_question(question: str, history: list[dict], llm: LLM | None) -> str:
    """Question de suivi → question autonome (pour la recherche ET la réponse). Sans LLM ou en cas
    d'échec : la question telle quelle."""
    turns = [t for t in (history or []) if str(t.get("question") or "").strip()][-HISTORY_TURNS:]
    if not turns or llm is None:
        return question
    dialogue = "\n".join(
        f"Utilisateur : {str(t['question']).strip()[:500]}\nAssistant : {str(t.get('reponse') or '').strip()[:600]}"
        for t in turns)
    try:
        raw = llm.complete(CONDENSE_PROMPT, f"Échange :\n{dialogue}\n\nDernière question : {question}")
    except Exception:
        return question
    line = next((x.strip().strip('«»"\'').strip() for x in str(raw).splitlines() if x.strip()), "")
    return line if 3 <= len(line) <= 600 else question


def ask(
    question: str,
    groups: frozenset[str],
    store: Store,
    embedder: Embedder,
    llm: LLM | None,
    *,
    k: int = 8,
    generate: bool = True,
    doc_types: frozenset[str] | None = None,
    url_for=lambda h: f"/documents/{h.source}/{doc_path(h.doc_id)}#page={h.page}",
    per_document: bool = False,
    reranker=None,
    history: list[dict] | None = None,
) -> Answer:
    """`per_document` : un résultat par document (ses passages regroupés), k documents ;
    les numéros [n] désignent alors des documents, pas des passages.
    `reranker` : reclassement des passages (ged.rerank). `history` : échanges précédents
    [{question, reponse}] — la question de suivi est d'abord rendue autonome."""
    asked = question
    llm = _Timed(llm) if llm is not None else None
    if history:
        question = standalone_question(question, history, llm)
    steps: dict[str, int] = {}
    answer = _answer(question, groups, store, embedder, llm, k=k, generate=generate, doc_types=doc_types,
                     url_for=url_for, per_document=per_document, reranker=reranker, steps=steps)
    answer.steps = steps
    if question != asked:
        answer.reformulated = question
    answer.llm_ms = int(llm.ms) if llm is not None else 0
    return answer


class _Timed:
    """LLM chronométré, le temps d'une question (propre à l'appel : pas d'état partagé)."""

    def __init__(self, llm):
        self.llm = llm
        self.ms = 0.0

    def complete(self, system: str, user: str) -> str:
        import time

        t0 = time.perf_counter()
        try:
            return self.llm.complete(system, user)
        finally:
            self.ms += (time.perf_counter() - t0) * 1000

    def __getattr__(self, name):
        return getattr(self.llm, name)


def _answer(question, groups, store, embedder, llm, *, k, generate, doc_types, url_for, per_document,
            reranker, steps=None) -> Answer:
    # Question de liste (« les factures de Dupont ») : recherche exacte par critères, rédaction ou
    # non — sans quoi on ne rendrait que des passages ressemblants, moins précis.
    if llm is not None:
        criteria = translate(question, store, llm)
        if criteria is not None:
            listed = list_answer(question, groups, store, criteria)
            # Aucune donnée extraite ne correspond (question thématique : « les documents sur la
            # sécurité ») : ce n'était pas une liste par critères, recherche ordinaire.
            if listed.criteria.documents:
                return listed
            log.info("Question de liste sans document par critères, recherche ordinaire : %r", question[:120])

    from time import perf_counter

    steps = {} if steps is None else steps
    t = perf_counter()
    vector = embedder.embed([question])[0]
    steps["vecteur"] = int((perf_counter() - t) * 1000)
    # Identifiant dans la question (n° de permis, de facture…) : seuls les documents qui le
    # contiennent tel quel comptent ; la ressemblance n'en ajoute pas d'autres.
    idents, absent = identifiers(question), ()
    exact = exact_documents(store, groups, idents, doc_types) if idents else {}
    # par document ou filtrage par identifiant : plus de passages, puis sélection
    wide = k * 4 if (per_document or exact) else k
    if reranker is not None:  # assez de candidats pour que le reclassement ait du choix
        wide = max(wide, reranker.candidates)
    t = perf_counter()
    hits = dedupe(store.search(question, vector, groups, k=wide, doc_types=doc_types or None))
    steps["recherche"] = int((perf_counter() - t) * 1000)
    if reranker is not None:
        t = perf_counter()
        hits = reranker.rerank(question, hits)
        steps["reclassement"] = int((perf_counter() - t) * 1000)
    if exact:
        hits = [h for h in hits if (h.source, h.doc_id) in exact]
        seen = {(h.source, h.doc_id) for h in hits}
        hits += [h for key, h in exact.items() if key not in seen]  # passage où figure l'identifiant
        if not per_document:
            hits = hits[:k]
    else:
        absent, idents = idents, ()
        hits = hits[:k] if not per_document else hits
    if not hits:
        return Answer(NO_RESULT, generated=False)

    if per_document:
        docs = group_by_document(hits, k, url_for)
        kept = [h for d in docs for h in d.hits]

        def cite_doc(n: int) -> Citation:
            d = docs[n - 1]
            return Citation(n, d.source, d.doc_id, d.path, d.best.page, d.url)

        if llm is None or not generate:
            return Answer(EXTRACTS_BY_DOC, [cite_doc(d.n) for d in docs], kept, generated=False, documents=docs, identifiers=idents, identifiers_absent=absent)
        text = split_cites(llm.complete(SYSTEM_PROMPT, f"Documents :\n\n{build_document_context(docs)}\n\nQuestion : {question}"))
        cited = sorted({int(n) for n in _CITE.findall(text) if 1 <= int(n) <= len(docs)})
        return Answer(text, [cite_doc(n) for n in cited], kept, documents=docs, identifiers=idents, identifiers_absent=absent)

    def cite(n: int) -> Citation:
        h = hits[n - 1]
        return Citation(n, h.source, h.doc_id, h.path, h.page, url_for(h))

    if llm is None or not generate:
        return Answer(EXTRACTS, [cite(n) for n in range(1, len(hits) + 1)], hits, generated=False, identifiers=idents, identifiers_absent=absent)

    text = split_cites(llm.complete(SYSTEM_PROMPT, f"Extraits :\n\n{build_context(hits)}\n\nQuestion : {question}"))

    cited = sorted({int(n) for n in _CITE.findall(text) if 1 <= int(n) <= len(hits)})
    return Answer(text, [cite(n) for n in cited], hits, identifiers=idents, identifiers_absent=absent)


# --- Questions de liste → recherche par critères ------------------------------------------

def translate(question: str, store: Store, llm: LLM) -> dict | None:
    """Critères {type, champ, valeur} d'une question de liste, ou None (recherche ordinaire).

    Tout est contrôlé : type et champ doivent exister dans l'index, et la valeur figurer
    telle quelle dans la question. Un LLM qui invente un critère est ignoré, jamais suivi."""
    import json

    if not LIST_QUESTION.search(question):
        return None
    types = store.type_counts()
    if not types:
        return None  # aucune donnée extraite : rien à filtrer
    fields = {code: store.field_names(code) for code, _, _ in types}
    catalog = "\n".join(f"- {code} ({label}) : champs {', '.join(fields[code]) or 'aucun'}"
                         for code, label, _ in types)
    try:
        raw = llm.complete(ROUTER_PROMPT, f"Types et champs :\n{catalog}\n\nQuestion : {question}")
        found = re.search(r"\{.*\}", raw, re.DOTALL)
        data = json.loads(found.group(0)) if found else {}
    except Exception:  # LLM injoignable ou réponse illisible : recherche ordinaire
        return None
    if not isinstance(data, dict) or not data.get("liste"):
        return None
    value = str(data.get("valeur") or "").strip()
    if len(value) < 2 or normalize(value) not in normalize(question):
        return None
    doc_type = data.get("type") if data.get("type") in fields else None
    field = data.get("champ") if doc_type and data.get("champ") in fields[doc_type] else None
    return {"type": doc_type, "champ": field, "valeur": value}


def list_answer(question: str, groups: frozenset[str], store: Store, criteria: dict) -> Answer:
    from ged.criteria import search_by_criteria

    r = search_by_criteria(store, groups, criteria["valeur"], doc_type=criteria["type"], field=criteria["champ"])
    n = len(r.documents)
    what = next((label for code, label, _ in store.type_counts() if code == r.doc_type), "document").lower()
    text = (f"{n} {what}{'s' if n > 1 else ''} dont {'le champ « ' + r.field + ' »' if r.field else 'un champ'} "
            f"contient « {r.value} »" + ("" if r.complete else " (liste tronquée)") + ".")
    if r.doubtful:
        text += f" Dont {r.doubtful} à vérifier."
    if r.mentions:
        text += f" {len(r.mentions)} autre{'s' if len(r.mentions) > 1 else ''} document{'s' if len(r.mentions) > 1 else ''} le mentionne{'nt' if len(r.mentions) > 1 else ''} seulement dans le texte."
    citations = [Citation(i, m.source, m.doc_id, m.path, 1, f"/documents/{m.source}/{doc_path(m.doc_id)}")
                 for i, m in enumerate(r.documents, 1)]
    return Answer(text, citations, [], generated=False, criteria=r)
