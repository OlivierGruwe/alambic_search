"""Connecteur SharePoint Online / OneDrive (Microsoft Graph).

Application Entra ID (client_credentials), permission d'application Sites.Read.All (ou
Sites.Selected limitée aux sites indexés) + GroupMember.Read.All et User.Read.All pour traduire
les droits en SID AD.

  GET /sites/{hôte}:/{chemin du site}                site
  GET /sites/{id}/drives                             bibliothèque (« Documents »…)
  GET /drives/{d}/items/{id}/children                parcours, page par page (@odata.nextLink)
  GET /drives/{d}/items/{id}/permissions             droits (hérités compris)
  GET /drives/{d}/items/{id}/content                 contenu

Droits → principaux d'Alambic Search :
- utilisateur ou groupe Entra synchronisé depuis l'AD → son SID AD (onPremisesSecurityIdentifier),
  le même que celui que donne l'annuaire à la connexion ;
- groupe de site SharePoint (« Membres de Compta »…) → groupes AD indiqués dans la
  correspondance `groupes_de_site` de la source ; sans correspondance : ignoré (personne) ;
- liens de partage : ignorés (ils donnent l'accès à qui a le lien, pas à un groupe) ;
- utilisateur ou groupe Entra sans équivalent AD (cloud seulement) : « entra:… », qui ne
  correspond à aucun compte de l'annuaire : document invisible plutôt que trop visible.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from urllib.parse import quote, urlsplit

import httpx

from ged.connectors.ecm import AclCache, indexable, iso_ts
from ged.connectors.filesystem import DEFAULT_EXTENSIONS
from ged.models import Acl, DocumentRef

log = logging.getLogger(__name__)

GRAPH = "https://graph.microsoft.com/v1.0"
LOGIN = "https://login.microsoftonline.com"
READ_ROLES = frozenset({"read", "write", "owner", "sp.full control"})


class SharePointConnector:
    def __init__(self, name: str, site_url: str, library: str = "Documents", folder: str = "", *,
                 tenant_id: str, client_id: str, client_secret: str, site_groups: dict[str, list[str]] | None = None,
                 acl_refresh_hours: float = 24.0, extensions: frozenset[str] = DEFAULT_EXTENSIONS,
                 transport: httpx.BaseTransport | None = None, timeout: float = 60.0):
        self.name = name
        self.site_url = site_url.rstrip("/")
        self.library = library or "Documents"
        self.folder = folder.strip("/")
        self.extensions = frozenset(e.lower() for e in extensions)
        self.site_groups = {k.strip().lower(): list(v) for k, v in (site_groups or {}).items()}
        self.unmapped_site_groups: set[str] = set()
        self._tenant, self._client_id, self._secret = tenant_id, client_id, client_secret
        self._token: tuple[str, float] | None = None
        self._http = httpx.Client(transport=transport, follow_redirects=True,
                                  timeout=httpx.Timeout(timeout, connect=10.0))
        self._acls = AclCache(acl_refresh_hours * 3600)
        self._sids: dict[str, str] = {}  # id Entra → SID AD (ou entra:id)
        self._mtime: dict[str, float] = {}

        parts = urlsplit(self.site_url)
        if not parts.netloc:
            raise ValueError(f"Adresse de site invalide : {site_url} (ex. https://contoso.sharepoint.com/sites/Compta)")
        site = self._get(f"/sites/{parts.netloc}:{parts.path or '/'}")
        self.site_id = site["id"]
        drives = self._get(f"/sites/{self.site_id}/drives")["value"]
        drive = next((d for d in drives if d.get("name", "").lower() == self.library.lower()), None)
        if drive is None:
            raise FileNotFoundError(f"Bibliothèque « {self.library} » absente du site (disponibles : "
                                    f"{', '.join(d.get('name', '?') for d in drives)})")
        self.drive_id = drive["id"]
        root = f"/drives/{self.drive_id}/root" + (f":/{quote(self.folder)}" if self.folder else "")
        self.root_id = self._get(root)["id"]

    def describe(self) -> dict:
        info = {
            "type": "SharePoint / OneDrive (Microsoft 365)",
            "location": f"{self.site_url} · {self.library}" + (f" · /{self.folder}" if self.folder else ""),
            "account": f"application Entra {self._client_id}",
            "acl_refresh": f"droits SharePoint de chaque document, relus au moins toutes les {self._acls.ttl / 3600:g} h",
            "extensions": sorted(self.extensions),
        }
        if self.site_groups:
            info["site_groups"] = {k: v for k, v in self.site_groups.items()}
        if self.unmapped_site_groups:
            info["unmapped"] = sorted(self.unmapped_site_groups)
        return info

    # --- Transport -----------------------------------------------------------------

    def _bearer(self) -> str:
        if self._token is None or self._token[1] < time.monotonic():
            r = self._http.post(f"{LOGIN}/{self._tenant}/oauth2/v2.0/token", data={
                "grant_type": "client_credentials", "client_id": self._client_id, "client_secret": self._secret,
                "scope": "https://graph.microsoft.com/.default",
            })
            if r.status_code != 200:
                raise PermissionError(f"Entra ID refuse l'application ({r.status_code}) : identifiant, secret ou tenant")
            body = r.json()
            self._token = (body["access_token"], time.monotonic() + int(body.get("expires_in", 3600)) - 60)
        return self._token[0]

    def _request(self, url: str, **params) -> httpx.Response:
        url = url if url.startswith("http") else GRAPH + url
        for attempt in range(5):
            r = self._http.get(url, params=params or None, headers={"Authorization": f"Bearer {self._bearer()}"})
            if r.status_code in (429, 503):  # limitation de Graph : on attend ce qu'il demande
                time.sleep(min(float(r.headers.get("Retry-After") or 2 ** attempt), 60))
                continue
            if r.status_code == 401 and attempt == 0:
                self._token = None
                continue
            if r.status_code == 403:
                raise PermissionError("Graph refuse l'accès (403) : permissions de l'application à vérifier "
                                      "(Sites.Read.All ou Sites.Selected sur ce site)")
            if r.status_code == 404:
                raise FileNotFoundError(f"SharePoint : introuvable ({url.replace(GRAPH, '')})")
            r.raise_for_status()
            return r
        raise ConnectionError("Graph limite les appels : nouvel essai à la prochaine synchronisation")

    def _get(self, url: str, **params) -> dict:
        return self._request(url, **params).json()

    def _pages(self, url: str, **params) -> Iterator[dict]:
        page = self._get(url, **params)
        while True:
            yield from page.get("value", [])
            nxt = page.get("@odata.nextLink")
            if not nxt:
                return
            page = self._get(nxt)

    # --- Parcours ------------------------------------------------------------------

    def list_documents(self) -> Iterator[DocumentRef]:
        stack = [(self.root_id, "")]
        while stack:
            item_id, prefix = stack.pop()
            for it in self._pages(f"/drives/{self.drive_id}/items/{item_id}/children", **{"$top": 200}):
                name = it.get("name", "")
                if "folder" in it:
                    if not name.startswith("."):
                        stack.append((it["id"], f"{prefix}{name}/"))
                    continue
                if "file" not in it or not indexable(name, self.extensions):
                    continue
                mtime = iso_ts(it.get("lastModifiedDateTime"))
                self._mtime[it["id"]] = mtime
                yield DocumentRef(self.name, it["id"], f"{prefix}{name}", mtime, int(it.get("size") or 0))

    def read_bytes(self, ref: DocumentRef) -> bytes:
        # 302 vers une adresse de téléchargement signée : httpx ne lui transmet pas le jeton
        return self._request(f"/drives/{self.drive_id}/items/{ref.doc_id}/content").content

    def locate(self, doc_id: str, hint_path: str | None = None) -> DocumentRef | None:
        try:
            it = self._get(f"/drives/{self.drive_id}/items/{doc_id}")
        except FileNotFoundError:
            return None
        parent = (it.get("parentReference") or {}).get("path", "")  # /drives/{d}/root:/Compta/2026
        rel_parent = parent.split("root:", 1)[1].strip("/") if "root:" in parent else ""
        if self.folder:
            if rel_parent != self.folder and not rel_parent.startswith(self.folder + "/"):
                return None  # déplacé hors de la source
            rel_parent = rel_parent[len(self.folder):].strip("/")
        path = f"{rel_parent}/{it['name']}" if rel_parent else it["name"]
        return DocumentRef(self.name, doc_id, path, iso_ts(it.get("lastModifiedDateTime")), int(it.get("size") or 0))

    # --- Droits --------------------------------------------------------------------

    def _sid(self, kind: str, entra_id: str) -> str:
        key = f"{kind}:{entra_id}"
        if key not in self._sids:
            try:
                obj = self._get(f"/{kind}s/{entra_id}", **{"$select": "onPremisesSecurityIdentifier"})
                sid = obj.get("onPremisesSecurityIdentifier")
            except FileNotFoundError:
                sid = None
            self._sids[key] = sid or f"entra:{entra_id}"
        return self._sids[key]

    def _principals(self, identity: dict) -> set[str]:
        out: set[str] = set()
        if (u := identity.get("user")) and u.get("id"):
            out.add(self._sid("user", u["id"]))
        if (g := identity.get("group")) and g.get("id"):
            out.add(self._sid("group", g["id"]))
        if (sg := identity.get("siteGroup")) and sg.get("displayName"):
            mapped = self.site_groups.get(sg["displayName"].strip().lower())
            if mapped:
                out.update(mapped)
            else:
                self.unmapped_site_groups.add(sg["displayName"])
        return out

    def acl(self, ref: DocumentRef) -> Acl:
        version = self._mtime.get(ref.doc_id, ref.mtime)
        cached = self._acls.get(ref.doc_id, version)
        if cached is not None:
            return cached
        allow: set[str] = set()
        for perm in self._pages(f"/drives/{self.drive_id}/items/{ref.doc_id}/permissions"):
            if "link" in perm:
                continue  # lien de partage : accès à qui a le lien, pas à un groupe
            if not READ_ROLES & {r.lower() for r in perm.get("roles", [])}:
                continue
            for identity in [perm.get("grantedToV2") or {}, *(perm.get("grantedToIdentitiesV2") or [])]:
                allow |= self._principals(identity)
        acl = Acl(frozenset(allow))
        self._acls.put(ref.doc_id, version, acl)
        return acl

    def invalidate_acls(self) -> None:
        self._acls.clear()
