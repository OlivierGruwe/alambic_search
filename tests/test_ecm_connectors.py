"""Connecteurs Alfresco (REST), CMIS (browser binding) et SharePoint (Graph), contre des serveurs
simulés qui suivent leur API publique. Pas de serveur réel ici : à valider sur une vraie GED."""
from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from ged.connectors.alfresco import AlfrescoConnector
from ged.connectors.cmis import CmisConnector
from ged.connectors.ecm import map_principal
from ged.connectors.sharepoint import SharePointConnector
from ged.ntsecurity import EVERYONE
from ged.store import MemoryStore
from ged.sync import sync_source


def ok(data) -> httpx.Response:
    return httpx.Response(200, json=data)


# --- Correspondance des principaux ---------------------------------------------------------

@pytest.mark.parametrize("authority, principal", [
    ("GROUP_EVERYONE", EVERYONE), ("Everyone", EVERYONE), ("cmis:anyone", EVERYONE),
    ("GROUP_Compta", "Compta"), ("alice", "u:alice"), ("Alice", "u:alice"),
    ("GROUP_site_compta_SiteManager", "ged:GROUP_site_compta_SiteManager"), ("ROLE_OWNER", "ged:ROLE_OWNER"),
    ("", None),
])
def test_map_principal(authority, principal):
    assert map_principal(authority) == principal


def test_users_get_their_login_principal():
    from ged.identity import StaticResolver

    assert "u:alice" in StaticResolver({"Alice": ["compta"]}).principals("alice")


# --- Alfresco --------------------------------------------------------------------------------

class FakeAlfresco:
    API = "/alfresco/api/-default-/public/alfresco/versions/1"

    def __init__(self):
        # id : (nom, parent, dossier ?, contenu)
        self.nodes = {
            "root": ("documentLibrary", None, True, None),
            "f-compta": ("Compta", "root", True, None),
            "d1": ("facture-durand.pdf", "f-compta", False, b"%PDF facture"),
            "d2": ("note.txt", "root", False, b"Note de service : reunion lundi"),
            "d3": ("image.bmp", "root", False, b"x"),  # format non indexé
        }
        self.perms = {
            "d1": {"isInheritanceEnabled": True, "inherited": [{"authorityId": "GROUP_Compta", "accessStatus": "ALLOWED"}],
                   "locallySet": [{"authorityId": "bob", "accessStatus": "DENIED"}]},
            "d2": {"isInheritanceEnabled": False, "inherited": [{"authorityId": "GROUP_EVERYONE"}],
                   "locallySet": [{"authorityId": "GROUP_RH", "accessStatus": "ALLOWED"}]},
        }
        self.calls = []

    def entry(self, nid):
        name, _, folder, content = self.nodes[nid]
        e = {"id": nid, "name": name, "isFolder": folder, "isFile": not folder,
             "modifiedAt": "2026-11-03T10:15:00.000+0000"}
        if content is not None:
            e["content"] = {"sizeInBytes": len(content)}
        return e

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization") != httpx.BasicAuth("svc", "pw")._auth_header:
            return httpx.Response(401)
        path = request.url.path.removeprefix(self.API)
        q = {k: v[0] for k, v in parse_qs(urlsplit(str(request.url)).query).items()}
        self.calls.append(path)
        if path == "/nodes/-root-":
            if q.get("relativePath") != "Sites/compta/documentLibrary":
                return httpx.Response(404)
            return ok({"entry": self.entry("root")})
        parts = path.split("/")
        nid = parts[2]
        if nid not in self.nodes:
            return httpx.Response(404)
        if path.endswith("/children"):
            kids = [k for k, v in self.nodes.items() if v[1] == nid]
            skip, size = int(q.get("skipCount", 0)), int(q.get("maxItems", 100))
            page = kids[skip:skip + size]
            return ok({"list": {"entries": [{"entry": self.entry(k)} for k in page],
                                "pagination": {"hasMoreItems": skip + size < len(kids)}}})
        if path.endswith("/content"):
            return httpx.Response(200, content=self.nodes[nid][3])
        e = self.entry(nid)
        if q.get("include") == "permissions":
            e["permissions"] = self.perms.get(nid, {})
        if q.get("include") == "path":
            chain, cur = [], self.nodes[nid][1]
            while cur:
                chain.insert(0, {"id": cur, "name": self.nodes[cur][0]})
                cur = self.nodes[cur][1]
            e["path"] = {"elements": [{"id": "company", "name": "Company Home"}, *chain]}
        return ok({"entry": e})


def alfresco(fake, **kw):
    return AlfrescoConnector("ged", "https://alfresco.test", "Sites/compta/documentLibrary", username="svc",
                             password=kw.pop("password", "pw"), transport=httpx.MockTransport(fake.handler),
                             extensions=frozenset({".pdf", ".txt"}), **kw)


