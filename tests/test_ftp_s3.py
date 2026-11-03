"""Sources FTP (vrai serveur pyftpdlib, en local) et S3 (moto, qui imite l'API d'Amazon S3)."""
from __future__ import annotations

import socket
import threading
from pathlib import Path

import pytest

from ged.connectors.ftp import FtpConnector
from ged.connectors.s3 import S3Connector
from ged.store import MemoryStore
from ged.sync import sync_source

RULES = {"": ["direction"], "Compta": ["compta", "direction"]}


# --- FTP ---------------------------------------------------------------------------------------

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def ftp_server(tmp_path: Path):
    from pyftpdlib.authorizers import DummyAuthorizer
    from pyftpdlib.handlers import FTPHandler
    from pyftpdlib.servers import FTPServer

    root = tmp_path / "ftp"
    (root / "Echanges" / "Compta").mkdir(parents=True)
    (root / "Echanges" / "Compta" / "releve.txt").write_text("Releve bancaire de novembre, solde crediteur", encoding="utf-8")
    (root / "Echanges" / "lisez-moi.txt").write_text("Depot des fichiers fournisseurs chaque lundi", encoding="utf-8")
    (root / "Echanges" / "image.bmp").write_bytes(b"x")
    auth = DummyAuthorizer()
    auth.add_user("svc", "pw", str(root), perm="elr")  # lecture seule
    handler = type("H", (FTPHandler,), {"authorizer": auth})
    port = _free_port()
    server = FTPServer(("127.0.0.1", port), handler)
    t = threading.Thread(target=server.serve_forever, kwargs={"timeout": 0.1}, daemon=True)
    t.start()
    yield port, root
    server.close_all()


def ftp(port, **kw):
    return FtpConnector("ftp", "127.0.0.1", "Echanges", username="svc", password=kw.pop("password", "pw"), port=port,
                        tls=False, acl_rules=RULES, extensions=frozenset({".txt"}), **kw)


def test_ftp_listing_reading_rights(ftp_server):
    port, _ = ftp_server
    c = ftp(port)
    refs = {r.path: r for r in c.list_documents()}
    assert set(refs) == {"Compta/releve.txt", "lisez-moi.txt"}
    assert refs["Compta/releve.txt"].mtime > 0 and refs["Compta/releve.txt"].size > 0
    assert c.acl(refs["Compta/releve.txt"]).allow == {"compta", "direction"}
    assert c.acl(refs["lisez-moi.txt"]).allow == {"direction"}
    assert c.read_bytes(refs["lisez-moi.txt"]).startswith(b"Depot des fichiers")
    ref = refs["Compta/releve.txt"]
    assert c.locate(ref.doc_id, hint_path=ref.path).path == "Compta/releve.txt"
    assert "ftp://127.0.0.1" in c.describe()["location"] and "non chiffré" in c.describe()["type"]


def test_ftp_wrong_password_and_folder(ftp_server):
    port, _ = ftp_server
    with pytest.raises(PermissionError, match="refuse le compte"):
        ftp(port, password="faux")
    with pytest.raises(FileNotFoundError, match="introuvable"):
        FtpConnector("ftp", "127.0.0.1", "Inexistant", username="svc", password="pw", port=port, tls=False)


def test_ftp_end_to_end_and_reconnection(ftp_server, embedder):
    port, root = ftp_server
    c = ftp(port)
    store = MemoryStore()
    report = sync_source(c, store, embedder)
    assert report.added == 2 and not report.errors
    c._ftp.close()  # connexion perdue (serveur redémarré, délai d'inactivité) : rétablie toute seule
    (root / "Echanges" / "nouveau.txt").write_text("Nouvelle procedure de depot des factures", encoding="utf-8")
    report = sync_source(c, store, embedder)
    assert report.added == 1 and report.unchanged == 2


# --- S3 ------------------------------------------------------------------------------------------

@pytest.fixture
def s3():
    import boto3
    from moto import mock_aws

    with mock_aws():
        client = boto3.client("s3", region_name="eu-west-3", aws_access_key_id="AK", aws_secret_access_key="SK")
        client.create_bucket(Bucket="archives", CreateBucketConfiguration={"LocationConstraint": "eu-west-3"})
        for key, body in {
            "ged/Compta/facture-durand.txt": "Facture Durand, maintenance annuelle 1200 euros",
            "ged/Compta/": "",  # « dossier » créé par une console S3
            "ged/notes.txt": "Notes de la reunion du comite de direction",
            "ged/.cache/x.txt": "cache technique a ignorer completement",
            "autre/hors-prefixe.txt": "hors du prefixe indexe, jamais lu",
        }.items():
            client.put_object(Bucket="archives", Key=key, Body=body.encode())
        yield client


