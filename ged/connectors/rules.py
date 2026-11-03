"""Droits par dossier, pour les sources qui n'ont pas de droits exploitables (dossier monté,
FTP, S3) : la règle du dossier le plus spécifique s'applique ; sans règle, personne ne voit."""
from __future__ import annotations

from pathlib import PurePosixPath

from ged.models import Acl


class FolderRules:
    def __init__(self, rules: dict[str, list[str]] | None):
        # « Compta/Factures » → {"compta"} ; "" = racine
        self.rules = {
            str(PurePosixPath(k.strip("/"))) if k.strip("/") else "": frozenset(v)
            for k, v in (rules or {}).items()
        }

    def acl(self, path: str) -> Acl:
        parts = PurePosixPath(path).parent.parts
        for i in range(len(parts), -1, -1):
            key = "/".join(parts[:i])
            if key in self.rules:
                return Acl(self.rules[key])
        return Acl()

    def describe(self) -> dict[str, list[str]]:
        return {(k or "(racine)"): sorted(v) for k, v in sorted(self.rules.items())}
