"""Connecteur file system (partage SMB/NFS monté localement).

POC : les droits viennent d'une table de règles par dossier (fichier de config).
Étape suivante : lire les ACL NTFS et résoudre les SID via l'AD.
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

from ged.connectors.rules import FolderRules
from ged.models import Acl, DocumentRef

DEFAULT_EXTENSIONS = frozenset({".pdf", ".docx", ".txt", ".md", ".csv"})
DEFAULT_EXCLUDED_DIRS = frozenset({"$RECYCLE.BIN", "System Volume Information", "~snapshot", ".snapshot"})


class FileSystemConnector:
    def __init__(
        self,
        name: str,
        root: str | Path,
        acl_rules: dict[str, list[str]] | None = None,
        extensions: frozenset[str] = DEFAULT_EXTENSIONS,
        excluded_dirs: frozenset[str] = DEFAULT_EXCLUDED_DIRS,
    ):
        self.name = name
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise FileNotFoundError(f"Racine introuvable ou inaccessible : {self.root}")
        self._folder_rules = FolderRules(acl_rules)
        self._rules = self._folder_rules.rules
        self.extensions = frozenset(e.lower() for e in extensions)
        self.excluded_dirs = excluded_dirs

    def describe(self) -> dict:
        """Résumé lisible pour la console d'administration."""
        return {
            "type": "Partage réseau (file system)",
            "location": str(self.root),
            "rules": self._folder_rules.describe(),
            "extensions": sorted(self.extensions),
        }

    # --- Listing -----------------------------------------------------------------

    def list_documents(self) -> Iterator[DocumentRef]:
        def fail(err: OSError) -> None:
            # Un dossier illisible = listing incomplet : on remonte l'erreur plutôt
            # que de laisser croire que son contenu a été supprimé.
            raise err

        for dirpath, dirnames, filenames in os.walk(self.root, onerror=fail):
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in self.excluded_dirs]
            for fname in filenames:
                if fname.startswith("~$") or fname.startswith("."):
                    continue  # fichiers de verrou Office, fichiers cachés
                full = Path(dirpath) / fname
                if full.suffix.lower() not in self.extensions:
                    continue
                st = full.stat()
                yield self._ref(full, st)

    def _ref(self, full: Path, st: os.stat_result) -> DocumentRef:
        return DocumentRef(
            source=self.name,
            # Identité stable : périphérique + inode (POSIX) / File ID (NTFS via SMB).
            # Un déplacement ou un renommage conserve l'identifiant.
            doc_id=f"{st.st_dev}:{st.st_ino}",
            path=full.relative_to(self.root).as_posix(),
            mtime=st.st_mtime,
            size=st.st_size,
        )

    # --- Lecture ------------------------------------------------------------------

    def read_bytes(self, ref: DocumentRef) -> bytes:
        return self._safe_path(ref.path).read_bytes()

    def absolute_path(self, ref: DocumentRef) -> Path:
        return self._safe_path(ref.path)

    def locate(self, doc_id: str, hint_path: str | None = None) -> DocumentRef | None:
        if hint_path:  # chemin connu de l'index : vérification directe, sans parcours
            try:
                full = self._safe_path(hint_path)
                ref = self._ref(full, full.stat())
                if ref.doc_id == doc_id:
                    return ref
            except (OSError, PermissionError):
                pass
        for ref in self.list_documents():
            if ref.doc_id == doc_id:
                return ref
        return None

    def _safe_path(self, rel: str) -> Path:
        full = (self.root / rel).resolve()
        if not full.is_relative_to(self.root):
            raise PermissionError(f"Chemin hors de la source : {rel}")
        return full

    # --- Droits -------------------------------------------------------------------

    def acl(self, ref: DocumentRef) -> Acl:
        """Règle du dossier le plus spécifique. Aucune règle applicable = personne."""
        return self._folder_rules.acl(ref.path)
