"""Droits : de quoi est fait un « principal », et pourquoi un document est (ou n'est pas) visible.

Un document porte des principaux autorisés et refusés (traduits par son connecteur) ; un
utilisateur a ses principaux (annuaire ou jeton Keycloak). Visible = au moins un en commun, et
aucun refus. Ce module classe les principaux par nature et repère ce que personne ne peut avoir.
"""
from __future__ import annotations

from dataclasses import dataclass

from ged.identity import AUTHENTICATED_USERS, BUILTIN_USERS, WELL_KNOWN
from ged.ntsecurity import EVERYONE

SPECIAL = frozenset({EVERYONE, AUTHENTICATED_USERS, BUILTIN_USERS})


@dataclass(frozen=True)
class Kind:
    code: str
    label: str
    orphan: bool = False  # aucun utilisateur ne peut porter ce principal
    hint: str = ""


KINDS = {
    "special": Kind("special", "Tout le monde"),
    "sid": Kind("sid", "Compte ou groupe Windows (SID)"),
    "user": Kind("user", "Utilisateur (identifiant)"),
    "name": Kind("name", "Nom de groupe"),
    "ged": Kind("ged", "Groupe propre à la GED", True,
                "Groupe de site ou rôle interne à la GED, sans équivalent dans l'annuaire : "
                "personne ne l'a. Donner les droits à un groupe AD dans la GED."),
    "entra": Kind("entra", "Compte Entra ID sans SID local", True,
                  "Compte ou groupe créé dans le nuage Microsoft, non synchronisé depuis l'AD : "
                  "aucun SID à comparer. Utiliser un groupe synchronisé."),
    "unknown-sid": Kind("unknown-sid", "SID inconnu de l'annuaire", True,
                        "Compte ou groupe supprimé, ou d'un autre domaine : personne ne l'a."),
}


def classify(principal: str, display=None) -> Kind:
    """Nature d'un principal. `display` (annuaire) : un SID qu'il ne sait pas nommer est inconnu."""
    if principal in SPECIAL:
        return KINDS["special"]
    if principal.startswith("S-1-"):
        if display is not None and principal not in WELL_KNOWN and display(principal) == principal:
            return KINDS["unknown-sid"]
        return KINDS["sid"]
    if principal.startswith("u:"):
        return KINDS["user"]
    if principal.startswith("ged:"):
        return KINDS["ged"]
    if principal.startswith("entra:"):
        return KINDS["entra"]
    return KINDS["name"]


def user_warnings(principals: frozenset[str], index_kinds: set[str]) -> list[str]:
    """Ce qui empêchera cet utilisateur de voir des documents qu'il devrait voir."""
    own = {classify(p).code for p in principals}
    out = []
    if not principals - SPECIAL - {p for p in principals if p.startswith("u:")}:
        out.append("Aucun groupe : l'utilisateur ne verra que les documents ouverts à tous ou à son nom. "
                   "Identifiant inconnu de l'annuaire, ou jeton Keycloak sans groupes ?")
    if "sid" in index_kinds and "sid" not in own:
        out.append("Aucun SID Windows : les documents des partages Windows (SMB) et de SharePoint, dont les "
                   "droits sont des SID, lui restent invisibles. Avec Keycloak, passer en « groupes: annuaire » "
                   "(groupes lus dans l'AD) plutôt qu'en « groupes: jeton ».")
    if "name" in index_kinds and "name" not in own and "sid" in own:
        out.append("Aucun nom de groupe : les règles par dossier (dossier, FTP, S3) et les groupes Alfresco ou "
                   "CMIS, écrits avec des noms, ne lui donnent rien.")
    return out