def test_alfresco_listing_paths_and_rights():
    fake = FakeAlfresco()
    c = alfresco(fake)
    refs = {r.path: r for r in c.list_documents()}
    assert set(refs) == {"Compta/facture-durand.pdf", "note.txt"}
    acl = c.acl(refs["Compta/facture-durand.pdf"])
    assert acl.allow == {"Compta"} and acl.deny == {"u:bob"}
    assert not acl.permits(frozenset({"Compta", "u:bob"}))  # refus explicite : l'emporte
    assert c.acl(refs["note.txt"]).allow == {"RH"}  # héritage coupé : « tout le monde » ne s'applique pas
    assert c.read_bytes(refs["note.txt"]).startswith(b"Note de service")
    assert c.locate("d1").path == "Compta/facture-durand.pdf"
    assert c.locate("inconnu") is None


def test_alfresco_wrong_password_or_path_fails_at_creation():
    with pytest.raises(PermissionError, match="401"):
        alfresco(FakeAlfresco(), password="faux")
    with pytest.raises(FileNotFoundError):
        AlfrescoConnector("ged", "https://alfresco.test", "Sites/autre", username="svc", password="pw",
                          transport=httpx.MockTransport(FakeAlfresco().handler))


def test_alfresco_acl_cache_and_paging(monkeypatch):
    import ged.connectors.alfresco as mod

    monkeypatch.setattr(mod, "PAGE", 1)  # une entrée par page : la pagination est suivie
    fake = FakeAlfresco()
    c = alfresco(fake)
    refs = list(c.list_documents())
    assert len(refs) == 2
    c.acl(refs[0])
    n = len(fake.calls)
    c.acl(refs[0])
    assert len(fake.calls) == n  # en cache
    c.invalidate_acls()
    c.acl(refs[0])
    assert len(fake.calls) == n + 1


def test_alfresco_end_to_end_search_respects_rights(embedder):
    store = MemoryStore()
    sync_source(alfresco(FakeAlfresco()), store, embedder)
    q = "note de service reunion"
    assert store.search(q, embedder.embed([q])[0], frozenset({"RH"}), k=3)
    assert store.search(q, embedder.embed([q])[0], frozenset({"Compta", EVERYONE}), k=3) == []


# --- CMIS --------------------------------------------------------------------------------------

class FakeCmis:
    ROOT = "https://ged.test/cmis/browser/root"

    def __init__(self):
        self.objects = {
            "racine": {"cmis:objectId": "racine", "cmis:baseTypeId": "cmis:folder", "cmis:name": "Documents", "parent": None},
            "f1": {"cmis:objectId": "f1", "cmis:baseTypeId": "cmis:folder", "cmis:name": "Contrats", "parent": "racine"},
            "c1;1.1": {"cmis:objectId": "c1;1.1", "cmis:versionSeriesId": "c1", "cmis:baseTypeId": "cmis:document",
                       "cmis:name": "contrat.txt", "cmis:lastModificationDate": 1762164900000,
                       "cmis:contentStreamLength": 30, "parent": "f1", "content": b"Contrat de maintenance Durand"},
        }
        self.acl = {"c1;1.1": [{"principal": {"principalId": "GROUP_Juridique"}, "permissions": ["cmis:read"]},
                               {"principal": {"principalId": "claire"}, "permissions": ["{http://x}Coordinator"]},
                               {"principal": {"principalId": "dave"}, "permissions": ["cmis:none"]}]}

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url).split("?")[0]
        q = {k: v[0] for k, v in parse_qs(urlsplit(str(request.url)).query).items()}
        if url == "https://ged.test/cmis/browser":
            return ok({"main": {"repositoryId": "main", "rootFolderUrl": self.ROOT}})
        if url == f"{self.ROOT}/Documents" and q.get("cmisselector") == "object":
            return ok({"succinctProperties": self.objects["racine"]})
        if url == f"{self.ROOT}/Documents/Contrats/contrat.txt":
            return ok({"succinctProperties": {k: v for k, v in self.objects["c1;1.1"].items() if k != "content"}})
        if url == self.ROOT:
            oid, sel = q.get("objectId"), q.get("cmisselector")
            if sel == "children":
                kids = [{"object": {"succinctProperties": {k: v for k, v in o.items() if k not in ("parent", "content")}}}
                        for o in self.objects.values() if o["parent"] == oid]
                return ok({"objects": kids, "hasMoreItems": False})
            if sel == "content":
                return httpx.Response(200, content=self.objects[oid]["content"])
            if sel == "acl":
                return ok({"aces": self.acl.get(oid, []), "isExact": True})
        return httpx.Response(404)


