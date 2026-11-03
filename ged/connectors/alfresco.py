"""Connecteur Alfresco (API REST publique v1).

  GET /nodes/{id}?relativePath=…        dossier de départ (ex. Sites/compta/documentLibrary)
  GET /nodes/{id}/children              parcours, page par page
  GET /nodes/{id}?include=permissions   droits hérités et posés (accessStatus ALLOWED / DENIED)
  GET /nodes/{id}/content               contenu

Identité stable : l'identifiant du nœud (survit aux renommages et déplacements).
Compte de service : lecture seule (« Consommateur » sur les espaces à indexer).
"""
from __future__ import annotations

from collections.abc import Iterator

import httpx

from ged.connectors.ecm import AclCache, indexable, iso_ts, map_principal
from ged.connectors.filesystem import DEFAULT_EXTENSIONS
from ged.models import Acl, DocumentRef

API = "/alfresco/api/-default-/public/alfresco/versions/1"
PAGE = 100


class AlfrescoConnector:
    def __init__(self, name: str, url: str, root: str = "", *, username: str, password: str,
                 acl_refresh_hours: float = 24.0, extensions: frozenset[str] = DEFAULT_EXTENSIONS,
                 transport: httpx.BaseTransport | None = None, timeout: float = 60.0):
        self.name = name
        self.url = url.rstrip("/")
        self.root = root.strip("/")
        self.username = username
        self.extensions = frozenset(e.lower() for e in extensions)
        self._http = httpx.Client(base_url=self.url + API, auth=(username, password), transport=transport,
                                  timeout=httpx.Timeout(timeout, connect=10.0), follow_redirects=True)
        self._acls = AclCache(acl_refresh_hours * 3600)
        self._mtime: dict[str, float] = {}
        self.root_id = self._resolve_root()  # échoue tout de suite si l'URL, le compte ou le chemin sont faux

    def describe(self) -> dict:
        return {
            "type": "Alfresco (API REST)",
            "location": f"{self.url} · {self.root or 'Company Home'}",
            "account": self.username,
            "acl_refresh": f"droits Alfresco de chaque document, relus au moins toutes les {self._acls.ttl / 3600:g} h",
            "extensions": sorted(self.extensions),
        }

    def _get(self, path: str, **params) -> dict:
        r = self._http.get(path, params=params)
        if r.status_code == 401:
            raise PermissionError("Alfresco refuse le compte de service (401) : identifiant ou mot de passe")
        if r.status_code == 404:
            raise FileNotFoundError(f"Alfresco : introuvable ({path})")
        r.raise_for_status()
        return r.json()

    def _resolve_root(self) -> str:
        params = {"relativePath": self.root} if self.root else {}
        return self._get("/nodes/-root-", **params)["entry"]["id"]

    # --- Parcours ------------------------------------------------------------------

    def _children(self, node_id: str) -> Iterator[dict]:
        skip = 0
        while True:
            page = self._get(f"/nodes/{node_id}/children", skipCount=skip, maxItems=PAGE)["list"]
            for item in page["entries"]:
                yield item["entry"]
            if not page["pagination"].get("hasMoreItems"):
                return
            skip += PAGE

    def list_documents(self) -> Iterator[DocumentRef]:
        # Toute erreur remonte : un parcours incomplet ne doit pas déclencher de suppressions.
        stack = [(self.root_id, "")]
        while stack:
            node_id, prefix = stack.pop()
            for e in self._children(node_id):
                path = f"{prefix}{e['name']}"
                if e.get("isFolder"):
                    if not e["name"].startswith("."):
                        stack.append((e["id"], path + "/"))
                    continue
                if not e.get("isFile") or not indexable(e["name"], self.extensions):
                    continue
                mtime = iso_ts(e.get("modifiedAt"))
                self._mtime[e["id"]] = mtime
                yield DocumentRef(self.name, e["id"], path, mtime, int((e.get("content") or {}).get("sizeInBytes", 0)))

    # --- Lecture -------------------------------------------------------------------

    def read_bytes(self, ref: DocumentRef) -> bytes:
        r = self._http.get(f"/nodes/{ref.doc_id}/content")
        r.raise_for_status()
        return r.content

    def locate(self, doc_id: str, hint_path: str | None = None) -> DocumentRef | None:
        try:
            e = self._get(f"/nodes/{doc_id}", include="path")["entry"]
        except FileNotFoundError:
            return None
        if not e.get("isFile"):
            return None
        # Toujours dans la source ? (sinon : déplacé hors du périmètre indexé)
        elements = [p["id"] for p in (e.get("path") or {}).get("elements", [])]
        if self.root_id not in elements:
            return None
        names = [p["name"] for p in e["path"]["elements"]]
        rel = "/".join(names[elements.index(self.root_id) + 1:] + [e["name"]])
        return DocumentRef(self.name, doc_id, rel, iso_ts(e.get("modifiedAt")),
                           int((e.get("content") or {}).get("sizeInBytes", 0)))

    # --- Droits --------------------------------------------------------------------

    def acl(self, ref: DocumentRef) -> Acl:
        version = self._mtime.get(ref.doc_id, ref.mtime)
        cached = self._acls.get(ref.doc_id, version)
        if cached is not None:
            return cached
        perms = self._get(f"/nodes/{ref.doc_id}", include="permissions")["entry"].get("permissions") or {}
        entries = list(perms.get("locallySet") or [])
        if perms.get("isInheritanceEnabled", True):
            entries += perms.get("inherited") or []
        allow, deny = set(), set()
        for p in entries:
            principal = map_principal(p.get("authorityId", ""))
            if principal is None:
                continue
            (deny if p.get("accessStatus") == "DENIED" else allow).add(principal)
        acl = Acl(frozenset(allow), frozenset(deny))
        self._acls.put(ref.doc_id, version, acl)
        return acl

    def invalidate_acls(self) -> None:
        self._acls.clear()