def test_s3_listing_reading_rights(s3):
    c = S3Connector("s3", "archives", "ged", acl_rules=RULES, extensions=frozenset({".txt"}), client=s3)
    refs = {r.path: r for r in c.list_documents()}
    assert set(refs) == {"Compta/facture-durand.txt", "notes.txt"}
    assert refs["notes.txt"].doc_id == "ged/notes.txt"
    assert c.acl(refs["Compta/facture-durand.txt"]).allow == {"compta", "direction"}
    assert c.read_bytes(refs["notes.txt"]).startswith(b"Notes de la reunion")
    assert c.locate("ged/notes.txt").path == "notes.txt"
    assert c.locate("autre/hors-prefixe.txt") is None and c.locate("ged/absent.txt") is None
    assert c.describe()["location"].endswith("s3://archives/ged/")


def test_s3_paging_and_errors(s3):
    for i in range(1100):  # plus d'une page (1 000 clés par page)
        s3.put_object(Bucket="archives", Key=f"ged/lot/{i:04d}.txt", Body=b"contenu")
    c = S3Connector("s3", "archives", "ged", acl_rules=RULES, extensions=frozenset({".txt"}), client=s3)
    assert sum(1 for _ in c.list_documents()) == 1102
    with pytest.raises(FileNotFoundError, match="Bucket introuvable"):
        S3Connector("s3", "inexistant", client=s3)


def test_s3_end_to_end(s3, embedder):
    store = MemoryStore()
    c = S3Connector("s3", "archives", "ged", acl_rules=RULES, extensions=frozenset({".txt"}), client=s3)
    sync_source(c, store, embedder)
    q = "facture Durand maintenance"
    assert store.search(q, embedder.embed([q])[0], frozenset({"compta"}), k=1)[0].path == "Compta/facture-durand.txt"
    hits = store.search(q, embedder.embed([q])[0], frozenset({"rh"}), k=5)
    assert hits == []


# --- Configuration --------------------------------------------------------------------------------

def test_validate_ftp_s3():
    from ged.config import ConfigError, validate_source

    f = validate_source({"name": "f", "type": "ftp", "host": "ftp.corp.local", "password_env": "FTP_PW",
                         "acl": {"": ["tous"]}})
    assert f["port"] == 21 and f["tls"] is True and f["username"] == "anonymous"
    s = validate_source({"name": "s", "type": "s3", "bucket": "b", "endpoint_url": "https://s3.garage.local",
                         "access_key_id": "GK1", "secret_access_key_env": "S3_SECRET"})
    assert s["path_style"] is True  # stockage compatible : adressage par chemin par défaut
    assert validate_source({"name": "s", "type": "s3", "bucket": "b"})["path_style"] is False  # Amazon
    with pytest.raises(ConfigError, match="secret obligatoire avec une clé"):
        validate_source({"name": "s", "type": "s3", "bucket": "b", "access_key_id": "GK1"})
    with pytest.raises(ConfigError, match="en clair"):
        validate_source({"name": "f", "type": "ftp", "host": "h", "password": "x"})


def test_console_adds_ftp_source_and_syncs(ftp_server, embedder, monkeypatch):
    from fastapi.testclient import TestClient

    from ged.api import create_app
    from tests.conftest import FakeLLM

    port, _ = ftp_server
    monkeypatch.setenv("FTP_PASSWORD", "pw")
    store = MemoryStore()
    http = TestClient(create_app(store, embedder, FakeLLM(), {}, admin_password="pw"), follow_redirects=False)
    data = {"name": "echanges", "type": "ftp", "host": "127.0.0.1", "ftp_port": str(port), "ftp_root": "/Echanges",
            "ftp_username": "svc", "ftp_password_env": "FTP_PASSWORD", "acl": "(racine) = direction\nCompta = compta"}
    test = http.post("/admin/sources/tester", data=data, auth=("admin", "pw")).text
    assert "Source accessible" in test and "fichiers" in test
    assert http.post("/admin/sources/nouvelle", data=data, auth=("admin", "pw")).status_code == 303
    saved = store.list_source_configs()[0]
    assert saved["tls"] is False and saved["acl"] == {"": ["direction"], "Compta": ["compta"]}
    assert "FTP (non chiffré)" in http.get("/admin/sources", auth=("admin", "pw")).text


def test_console_form_s3_fields(embedder):
    from fastapi.testclient import TestClient

    from ged.api import create_app
    from tests.conftest import FakeLLM
    from tests.test_sources_admin import _Captured

    store = MemoryStore()
    http = TestClient(create_app(store, embedder, FakeLLM(), {}, admin_password="pw", source_factory=_Captured),
                      follow_redirects=False)
    r = http.post("/admin/sources/nouvelle", auth=("admin", "pw"), data={
        "name": "archives", "type": "s3", "bucket": "archives", "prefix": "ged", "region": "garage",
        "endpoint_url": "http://garage:3900", "access_key_id": "GK1", "secret_access_key_env": "S3_SECRET",
        "acl": "(racine) = direction"})
    assert r.status_code == 303, r.text
    saved = store.list_source_configs()[0]
    assert saved["path_style"] is True and saved["endpoint_url"] == "http://garage:3900"
    page = http.get("/admin/sources/archives/modifier", auth=("admin", "pw")).text
    assert 'value="archives"' in page and "(racine) = direction" in page