def test_cmis_listing_versions_and_rights():
    c = CmisConnector("ged", "https://ged.test/cmis/browser", "Documents", username="svc", password="pw",
                      transport=httpx.MockTransport(FakeCmis().handler), extensions=frozenset({".txt"}))
    refs = list(c.list_documents())
    assert [(r.doc_id, r.path) for r in refs] == [("c1", "Contrats/contrat.txt")]  # série de versions : stable
    assert c.acl(refs[0]).allow == {"Juridique", "u:claire"}  # « cmis:none » ne donne pas la lecture
    assert c.read_bytes(refs[0]) == b"Contrat de maintenance Durand"
    assert c.locate("c1", hint_path="Contrats/contrat.txt").path == "Contrats/contrat.txt"
    assert "dépôt main" in c.describe()["location"]


def test_cmis_unknown_repository():
    with pytest.raises(FileNotFoundError, match="disponibles : main"):
        CmisConnector("ged", "https://ged.test/cmis/browser", repository="autre", username="svc", password="pw",
                      transport=httpx.MockTransport(FakeCmis().handler))


# --- SharePoint (Graph) ----------------------------------------------------------------------

SID_COMPTA = "S-1-5-21-1-2-3-1201"
SID_ALICE = "S-1-5-21-1-2-3-1104"


