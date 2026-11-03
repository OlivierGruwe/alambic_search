"""Secrets des sources saisis dans la console : chiffrés en base, jamais réaffichés ni exportés."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from ged.api import create_app
from ged.config import _secret
from ged.secret_box import SecretBox, SecretError, new_key, rotate
from ged.store import MemoryStore

from .conftest import FakeLLM

AUTH = ("admin", "pw")
SECRET = "Tr3s-s3cret!"


class Recorder:
    """Fabrique de connecteurs qui retient le mot de passe reçu (sans se connecter)."""

    def __init__(self):
        self.passwords: dict[str, str | None] = {}

    def __call__(self, cfg):
        from ged.config import validate_source

        runtime = cfg.get("__secrets__")
        checked = validate_source(cfg)
        if runtime:
            checked["__secrets__"] = runtime
        self.passwords[cfg["name"]] = _secret(checked, "password", "test")

        class Empty:
            name = cfg["name"]

            def list_documents(self):
                return iter(())

        return Empty()


def make(store, box, factory):
    app = create_app(store, None, FakeLLM(), {}, admin_password="pw", source_factory=factory, secret_box=box)
    return TestClient(app, follow_redirects=False), app


def ftp(**kw):
    data = {"name": "echanges", "type": "ftp", "host": "ftp.corp.local", "ftp_port": "21", "ftp_username": "svc",
            "tls": "true", "acl": "(racine) = compta"}
    data.update(kw)
    return data


# --- Chiffrement --------------------------------------------------------------------

def test_roundtrip_and_binding_to_source_and_field():
    box = SecretBox(new_key())
    token = box.encrypt("compta", "password", SECRET)
    assert token.startswith(f"v1.{box.key_id}.") and SECRET not in token
    assert box.encrypt("compta", "password", SECRET) != token  # nonce aléatoire
    assert box.decrypt("compta", "password", token) == SECRET
    with pytest.raises(SecretError, match="recopié"):
        box.decrypt("autre", "password", token)
    with pytest.raises(SecretError, match="autre clé"):
        SecretBox(new_key()).decrypt("compta", "password", token)


def test_invalid_key_is_refused():
    with pytest.raises(SecretError, match="32 octets"):
        SecretBox("trop-courte")


def test_key_from_docker_secret_file(tmp_path, monkeypatch):
    key = new_key()
    (tmp_path / "k").write_text(key + "\n")
    monkeypatch.delenv("SECRETS_KEY", raising=False)
    monkeypatch.setenv("SECRETS_KEY_FILE", str(tmp_path / "k"))
    assert SecretBox.from_env().key_id == SecretBox(key).key_id
    monkeypatch.delenv("SECRETS_KEY_FILE")
    assert SecretBox.from_env() is None


def test_rotation_with_previous_key():
    store, old = MemoryStore(), SecretBox(new_key())
    store.set_source_secret("compta", "password", old.encrypt("compta", "password", SECRET))
    old_key = old._current
    import base64

    new = SecretBox(new_key(), [base64.urlsafe_b64encode(old_key).decode()])
    assert rotate(store, new) == (1, [])
    token = store.get_source_secrets("compta")["password"]
    assert not new.needs_rotation(token) and new.decrypt("compta", "password", token) == SECRET
    assert rotate(store, new) == (0, [])


# --- Console ------------------------------------------------------------------------

def test_secret_typed_in_console_is_encrypted_and_never_shown():
    store, box, rec = MemoryStore(), SecretBox(new_key()), Recorder()
    http, app = make(store, box, rec)
    page = http.get("/admin/sources/nouvelle", auth=AUTH).text
    assert 'name="ftp_secret"' in page and 'type="password"' in page

    assert "Source accessible" in http.post("/admin/sources/tester", data=ftp(ftp_secret=SECRET), auth=AUTH).text
    r = http.post("/admin/sources/nouvelle", data=ftp(ftp_secret=SECRET), auth=AUTH)
    assert r.status_code == 303 and rec.passwords["echanges"] == SECRET

    cfg = store.list_source_configs()[0]
    assert cfg["password_chiffre"] is True and SECRET not in json.dumps(cfg) and "__secrets__" not in cfg
    token = store.get_source_secrets("echanges")["password"]
    assert SECRET not in token and box.decrypt("echanges", "password", token) == SECRET

    edit = http.get("/admin/sources/echanges/modifier", auth=AUTH).text
    assert SECRET not in edit and "enregistré — laisser vide pour le garder" in edit
    export = http.get("/admin/sources/export", auth=AUTH).text
    assert SECRET not in export and token not in export and "chiffre:" not in export
    assert "non exportés" in export and "echanges" in export

    # Modifier sans ressaisir : le secret est gardé ; test d'accès aussi
    rec.passwords.clear()
    assert "Source accessible" in http.post("/admin/sources/tester", data=ftp(), auth=AUTH).text
    assert rec.passwords["echanges"] == SECRET
    http.post("/admin/sources/echanges/modifier", data=ftp(ftp_username="svc2"), auth=AUTH)
    assert store.get_source_secrets("echanges")["password"] == token and rec.passwords["echanges"] == SECRET

    # Redémarrage : déchiffré au chargement
    rec2 = Recorder()
    make(store, box, rec2)
    assert rec2.passwords["echanges"] == SECRET


def test_env_variable_wins_and_clear_removes(monkeypatch):
    store, box, rec = MemoryStore(), SecretBox(new_key()), Recorder()
    http, _ = make(store, box, rec)
    http.post("/admin/sources/nouvelle", data=ftp(ftp_secret=SECRET), auth=AUTH)
    monkeypatch.setenv("FTP_PW_TEST", "depuis-env")
    http.post("/admin/sources/echanges/modifier", data=ftp(ftp_password_env="FTP_PW_TEST"), auth=AUTH)
    assert rec.passwords["echanges"] == "depuis-env"

    http.post("/admin/sources/echanges/modifier", data=ftp(ftp_secret_clear="true"), auth=AUTH)
    assert store.get_source_secrets("echanges") == {} and "password_chiffre" not in store.list_source_configs()[0]
    assert rec.passwords["echanges"] is None  # ni variable ni secret : FTP anonyme possible


def test_required_secret_can_come_from_console():
    store, box, rec = MemoryStore(), SecretBox(new_key()), Recorder()
    http, _ = make(store, box, rec)
    ged = {"name": "ged", "type": "alfresco", "url": "https://ged.corp.local", "ged_username": "svc",
           "acl_refresh_hours": "24"}
    r = http.post("/admin/sources/nouvelle", data=ged, auth=AUTH)
    assert r.status_code == 422 and "mot de passe obligatoire" in r.text
    r = http.post("/admin/sources/nouvelle", data={**ged, "ged_secret": SECRET}, auth=AUTH)
    assert r.status_code == 303 and rec.passwords["ged"] == SECRET


def test_without_key_typing_is_refused_and_stored_secret_reported():
    store, box = MemoryStore(), SecretBox(new_key())
    make(store, box, Recorder())[0].post("/admin/sources/nouvelle", data=ftp(ftp_secret=SECRET), auth=AUTH)

    http, app = make(store, None, Recorder())  # SECRETS_KEY retirée
    assert "SECRETS_KEY" in app.state.catalog.errors["echanges"]
    page = http.get("/admin/sources/nouvelle", auth=AUTH).text
    assert 'name="ftp_secret"' not in page and "make secrets-key" in page
    r = http.post("/admin/sources/nouvelle", data=ftp(name="autre", ftp_secret=SECRET), auth=AUTH)
    assert r.status_code == 422 and "SECRETS_KEY" in r.text and SECRET not in r.text


def test_failed_save_never_echoes_the_secret():
    store, box = MemoryStore(), SecretBox(new_key())

    def unreachable(cfg):
        raise OSError("serveur injoignable")

    http, _ = make(store, box, unreachable)
    r = http.post("/admin/sources/nouvelle", data=ftp(ftp_secret=SECRET), auth=AUTH)
    assert r.status_code == 422 and SECRET not in r.text and "à ressaisir" in r.text
    assert store.get_source_secrets("echanges") == {}


def test_delete_source_removes_its_secret():
    store, box = MemoryStore(), SecretBox(new_key())
    http, _ = make(store, box, Recorder())
    http.post("/admin/sources/nouvelle", data=ftp(ftp_secret=SECRET), auth=AUTH)
    http.post("/admin/sources/echanges/supprimer", data={"confirm": "echanges"}, auth=AUTH)
    assert store.list_all_source_secrets() == []
