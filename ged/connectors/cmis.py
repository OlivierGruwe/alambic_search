"""Connecteur GED CMIS 1.1 (« browser binding » JSON) : Alfresco, Nuxeo, OpenText, Documentum…

  GET {url}                                                  dépôts disponibles
  GET {rootFolderUrl}/{chemin}?cmisselector=object            dossier de départ
  GET {rootFolderUrl}?objectId=…&cmisselector=children        parcours, page par page
  GET {rootFolderUrl}?objectId=…&cmisselector=acl             droits (ACE : principal + permissions)
  GET {rootFolderUrl}?objectId=…&cmisselector=content         contenu

Identité stable : cmis:versionSeriesId (une nouvelle version garde le même document),
sinon cmis:objectId. CMIS ne connaît pas les refus : seules des autorisations.
"""
from __future__ import annotations

from collections.abc import Iterator
from urllib.parse import quote

import httpx

from ged.connectors.ecm import AclCache, indexable, iso_ts, map_principal
from ged.connectors.filesystem import DEFAULT_EXTENSIONS
from ged.models import Acl, DocumentRef

PAGE = 100
# Permissions qui donnent la lecture (CMIS de base, puis noms courants des GED)
READ_HINTS = ("cmis:read", "cmis:write", "cmis:all", "read", "consumer", "contributor", "collaborator",
              "coordinator", "editor", "everything", "browse")


def _props(obj: dict) -> dict:
    o = obj.get("object", obj)
    return o.get("succinctProperties") or {k: v.get("value") for k, v in (o.get("properties") or {}).items()}


class CmisConnector:
    def __init__(self, name: str, url: str, root: str = "", *, repository: str = "", username: str,
                 password: str, acl_refresh_hours: float = 24.0, extensions: frozenset[str] = DEFAULT_EXTENSIONS,
                 transport: httpx.BaseTransport | None = None, timeout: float = 60.0):
        self.name = name
        self.url = url.rstrip("/")
        self.root = root.strip("/")
        self.username = username
        self.extensions = frozenset(e.lower() for e in extensions)
        self._http = httpx.Client(auth=(username, password), transport=transport, follow_redirects=True,
                                  timeout=httpx.Timeout(timeout, connect=10.0))
        self._acls = AclCache(acl_refresh_hours * 3600)
        self._object_ids: dict[str, str] = {}  # doc_id (série de versions) → objectId de la version courante
        self._mtime: dict[str, float] = {}
        repos = self._get(self.url)
        if not repos:
            raise FileNotFoundError(f"CMIS : aucun dépôt sur {self.url}")
        if repository and repository not in repos:
            raise FileNotFoundError(f"CMIS : dépôt « {repository} » absent (disponibles : {', '.join(repos)})")
        repo = repos[repository] if repository else next(iter(repos.values()))
        self.repository = repo.get("repositoryId", repository)
        self.root_url = repo["rootFolderUrl"].rstrip("/")
        target = f"{self.root_url}/{quote(self.root)}" if self.root else self.root_url
        self.root_id = _props(self._get(target, cmisselector="object", succinct="true"))["cmis:objectId"]

    def describe(self) -> dict:
        return {
            "type": "GED CMIS",
            "location": f"{self.url} · dépôt {self.repository} · /{self.root}",
            "account": self.username,
            "acl_refresh": f"ACL CMIS de chaque document, relues au moins toutes les {self._acls.ttl / 3600:g} h",
            "extensions": sorted(self.extensions),
        }

    def _get(self, url: str, **params):
        r = self._http.get(url, params=params)
        if r.status_code == 401:
            raise PermissionError("La GED refuse le compte de service (401) : identifiant ou mot de passe")
        if r.status_code == 404:
            raise FileNotFoundError(f"CMIS : introuvable ({url})")
        r.raise_for_status()
        return r.json()

    def _children(self, object_id: str) -> Iterator[dict]:
        skip = 0
        while True:
            page = self._get(self.root_url, objectId=object_id, cmisselector="children", succinct="true",
                             maxItems=PAGE, skipCount=skip)
            for obj in page.get("objects", []):
                yield _props(obj)
            if not page.get("hasMoreItems"):
                return
            skip += PAGE

    def list_documents(self) -> Iterator[DocumentRef]:
        stack = [(self.root_id, "")]
        while stack:
            object_id, prefix = stack.pop()
            for p in self._children(object_id):
                name = p.get("cmis:name", "")
                base = p.get("cmis:baseTypeId")
                if base == "cmis:folder":
                    if not name.startswith("."):
                        stack.append((p["cmis:objectId"], f"{prefix}{name}/"))
                    continue
                if base != "cmis:document" or not indexable(name, self.extensions):
                    continue
                doc_id = p.get("cmis:versionSeriesId") or p["cmis:objectId"]
                mtime = iso_ts(p.get("cmis:lastModificationDate"))
                self._object_ids[doc_id] = p["cmis:objectId"]
                self._mtime[doc_id] = mtime
                yield DocumentRef(self.name, doc_id, f"{prefix}{name}", mtime, int(p.get("cmis:contentStreamLength") or 0))

    def _object_id(self, doc_id: str) -> str:
        return self._object_ids.get(doc_id, doc_id)

    def read_bytes(self, ref: DocumentRef) -> bytes:
        r = self._http.get(self.root_url, params={"objectId": self._object_id(ref.doc_id), "cmisselector": "content"})
        r.raise_for_status()
        return r.content

    def locate(self, doc_id: str, hint_path: str | None = None) -> DocumentRef | None:
        if hint_path:  # chemin connu : vérification directe
            try:
                full = "/".join(x for x in (self.root, hint_path) if x)
                p = _props(self._get(f"{self.root_url}/{quote(full)}", cmisselector="object", succinct="true"))
                if (p.get("cmis:versionSeriesId") or p.get("cmis:objectId")) == doc_id:
                    self._object_ids[doc_id] = p["cmis:objectId"]
                    return DocumentRef(self.name, doc_id, hint_path, iso_ts(p.get("cmis:lastModificationDate")),
                                       int(p.get("cmis:contentStreamLength") or 0))
            except (FileNotFoundError, httpx.HTTPError):
                pass
        for ref in self.list_documents():  # déplacé : on le cherche
            if ref.doc_id == doc_id:
                return ref
        return None

    def acl(self, ref: DocumentRef) -> Acl:
        version = self._mtime.get(ref.doc_id, ref.mtime)
        cached = self._acls.get(ref.doc_id, version)
        if cached is not None:
            return cached
        data = self._get(self.root_url, objectId=self._object_id(ref.doc_id), cmisselector="acl")
        allow = set()
        for ace in data.get("aces", []):
            perms = " ".join(ace.get("permissions") or []).lower()
            if not any(h in perms for h in READ_HINTS):
                continue
            principal = map_principal((ace.get("principal") or {}).get("principalId", ""))
            if principal is not None:
                allow.add(principal)
        acl = Acl(frozenset(allow))
        self._acls.put(ref.doc_id, version, acl)
        return acl

    def invalidate_acls(self) -> None:
        self._acls.clear()