class FakeGraph:
    def __init__(self):
        self.tokens = 0
        self.throttled = False
        self.items = {
            "root": {"id": "root", "name": "root", "folder": {}, "parent": None},
            "fc": {"id": "fc", "name": "Factures", "folder": {}, "parent": "root"},
            "i1": {"id": "i1", "name": "facture.txt", "file": {}, "size": 20, "parent": "fc",
                   "lastModifiedDateTime": "2026-11-03T09:00:00Z", "content": b"Facture Durand pour 1200 euros"},
            "i2": {"id": "i2", "name": "budget.txt", "file": {}, "size": 10, "parent": "root",
                   "lastModifiedDateTime": "2026-11-02T09:00:00Z", "content": b"Budget previsionnel 2027 du service"},
        }
        self.perms = {
            "i1": [
                {"roles": ["read"], "grantedToV2": {"group": {"id": "g-compta"}}},
                {"roles": ["write"], "grantedToV2": {"user": {"id": "u-alice"}}},
                {"roles": ["read"], "grantedToV2": {"siteGroup": {"id": "3", "displayName": "Membres de Finances"}}},
                {"roles": ["read"], "link": {"scope": "organization"}, "grantedToIdentitiesV2": [{"user": {"id": "u-x"}}]},
            ],
            "i2": [{"roles": ["read"], "grantedToV2": {"siteGroup": {"id": "4", "displayName": "Visiteurs de Finances"}}},
                   {"roles": ["read"], "grantedToV2": {"group": {"id": "g-cloud"}}}],
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith("https://login.microsoftonline.com/tenant-1/oauth2/v2.0/token"):
            body = parse_qs(request.content.decode())
            if body.get("client_secret") != ["secret"]:
                return httpx.Response(401, json={"error": "invalid_client"})
            self.tokens += 1
            return ok({"access_token": f"t{self.tokens}", "expires_in": 3600})
        if url.startswith("https://download.test/"):
            assert "authorization" not in request.headers  # le jeton ne part pas vers l'adresse signée
            return httpx.Response(200, content=self.items[url.rsplit("/", 1)[1]]["content"])
        if request.headers.get("authorization") != f"Bearer t{self.tokens}":
            return httpx.Response(401)
        if self.throttled:
            self.throttled = False
            return httpx.Response(429, headers={"Retry-After": "0"})
        path = urlsplit(url).path.removeprefix("/v1.0")
        if path == "/sites/contoso.sharepoint.com:/sites/Finances":
            return ok({"id": "site-1"})
        if path == "/sites/site-1/drives":
            return ok({"value": [{"id": "drv", "name": "Documents"}, {"id": "drv2", "name": "Archives"}]})
        if path == "/drives/drv/root":
            return ok({"id": "root"})
        if path == "/drives/drv/root:/Factures":
            return ok({"id": "fc"})
        if path.startswith("/users/") or path.startswith("/groups/"):
            sids = {"u-alice": SID_ALICE, "g-compta": SID_COMPTA}
            oid = path.split("/")[2]
            return ok({"onPremisesSecurityIdentifier": sids.get(oid)})
        if path.startswith("/drives/drv/items/"):
            parts = path.split("/")
            iid = parts[4]
            if len(parts) == 5:
                it = self.items[iid]
                parent_path = "/drives/drv/root:" + ("/Factures" if it["parent"] == "fc" else "")
                return ok({**{k: v for k, v in it.items() if k not in ("parent", "content")},
                           "parentReference": {"path": parent_path}})
            if parts[5] == "children":
                kids = [{k: v for k, v in it.items() if k not in ("parent", "content")}
                        for it in self.items.values() if it["parent"] == iid]
                # deux pages pour vérifier @odata.nextLink
                if "skiptoken" not in url and len(kids) > 1:
                    return ok({"value": kids[:1], "@odata.nextLink": f"{url.split('?')[0]}?skiptoken=1"})
                return ok({"value": kids[1:] if "skiptoken" in url else kids})
            if parts[5] == "permissions":
                return ok({"value": self.perms.get(iid, [])})
            if parts[5] == "content":
                return httpx.Response(302, headers={"Location": f"https://download.test/{iid}"})
        return httpx.Response(404)


def sharepoint(fake, folder="", **kw):
    return SharePointConnector(
        "sp", "https://contoso.sharepoint.com/sites/Finances", "Documents", folder, tenant_id="tenant-1",
        client_id="app", client_secret=kw.pop("secret", "secret"), transport=httpx.MockTransport(fake.handler),
        extensions=frozenset({".txt"}), site_groups={"Membres de Finances": ["finance"]}, **kw)


def test_sharepoint_listing_content_and_rights(monkeypatch):
    monkeypatch.setattr("ged.connectors.sharepoint.time.sleep", lambda s: None)
    fake = FakeGraph()
    c = sharepoint(fake)
    refs = {r.path: r for r in c.list_documents()}
    assert set(refs) == {"Factures/facture.txt", "budget.txt"}
    acl = c.acl(refs["Factures/facture.txt"])
    # groupe et utilisateur synchronisés AD → SID ; groupe de site → correspondance ; lien ignoré
    assert acl.allow == {SID_COMPTA, SID_ALICE, "finance"}
    acl2 = c.acl(refs["budget.txt"])
    assert acl2.allow == {"entra:g-cloud"}  # cloud seulement : ne correspond à aucun compte AD
    assert c.describe()["unmapped"] == ["Visiteurs de Finances"]  # signalé dans la console
    fake.throttled = True  # 429 : on attend puis on réessaie
    assert c.read_bytes(refs["budget.txt"]) == b"Budget previsionnel 2027 du service"
    assert c.locate("i1").path == "Factures/facture.txt"


def test_sharepoint_subfolder_and_moved_out():
    c = sharepoint(FakeGraph(), folder="Factures")
    assert [r.path for r in c.list_documents()] == ["facture.txt"]
    assert c.locate("i2") is None  # hors du dossier indexé


def test_sharepoint_errors_are_explicit():
    with pytest.raises(PermissionError, match="Entra ID refuse"):
        sharepoint(FakeGraph(), secret="faux")
    with pytest.raises(FileNotFoundError, match="disponibles : Documents, Archives"):
        SharePointConnector("sp", "https://contoso.sharepoint.com/sites/Finances", "Inconnue", tenant_id="tenant-1",
                            client_id="app", client_secret="secret", transport=httpx.MockTransport(FakeGraph().handler))


def test_sharepoint_user_with_ad_rights_finds_document(embedder):
    store = MemoryStore()
    sync_source(sharepoint(FakeGraph()), store, embedder)
    q = "facture Durand"
    alice = frozenset({SID_ALICE, EVERYONE})
    assert store.search(q, embedder.embed([q])[0], alice, k=2)[0].path == "Factures/facture.txt"
    hits = store.search("budget", embedder.embed(["budget"])[0], alice, k=5)
    assert all(h.path != "budget.txt" for h in hits)  # groupe de site non associé + groupe cloud : invisible


# --- Configuration ------------------------------------------------------------------------------

def test_validate_new_types_and_secrets():
    from ged.config import ConfigError, validate_source

    cfg = validate_source({"name": "sp", "type": "sharepoint", "site_url": "https://contoso.sharepoint.com/sites/F",
                           "tenant_id": "t", "client_id": "c", "client_secret_env": "SP_SECRET",
                           "site_groups": {"Membres de F": ["finance", ""]}})
    assert cfg["library"] == "Documents" and cfg["site_groups"] == {"Membres de F": ["finance"]}
    with pytest.raises(ConfigError, match="en clair"):
        validate_source({"name": "sp", "type": "sharepoint", "site_url": "https://x", "tenant_id": "t",
                         "client_id": "c", "client_secret_env": "S", "client_secret": "abc"})
    with pytest.raises(ConfigError, match="mot de passe obligatoire"):
        validate_source({"name": "a", "type": "alfresco", "url": "https://x", "username": "svc"})
    with pytest.raises(ConfigError, match="adresse invalide"):
        validate_source({"name": "a", "type": "cmis", "url": "ged.local", "username": "svc", "password_env": "P"})
    assert json.dumps(validate_source({"name": "a", "type": "cmis", "url": "https://x/cmis/browser",
                                       "username": "svc", "password_env": "P", "repository": "main"}))
