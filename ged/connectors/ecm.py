"""Briques communes aux connecteurs de GED par API (Alfresco, CMIS, SharePoint).

Principaux : ce que les droits des GED désignent est traduit dans les noms que fournit
l'annuaire d'Alambic Search (identity.py), sinon personne ne verrait rien :
- groupe « GROUP_Compta » (Alfresco, CMIS) ou « Compta » (Nuxeo) → « Compta » : le nom du
  groupe AD, que le résolveur LDAP ajoute aux principaux de l'utilisateur ;
- utilisateur « alice » → « u:alice » (principal ajouté pour chaque utilisateur connecté) ;
- « tout le monde » (GROUP_EVERYONE, Everyone, cmis:anyone…) → SID « Tout le monde ».
"""
from __future__ import annotations

import datetime as dt
import time
from pathlib import PurePosixPath

from ged.ntsecurity import EVERYONE

EVERYONE_NAMES = frozenset({"group_everyone", "everyone", "cmis:anyone", "anyone", "tout le monde"})
SYSTEM_NAMES = frozenset({"system", "administrator", "admin", "guest"})


def user_principal(login: str) -> str:
    return f"u:{login.strip().lower()}"


def map_principal(authority: str) -> str | None:
    """Autorité d'une GED → principal d'Alambic Search. None : à ignorer (système, rôle interne)."""
    a = (authority or "").strip()
    if not a:
        return None
    low = a.lower()
    if low in EVERYONE_NAMES:
        return EVERYONE
    if low.startswith("group_site_") or low.startswith("role_") or low in SYSTEM_NAMES:
        # Groupes de site Alfresco (GROUP_site_compta_SiteManager) : leurs membres sont
        # gérés dans la GED, sans équivalent AD direct ; rôles internes : jamais un utilisateur.
        return f"ged:{a}"
    if a.startswith("GROUP_"):
        return a[len("GROUP_"):]
    return user_principal(a)


def indexable(name: str, extensions: frozenset[str]) -> bool:
    if name.startswith("~$") or name.startswith("."):
        return False
    return PurePosixPath(name).suffix.lower() in extensions


def iso_ts(value) -> float:
    """Date ISO 8601 (« 2026-11-03T10:15:00.000+0000 », « …Z ») ou millisecondes → epoch."""
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value) / 1000.0
    text = str(value).replace("Z", "+00:00")
    if len(text) > 5 and text[-5] in "+-" and text[-3] != ":":  # +0000 → +00:00
        text = text[:-2] + ":" + text[-2:]
    return dt.datetime.fromisoformat(text).timestamp()


class AclCache:
    """Droits en cache par document : relus si le document a changé, et au moins toutes les
    `ttl` secondes (un changement de droits ne modifie pas toujours la date du document)."""

    def __init__(self, ttl: float):
        self.ttl = ttl
        self._data: dict[str, tuple[float, float, object]] = {}

    def get(self, doc_id: str, version: float):
        hit = self._data.get(doc_id)
        if hit and hit[0] == version and time.monotonic() - hit[1] < self.ttl:
            return hit[2]
        return None

    def put(self, doc_id: str, version: float, acl) -> None:
        self._data[doc_id] = (version, time.monotonic(), acl)

    def clear(self) -> None:
        self._data.clear()
