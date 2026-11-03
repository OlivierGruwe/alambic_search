"""Sources ajoutées, modifiées, supprimées depuis la console, sans redémarrage."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ged.api import create_app
from ged.store import MemoryStore

from .conftest import ACL, FakeLLM

AUTH = ("admin", "pw")


@pytest.fixture
def app_parts(connector, embedder):
    store = MemoryStore()
    return store, embedder, connector


def make(store, embedder, connectors):
    return TestClient(create_app(store, embedder, FakeLLM(), connectors, admin_password="pw"), follow_redirects=False)


def form(share: Path, **kw) -> dict:
    data = {"name": "compta", "type": "filesystem", "root": str(share / "Compta"),
            "acl": "(racine) = compta\nFactures = compta, direction", "synchro_minutes": ""}
    data.update(kw)
    return data


def test_add_test_and_sync_a_folder(share, app_parts):
    store, embedder, connector = app_parts
    http = make(store, embedder, {connector.name: connector})
    assert "Ajouter une source" in http.get("/admin/sources", auth=AUTH).text

    test = http.post("/admin/sources/tester", data=form(share), auth=AUTH).text
    assert "Source accessible" in test and "2 fichiers" in test

    r = http.post("/admin/sources/nouvelle", data={**form(share), "sync_now": "true"}, auth=AUTH)
    assert r.status_code == 303 and r.headers["location"] == "/admin/sources#src-compta"
    assert store.list_source_configs()[0]["acl"] == {"": ["compta"], "Factures": ["compta", "direction"]}

    page = http.get("/admin/sources", auth=AUTH).text
    assert "/admin/sources/compta/modifier" in page
    assert f"/admin/sources/{connector.name}/modifier" not in page  # source fournie par le code (tests)
    for _ in range(100):
        if store.list_runs("compta") and store.list_runs("compta")[0].status != "running":
            break
        import time
        time.sleep(0.05)
    rows, total = store.list_documents(source="compta")
    assert total == 2


def test_console_sources_reload_after_restart(share, app_parts):
    store, embedder, connector = app_parts
    make(store, embedder, {}).post("/admin/sources/nouvelle", data=form(share, synchro_minutes="30"), auth=AUTH)
    http = make(store, embedder, {})  # « redémarrage » : même base
    page = http.get("/admin/sources", auth=AUTH).text
    assert "compta" in page and "Automatique, toutes les 30 min" in page


def test_validation_errors_keep_the_form(share, app_parts):
    store, embedder, connector = app_parts
    http = make(store, embedder, {connector.name: connector})
    r = http.post("/admin/sources/nouvelle", data=form(share, name="Pas Bon!"), auth=AUTH)
    assert r.status_code == 422 and "minuscules" in r.text and "Factures = compta, direction" in r.text
    r = http.post("/admin/sources/nouvelle", data=form(share, root=str(share / "nexiste-pas")), auth=AUTH)
    assert r.status_code == 422 and "introuvable" in r.text
    r = http.post("/admin/sources/nouvelle", data=form(share, name=connector.name), auth=AUTH)
    assert r.status_code == 422 and "existe déjà" in r.text  # pas d'écrasement
    r = http.post("/admin/sources/nouvelle", data=form(share, synchro_minutes="2"), auth=AUTH)
    assert r.status_code == 422 and "au moins 5" in r.text
    assert store.list_source_configs() == []


def test_smb_password_is_never_stored(app_parts):
    store, embedder, _ = app_parts
    http = make(store, embedder, {})
    page = http.get("/admin/sources/nouvelle", auth=AUTH).text
    assert 'name="password_env"' in page and 'type="password"' not in page
    r = http.post("/admin/sources/nouvelle", auth=AUTH, data={
        "name": "commun", "type": "smb", "server": "fs", "share": "Commun", "password_env": "VARIABLE_ABSENTE"})
    assert r.status_code == 422 and "VARIABLE_ABSENTE" in r.text and store.list_source_configs() == []


def test_edit_keeps_name_and_applies_live(share, app_parts):
    store, embedder, connector = app_parts
    http = make(store, embedder, {})
    http.post("/admin/sources/nouvelle", data=form(share), auth=AUTH)
    page = http.get("/admin/sources/compta/modifier", auth=AUTH).text
    assert "readonly" in page and "Factures = compta, direction" in page
    r = http.post("/admin/sources/compta/modifier", data=form(share, acl="(racine) = direction"), auth=AUTH)
    assert r.status_code == 303
    assert store.list_source_configs()[0]["acl"] == {"": ["direction"]}
    r = http.post("/admin/sources/compta/modifier", data=form(share, name="autre"), auth=AUTH)
    assert r.status_code == 422 and "ne se modifie pas" in r.text
    assert http.get(f"/admin/sources/{connector.name}/modifier", auth=AUTH).status_code == 404


def test_delete_requires_typed_name_and_purges_index(share, app_parts, embedder):
    from ged.sync import sync_source

    store, embedder, _ = app_parts
    http = make(store, embedder, {})
    http.post("/admin/sources/nouvelle", data=form(share), auth=AUTH)
    from ged.connectors import FileSystemConnector

    sync_source(FileSystemConnector("compta", share / "Compta", {"": ["compta"]}), store, embedder)
    assert store.list_documents(source="compta")[1] == 2
    assert "2 documents de cette source seront retirés" in http.get("/admin/sources/compta/supprimer", auth=AUTH).text

    r = http.post("/admin/sources/compta/supprimer", data={"confirm": "non"}, auth=AUTH)
    assert "Recopiez exactement" in r.text and store.list_source_configs()
    r = http.post("/admin/sources/compta/supprimer", data={"confirm": "compta"}, auth=AUTH)
    assert r.status_code == 303
    assert store.list_source_configs() == [] and store.list_documents(source="compta")[1] == 0
    assert (share / "Compta" / "contrat-fournisseur.docx").exists()  # fichiers jamais touchés


def test_cross_site_post_is_refused(share, app_parts):
    store, embedder, _ = app_parts
    http = make(store, embedder, {})
    r = http.post("/admin/sources/nouvelle", data=form(share), auth=AUTH,
                  headers={"origin": "https://pirate.example"})
    assert r.status_code == 403 and store.list_source_configs() == []
    r = http.post("/admin/sources/nouvelle", data=form(share), auth=AUTH, headers={"origin": "http://testserver"})
    assert r.status_code == 303


def test_unreachable_console_source_at_startup_stays_editable(share, app_parts):
    store, embedder, _ = app_parts
    store.save_source_config({"name": "ancien", "type": "filesystem", "root": str(share / "disparu"), "acl": {}})
    http = make(store, embedder, {})
    page = http.get("/admin/sources", auth=AUTH).text
    assert "Source inaccessible au démarrage" in page and "/admin/sources/ancien/modifier" in page
    assert ACL  # même règles que les autres tests


def test_first_start_imports_sources_yaml_once(share, app_parts):
    store, embedder, _ = app_parts
    yaml_sources = [{"name": "partage-commun", "type": "filesystem", "root": str(share), "acl": {"": ["direction"]},
                     "synchro_minutes": 60}]

    def app():
        return TestClient(create_app(store, embedder, FakeLLM(), {}, admin_password="pw", yaml_sources=yaml_sources),
                          follow_redirects=False)

    http = app()
    assert [c["name"] for c in store.list_source_configs()] == ["partage-commun"]
    page = http.get("/admin/sources", auth=AUTH).text
    assert "/admin/sources/partage-commun/modifier" in page and "Automatique, toutes les 60 min" in page

    # supprimée dans la console : NE revient PAS au redémarrage (le fichier n'est plus lu pour les sources)
    http.post("/admin/sources/partage-commun/supprimer", data={"confirm": "partage-commun"}, auth=AUTH)
    http = app()
    assert store.list_source_configs() == []
    page = http.get("/admin/sources", auth=AUTH).text
    assert "absente de la console : partage-commun" in page  # mais signalée, importable

    page = http.post("/admin/sources/importer", auth=AUTH).text
    assert "Importée de sources.yaml : partage-commun" in page
    assert [c["name"] for c in store.list_source_configs()] == ["partage-commun"]


def test_yaml_changes_after_import_do_not_override_console(share, app_parts):
    store, embedder, _ = app_parts
    first = [{"name": "p", "type": "filesystem", "root": str(share), "acl": {"": ["direction"]}}]
    make_app = lambda ys: create_app(store, embedder, FakeLLM(), {}, admin_password="pw", yaml_sources=ys)  # noqa: E731
    make_app(first)
    store.save_source_config({**store.list_source_configs()[0], "acl": {"": ["compta"]}})  # modifiée dans la console
    make_app([{**first[0], "acl": {"": ["rh"]}}])  # le fichier a changé depuis
    assert store.list_source_configs()[0]["acl"] == {"": ["compta"]}  # la base fait foi


def test_export_yaml_roundtrip(share, app_parts):
    import yaml

    from ged.config import validate_source

    store, embedder, _ = app_parts
    http = make(store, embedder, {})
    http.post("/admin/sources/nouvelle", data=form(share, synchro_minutes="30"), auth=AUTH)
    r = http.get("/admin/sources/export", auth=AUTH)
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    data = yaml.safe_load(r.text)
    assert [validate_source(src)["name"] for src in data["sources"]] == ["compta"]
    assert data["sources"][0]["synchro_minutes"] == 30
    assert "password_env" in r.text  # rappel en tête : aucun mot de passe exporté


class _Captured:
    """Connecteur factice : garde la définition reçue (pas de réseau dans les tests du formulaire)."""

    def __init__(self, cfg):
        from ged.config import validate_source

        self.cfg = validate_source(cfg)
        self.name = self.cfg["name"]

    def list_documents(self):
        return iter(())

    def describe(self):
        return {"type": self.cfg["type"], "location": "-", "extensions": []}


@pytest.mark.parametrize("data, expected", [
    ({"type": "sharepoint", "site_url": "https://contoso.sharepoint.com/sites/F", "library": "", "folder": "Factures",
      "tenant_id": "t", "client_id": "c", "client_secret_env": "SP_SECRET",
      "site_groups": "Membres de F = finance, direction\nVisiteurs de F = tous"},
     {"library": "Documents", "folder": "Factures",
      "site_groups": {"Membres de F": ["finance", "direction"], "Visiteurs de F": ["tous"]}}),
    ({"type": "alfresco", "url": "https://ged.test", "ged_root": "Sites/compta/documentLibrary",
      "ged_username": "svc", "ged_password_env": "ALF_PW"},
     {"root": "Sites/compta/documentLibrary", "username": "svc", "password_env": "ALF_PW"}),
    ({"type": "cmis", "url": "https://ged.test/cmis/browser", "repository": "main", "ged_root": "",
      "ged_username": "svc", "ged_password_env": "CMIS_PW"},
     {"repository": "main", "username": "svc"}),
])
def test_form_for_api_sources(store, embedder, data, expected):
    http = TestClient(create_app(store, embedder, FakeLLM(), {}, admin_password="pw", source_factory=_Captured),
                      follow_redirects=False)
    r = http.post("/admin/sources/nouvelle", data={"name": "ged", "acl_refresh_hours": "12", **data}, auth=AUTH)
    assert r.status_code == 303, r.text
    saved = store.list_source_configs()[0]
    assert saved["acl_refresh_hours"] == 12
    for k, v in expected.items():
        assert saved[k] == v
    page = http.get("/admin/sources/ged/modifier", auth=AUTH).text
    assert 'role="tooltip"' in page  # aides présentes
    if data["type"] == "sharepoint":
        assert "Membres de F = finance, direction" in page


def test_missing_secret_variable_is_explained(store, embedder):
    http = TestClient(create_app(store, embedder, FakeLLM(), {}, admin_password="pw"))
    html = http.post("/admin/sources/tester", auth=AUTH, data={
        "name": "sp", "type": "sharepoint", "site_url": "https://contoso.sharepoint.com/sites/F",
        "tenant_id": "t", "client_id": "c", "client_secret_env": "VARIABLE_ABSENTE"}).text
    assert "VARIABLE_ABSENTE" in html and "pas définie" in html
