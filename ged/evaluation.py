"""Jeu de questions de référence : mesurer la qualité de la recherche et des réponses.

Chaque question dit quels documents doivent être retrouvés (et, au besoin, quelles pages,
quels mots dans la réponse, quels documents ne doivent JAMAIS apparaître). La mesure passe par
le même chemin que les utilisateurs (`ask`), droits compris.

    alambic-search eval jeu.yaml [--reponses] [--sortie mesure.json] [--reference avant.json]
    alambic-search eval-generer --n 30 --sortie brouillon.yaml   # questions candidates à relire

Format du jeu (YAML) :

    defaut:
      groupes: [compta, direction]     # ou utilisateur: olivier (groupes de l'annuaire)
      k: 8
    questions:
      - id: penalite-maintenance
        question: Quelle est la pénalité de retard du contrat de maintenance ?
        documents: [Compta/contrat-fournisseur.docx]   # au moins un doit être retrouvé
        pages: [2]                     # facultatif : page où est la réponse
        mots: ["2 %"]                  # facultatif : la réponse rédigée doit les contenir
        interdits: [RH/salaires.txt]   # facultatif : ne doivent jamais apparaître (droits)
      - id: hors-sujet
        question: Quel est le cours de l'or aujourd'hui ?
        sans_reponse: true             # aucun document ne répond : la réponse doit le dire

Un chemin attendu peut être la fin du chemin (« contrat-fournisseur.docx ») ou
« source:chemin » pour lever une ambiguïté entre sources.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

from ged.store import normalize


class EvalError(ValueError):
    """Jeu de questions invalide (message pour l'auteur du jeu)."""


@dataclass
class Case:
    id: str
    question: str
    documents: list[str] = field(default_factory=list)
    pages: list[int] = field(default_factory=list)
    mots: list[str] = field(default_factory=list)
    interdits: list[str] = field(default_factory=list)
    sans_reponse: bool = False
    groupes: list[str] = field(default_factory=list)
    utilisateur: str = ""
    k: int = 8
    types: list[str] = field(default_factory=list)


@dataclass
class CaseResult:
    id: str
    question: str
    rang: int | None = None  # rang du premier document attendu (1 = premier), None = pas trouvé
    rang_etendu: int | None = None  # non trouvé dans les k premiers : son rang parmi 50 (None : au-delà)
    trouve: bool | None = None  # un document attendu parmi les k premiers
    page_ok: bool | None = None  # la page attendue fait partie des pages trouvées du document
    fuites: list[str] = field(default_factory=list)  # documents interdits retrouvés
    retrouves: list[str] = field(default_factory=list)  # documents retrouvés, dans l'ordre
    # Réponse rédigée (--reponses)
    citations_justes: bool | None = None  # cite un document attendu, et rien d'autre
    mots_ok: bool | None = None
    refus_ok: bool | None = None  # sans_reponse : la réponse dit qu'elle ne sait pas
    reponse: str | None = None
    ms: int = 0
    erreur: str | None = None


# --- Lecture du jeu -----------------------------------------------------------------------

def load_cases(path: str | Path) -> list[Case]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    defaults = data.get("defaut") or {}
    items = data.get("questions")
    if not isinstance(items, list) or not items:
        raise EvalError(f"{path} : liste « questions » vide ou absente")
    cases, seen = [], set()
    for i, raw in enumerate(items, 1):
        if not isinstance(raw, dict) or not str(raw.get("question") or "").strip():
            raise EvalError(f"question n° {i} : texte de la question manquant")
        merged = {**defaults, **raw}
        cid = str(merged.get("id") or f"q{i:03d}")
        if cid in seen:
            raise EvalError(f"identifiant en double : {cid}")
        seen.add(cid)
        case = Case(
            id=cid, question=str(merged["question"]).strip(),
            documents=_strings(merged.get("documents")), pages=[int(p) for p in merged.get("pages") or []],
            mots=_strings(merged.get("mots")), interdits=_strings(merged.get("interdits")),
            sans_reponse=bool(merged.get("sans_reponse", False)), groupes=_strings(merged.get("groupes")),
            utilisateur=str(merged.get("utilisateur") or ""), k=int(merged.get("k") or 8),
            types=_strings(merged.get("types")),
        )
        if not case.documents and not case.sans_reponse:
            raise EvalError(f"{cid} : indiquer « documents » attendus, ou « sans_reponse: true »")
        if not case.groupes and not case.utilisateur:
            raise EvalError(f"{cid} : indiquer « groupes » ou « utilisateur » (ici ou dans « defaut »)")
        cases.append(case)
    return cases


def _strings(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (str, int, float)):
        return [str(value)]
    return [str(v) for v in value]


def matches(expected: str, source: str, path: str) -> bool:
    """« contrat.docx », « Compta/contrat.docx » ou « partage:Compta/contrat.docx »."""
    if ":" in expected and not expected.startswith(("/", "\\")) and expected.split(":", 1)[0] == source:
        expected = expected.split(":", 1)[1]
    e, p = expected.strip("/").lower(), path.strip("/").lower()
    return p == e or p.endswith("/" + e)


# Réponse qui reconnaît ne pas savoir (consigne du SYSTEM_PROMPT)
_REFUSAL = re.compile(
    r"(ne permet(tent)? pas|aucun (document|extrait|élément|information)|pas d'information|"
    r"je ne (sais|trouve|dispose)|n'(est|sont) pas (mentionn|précis|indiqu)|ne (contien|mentionn|précis)\w*)",
    re.IGNORECASE,
)


# --- Mesure ---------------------------------------------------------------------------------

EXTENDED = 50  # profondeur du diagnostic des documents non retrouvés

def run_case(case: Case, principals: frozenset[str], store, embedder, llm, *, with_answers: bool,
             reranker=None) -> CaseResult:
    from ged.answer import NO_RESULT, ask

    res = CaseResult(case.id, case.question)
    types = frozenset(case.types) or None
    t0 = time.perf_counter()
    # Recherche : documents classés comme les verrait l'utilisateur (un résultat par document)
    found = ask(case.question, principals, store, embedder, None, k=case.k, generate=False, doc_types=types,
                per_document=True, reranker=reranker)
    docs = found.documents or []
    res.retrouves = [f"{d.source}:{d.path}" for d in docs]
    res.fuites = [f"{d.source}:{d.path}" for d in docs if any(matches(x, d.source, d.path) for x in case.interdits)]
    if case.documents:
        for d in docs:
            if any(matches(x, d.source, d.path) for x in case.documents):
                res.rang = d.n
                if case.pages:
                    res.page_ok = any(p in case.pages for p, _ in d.pages)
                break
        res.trouve = res.rang is not None
        if not res.trouve:  # diagnostic : mal classé (rang 14) ou jamais retrouvé (au-delà de 50) ?
            wide = ask(case.question, principals, store, embedder, None, k=EXTENDED, generate=False,
                       doc_types=types, per_document=True, reranker=reranker)
            res.rang_etendu = next((d.n for d in wide.documents or []
                                    if any(matches(x, d.source, d.path) for x in case.documents)), None)
    # Réponse rédigée
    if with_answers and llm is not None:
        answer = ask(case.question, principals, store, embedder, llm, k=case.k, doc_types=types, reranker=reranker)
        res.reponse = answer.text
        cited = [(c.source, c.path) for c in answer.citations]
        res.fuites += [f"{s}:{p}" for s, p in cited if any(matches(x, s, p) for x in case.interdits)
                       and f"{s}:{p}" not in res.fuites]
        if case.sans_reponse:
            res.refus_ok = answer.text == NO_RESULT or bool(_REFUSAL.search(answer.text))
        else:
            good = [any(matches(x, s, p) for x in case.documents) for s, p in cited]
            res.citations_justes = bool(good) and all(good)
            if case.mots:
                text = normalize(answer.text)
                res.mots_ok = all(normalize(m) in text for m in case.mots)
    res.ms = int((time.perf_counter() - t0) * 1000)
    return res


def evaluate(cases: list[Case], store, embedder, llm=None, identity=None, *, with_answers: bool = False,
             progress=None, reranker=None) -> dict:
    from ged.api import resolve_principals

    results = []
    for i, case in enumerate(cases, 1):
        try:
            principals = resolve_principals(identity, case.utilisateur, ",".join(case.groupes))
            res = run_case(case, principals, store, embedder, llm, with_answers=with_answers, reranker=reranker)
        except Exception as exc:  # une question en échec n'arrête pas la mesure
            res = CaseResult(case.id, case.question, erreur=f"{type(exc).__name__} : {exc}")
        results.append(res)
        if progress:
            progress(i, len(cases), res)
    return {"le": time.strftime("%Y-%m-%dT%H:%M:%S"), "reponses": with_answers and llm is not None,
            "reclassement": reranker.describe() if reranker is not None else None,
            "resume": summary(results), "questions": [asdict(r) for r in results]}


def _rate(values) -> float | None:
    values = [v for v in values if v is not None]
    return round(sum(1 for v in values if v) / len(values), 3) if values else None


def summary(results: list[CaseResult]) -> dict:
    ranked = [r for r in results if r.trouve is not None]
    return {
        "questions": len(results),
        "erreurs": sum(1 for r in results if r.erreur),
        "trouve": _rate(r.trouve for r in results),  # document attendu parmi les k premiers
        "premier": _rate((r.rang == 1) if r.trouve is not None else None for r in results),
        "mrr": round(sum(1 / r.rang for r in ranked if r.rang) / len(ranked), 3) if ranked else None,
        "page": _rate(r.page_ok for r in results),
        "fuites": sum(len(r.fuites) for r in results),
        "citations_justes": _rate(r.citations_justes for r in results),
        "mots": _rate(r.mots_ok for r in results),
        "refus": _rate(r.refus_ok for r in results),
        "ms_moyen": int(sum(r.ms for r in results) / len(results)) if results else 0,
    }


# --- Comparaison avec une mesure de référence -------------------------------------------

LABELS = [
    ("trouve", "Document attendu retrouvé (k premiers)"),
    ("premier", "Document attendu en premier"),
    ("mrr", "Rang moyen (MRR, 1 = toujours premier)"),
    ("page", "Bonne page"),
    ("citations_justes", "Réponse : citations justes"),
    ("mots", "Réponse : mots attendus présents"),
    ("refus", "Réponse : « je ne sais pas » quand il faut"),
]


def compare(current: dict, reference: dict) -> dict:
    """Écarts par indicateur, questions qui régressent ou progressent (rang du document attendu)."""
    before = {q["id"]: q for q in reference.get("questions", [])}
    regress, progress = [], []
    for q in current.get("questions", []):
        old = before.get(q["id"])
        if old is None or q.get("trouve") is None or old.get("trouve") is None:
            continue
        r_new, r_old = q.get("rang") or 10**6, old.get("rang") or 10**6
        if r_new > r_old:
            regress.append({"id": q["id"], "avant": old.get("rang"), "apres": q.get("rang")})
        elif r_new < r_old:
            progress.append({"id": q["id"], "avant": old.get("rang"), "apres": q.get("rang")})
    deltas = {}
    for key, _ in LABELS:
        a, b = reference.get("resume", {}).get(key), current.get("resume", {}).get(key)
        if a is not None and b is not None:
            deltas[key] = round(b - a, 3)
    return {"ecarts": deltas, "regressions": regress, "progres": progress}


def format_report(result: dict, comparison: dict | None = None) -> str:
    s = result["resume"]
    lines = [f"{s['questions']} question(s), {s['ms_moyen']} ms en moyenne"
             + (f", {s['erreurs']} en erreur" if s["erreurs"] else "")]
    for key, label in LABELS:
        value = s.get(key)
        if value is None:
            continue
        shown = f"{value:.3f}" if key == "mrr" else f"{value * 100:.0f} %"
        delta = (comparison or {}).get("ecarts", {}).get(key)
        if delta:
            shown += f"  ({'+' if delta > 0 else ''}{delta:.3f})" if key == "mrr" else \
                     f"  ({'+' if delta > 0 else ''}{delta * 100:.0f} pts)"
        lines.append(f"  {label:<44} {shown}")
    lines.append(f"  {'Fuites (document interdit retrouvé)':<44} {s['fuites']}"
                 + ("  ← À CORRIGER" if s["fuites"] else ""))
    missed = [q for q in result["questions"] if q.get("trouve") is False]
    if missed:
        lines.append("\nNon retrouvés (rang réel : mal classé si ≤ 50, sinon jamais retrouvé) :")
        lines += [f"  - {q['id']} [{'rang ' + str(q['rang_etendu']) if q.get('rang_etendu') else 'au-delà de 50'}] : "
                  f"{q['question'][:80]}" for q in missed]
    leaks = [q for q in result["questions"] if q.get("fuites")]
    if leaks:
        lines.append("\nFUITES :")
        lines += [f"  - {q['id']} : {', '.join(q['fuites'])}" for q in leaks]
    errors = [q for q in result["questions"] if q.get("erreur")]
    if errors:
        lines.append("\nErreurs :")
        lines += [f"  - {q['id']} : {q['erreur']}" for q in errors]
    if comparison:
        if comparison["regressions"]:
            lines.append("\nRégressions (document attendu moins bien classé) :")
            lines += [f"  - {r['id']} : rang {r['avant'] or '—'} → {r['apres'] or 'absent'}" for r in comparison["regressions"]]
        if comparison["progres"]:
            lines.append("\nProgrès :")
            lines += [f"  - {r['id']} : rang {r['avant'] or 'absent'} → {r['apres']}" for r in comparison["progres"]]
    return "\n".join(lines)


def save(result: dict, path: str | Path) -> None:
    Path(path).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


# --- Questions candidates (brouillon à relire) ---------------------------------------------

GENERATE_PROMPT = """On construit un jeu de questions pour tester un moteur de recherche documentaire.
La question doit porter sur ce qui est PROPRE à ce document (un montant, une date, une condition,
une décision), pas sur un texte type qu'on retrouve partout (notice, mentions légales).
À partir du passage fourni, écris UNE question précise qu'un employé pourrait poser et à
laquelle ce passage répond. L'employé ne voit PAS le passage : la question doit se comprendre
seule et nommer ce dont elle parle (la personne, la société, le type de document, le numéro).
Interdit : « ce document », « ce passage », « selon le passage », « la personne mentionnée »,
« ci-dessus ». La question ne cite pas le nom du fichier. Évite les questions vagues.
Donne aussi la réponse, en quelques mots recopiés du passage.
Réponds UNIQUEMENT par un objet JSON : {"question": "...", "reponse": "..."}
Si le passage ne contient rien d'exploitable (sommaire, mentions légales, texte illisible),
réponds {"question": null}."""


# Questions qui renvoient au passage (« ce document », « selon le passage ») : intestables
_DEICTIC = re.compile(r"\b(ce|cet|cette|ces) (document|passage|texte|extrait|courrier|fichier)\b|selon le passage|"
                      r"\b(mentionnée?s?|citée?s?|indiquée?s?) (dans|ci)|ci-dessus|ci-dessous", re.IGNORECASE)


def _words(text: str) -> set[str]:
    return set(re.findall(r"\w{3,}", normalize(text)))


def equivalent_documents(store, embedder, passage: dict, threshold: float = 0.6) -> list[str]:
    """Autres documents qui contiennent (presque) le même passage — copies, modèles identiques,
    notices communes à plusieurs passeports… : tous sont des réponses valables."""
    if embedder is None:
        return []
    words = _words(passage["text"])
    hits = store.search(passage["text"][:1000], embedder.embed([passage["text"][:2000]])[0],
                        frozenset(passage["groups"]), k=30)
    out = []
    for h in hits:
        if (h.source, h.doc_id) == (passage["source"], passage["doc_id"]):
            continue
        other = _words(h.text)
        if words and other and len(words & other) / len(words | other) >= threshold:
            ref = f"{h.source}:{h.path}"
            if ref not in out:
                out.append(ref)
    return out


MAX_EQUIVALENTS = 3  # passage présent dans plus de documents : texte type (notice, modèle), question sans intérêt


def generate_candidates(store, llm, n: int = 30, source: str | None = None, embedder=None,
                        stats: dict | None = None) -> list[dict]:
    """Questions candidates variées : un document (ou groupe de copies) ne sert qu'une fois,
    les textes types communs à beaucoup de documents et les doublons de question sont écartés."""
    out, covered, asked = [], set(), set()
    stats = stats if stats is not None else {}
    for key in ("copies", "texte_type", "doublon", "renvoi", "inexploitable"):
        stats.setdefault(key, 0)
    for c in store.sample_chunks(n * 4, source=source):  # large : beaucoup de passages sont écartés
        if len(out) >= n:
            break
        ref = f"{c['source']}:{c['path']}"
        if not c["groups"]:  # document lisible par personne : question intestable
            continue
        if ref in covered:  # document (ou une de ses copies) déjà interrogé
            stats["copies"] += 1
            continue
        try:
            same = equivalent_documents(store, embedder, c)
        except Exception:
            same = []
        if len(same) > MAX_EQUIVALENTS:
            covered.update([ref, *same])
            stats["texte_type"] += 1
            continue
        try:
            raw = llm.complete(GENERATE_PROMPT, f"Passage (fichier {c['path']}, page {c['page']}) :\n\n{c['text'][:2500]}")
            found = re.search(r"\{.*\}", raw, re.DOTALL)
            data = json.loads(found.group(0)) if found else {}
        except Exception:
            stats["inexploitable"] += 1
            continue
        question = str(data.get("question") or "").strip()
        if len(question) < 10:
            stats["inexploitable"] += 1
            continue
        if _DEICTIC.search(question):
            stats["renvoi"] += 1
            continue
        key = " ".join(sorted(_words(question)))
        if key in asked:
            stats["doublon"] += 1
            continue
        asked.add(key)
        covered.update([ref, *same])
        item = {"id": f"gen-{len(out) + 1:03d}", "question": question, "documents": [ref, *same],
                "pages": [c["page"]], "groupes": sorted(c["groups"])[:5]}
        answer = str(data.get("reponse") or "").strip()
        if answer:
            item["reponse_attendue"] = answer[:200]
        out.append(item)
    return out


def candidates_yaml(items: list[dict]) -> str:
    header = ("# Questions CANDIDATES générées à partir des documents : à relire avant usage.\n"
              "# Pour chaque question : la reformuler comme un vrai utilisateur, vérifier le document\n"
              "# attendu, garder « reponse_attendue » comme aide ou la transformer en « mots ».\n"
              "# Ajouter des questions sans réponse (sans_reponse: true) et des documents interdits.\n")
    return header + yaml.safe_dump({"questions": items}, allow_unicode=True, sort_keys=False, width=110)
