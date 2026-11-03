"""Interface commune à toutes les sources (file system, SharePoint, CMIS...)."""
from __future__ import annotations

from collections.abc import Iterator
from typing import Protocol

from ged.models import Acl, DocumentRef


class Connector(Protocol):
    name: str

    def list_documents(self) -> Iterator[DocumentRef]:
        """Liste TOUS les documents visibles de la source.

        Doit lever une exception si le parcours est incomplet (partage injoignable...),
        sinon la synchronisation prendrait les absents pour des suppressions.
        """
        ...

    def read_bytes(self, ref: DocumentRef) -> bytes:
        """Contenu brut du document."""
        ...

    def acl(self, ref: DocumentRef) -> Acl:
        """Droits de lecture du document. Aucun principal autorisé = personne (refus par défaut)."""
        ...

    def locate(self, doc_id: str, hint_path: str | None = None) -> DocumentRef | None:
        """Retrouve un document par son identifiant stable (lien retour, revérification des droits)."""
        ...
