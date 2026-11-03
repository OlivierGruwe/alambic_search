"""Recherche par critères : TOUS les documents qui correspondent, et seulement eux.

Complète la recherche par ressemblance (answer.py), qui classe des extraits et en garde
quelques-uns. « Toutes les factures de M. GRUWE » est un filtre, pas un classement :

1. `documents` : type demandé ET un champ extrait par Alambic Light contient la valeur (mot
   entier, sans tenir compte des majuscules ni des accents). Résultat sûr, exhaustif.
   Une valeur « à vérifier » est signalée, pas écartée.
2. `mentions` : documents du type demandé (ou non typés) dont le TEXTE contient la valeur sans
   qu'aucun champ ne corresponde : document mal extrait, non typé, ou personne citée ailleurs
   dans le document. Liste séparée, à regarder : rien n'est perdu, rien n'est mélangé.

Les droits de l'utilisateur s'appliquent aux deux listes, en SQL, comme pour les questions.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ged.models import Match
from ged.store import Store

LIMIT = 500


@dataclass
class CriteriaResult:
    value: str
    doc_type: str | None
    field: str | None
    documents: list[Match] = field(default_factory=list)
    mentions: list[Match] = field(default_factory=list)
    complete: bool = True  # False : plus de LIMIT documents, liste tronquée

    @property
    def doubtful(self) -> int:
        return sum(1 for m in self.documents if m.doubtful)


def search_by_criteria(
    store: Store, groups: frozenset[str] | None, value: str, *,
    doc_type: str | None = None, field: str | None = None, mentions: bool = True, limit: int = LIMIT,
) -> CriteriaResult:
    value = value.strip()
    result = CriteriaResult(value, doc_type or None, field or None)
    if len(value) < 2:
        return result
    result.documents = store.find_by_fields(groups, value, doc_type or None, field or None, limit=limit + 1)
    if len(result.documents) > limit:
        result.documents, result.complete = result.documents[:limit], False
    if mentions:
        found = {(m.source, m.doc_id) for m in result.documents}
        result.mentions = [
            m for m in store.find_mentions(groups, value, doc_type or None, limit=limit)
            if (m.source, m.doc_id) not in found
        ]
    return result
