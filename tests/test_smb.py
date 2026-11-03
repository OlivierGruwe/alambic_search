"""Intégration contre un vrai serveur SMB (Samba avec ACL Windows).

Préparer le serveur : sudo tests/samba/setup_samba.sh   (puis GED_TEST_SMB=1 pytest tests/test_smb.py)
"""
import os
import re
import subprocess

import pytest
from fastapi.testclient import TestClient

from ged.api import create_app
from ged.identity import StaticResolver
from ged.ntsecurity import EVERYONE
from ged.store import MemoryStore
from ged.sync import sync_source
from tests.conftest import FakeLLM

pytestmark = pytest.mark.skipif(not os.environ.get("GED_TEST_SMB"), reason="GED_TEST_SMB non défini")

CONF = "/tmp/ged-samba/smb.conf"
PASS = "Test-1234"


def smbcacls(path: str, *args: str) -> None:
    subprocess.run(["smbcacls", "-s", CONF, "-U", f"admin-test%{PASS}", "//127.0.0.1/Commun", path, *args],
                   check=True, capture_output=True)


@pytest.fixture(scope="module")
def sids() -> dict[str, str]:
    out = subprocess.run(["net", "-s", CONF, "groupmap", "list"], check=True, capture_output=True, text=True).stdout
    return {m.group(1): m.group(2) for m in re.finditer(r"^(\S+) \((S-1-[\d-]+)\)", out, re.M)}


@pytest.fixture
def smb():
    from ged.connectors.smb import SmbConnector

    # état de référence des droits modifiés par les tests
    smbcacls("Compta/Factures/facture-acme.txt", "--set",
             r"ACL:FICHIERS\admin-test:ALLOWED/0/FULL,ACL:FICHIERS\svc-alambic:ALLOWED/0/READ,"
             r"ACL:FICHIERS\compta:ALLOWED/0/READ")
    return SmbConnector("commun", "127.0.0.1", "Commun", username="svc-alambic", password=PASS)


@pytest.fixture
def identity(sids):
    return StaticResolver({
        "alice": [sids["compta"]],
        "claire": [sids["compta"], sids["stagiaires"]],  # stagiaire de la compta : refus explicite sur le contrat
        "bob": [sids["rh"]],
    })


def visible(client, login: str, q: str) -> set[str]:
    hits = client.get("/search", params={"q": q, "k": 10}, headers={"X-User": login}).json()
    return {h["path"] for h in hits}


def test_listing_and_ntfs_acls(smb, sids):
    refs = {r.path: r for r in smb.list_documents()}
    assert set(refs) == {"Compta/Factures/facture-acme.txt", "Compta/contrat-durand.txt",
                         "Public/horaires.txt", "RH/salaires.txt"}
    contrat = smb.acl(refs["Compta/contrat-durand.txt"])
    assert sids["compta"] in contrat.allow and contrat.deny == {sids["stagiaires"]}
    assert EVERYONE in smb.acl(refs["Public/horaires.txt"]).allow
    assert sids["rh"] in smb.acl(refs["RH/salaires.txt"]).allow


def test_users_see_only_what_ntfs_allows(smb, identity):
    store = MemoryStore()
    from ged.embeddings import HashEmbedder

    emb = HashEmbedder()
    assert sync_source(smb, store, emb).added == 4
    c = TestClient(create_app(store, emb, FakeLLM(), {"commun": smb}, identity=identity))
    q = "contrat pénalité facture salaires accueil"
    assert visible(c, "alice", q) == {"Compta/Factures/facture-acme.txt", "Compta/contrat-durand.txt",
                                      "Public/horaires.txt"}
    assert visible(c, "claire", q) == {"Compta/Factures/facture-acme.txt", "Public/horaires.txt"}  # refus explicite
    assert visible(c, "bob", q) == {"RH/salaires.txt", "Public/horaires.txt"}
    assert visible(c, "inconnu", q) == set()


def test_acl_change_is_detected_and_download_rechecks(smb, identity, sids):
    from ged.embeddings import HashEmbedder

    store, emb = MemoryStore(), HashEmbedder()
    sync_source(smb, store, emb)
    c = TestClient(create_app(store, emb, FakeLLM(), {"commun": smb}, identity=identity))
    assert "Compta/Factures/facture-acme.txt" not in visible(c, "bob", "facture ACME montant")

    smbcacls("Compta/Factures/facture-acme.txt", "--add", r"ACL:FICHIERS\rh:ALLOWED/0/READ")
    # Samba ne fait pas évoluer ChangeTime sur un changement de droits seul : on force la relecture,
    # comme le fait la synchro complète (ou l'expiration du cache, 24 h par défaut).
    smb.invalidate_acls()
    report = sync_source(smb, store, emb)
    assert report.acl_changed == 1 and report.updated == 0  # droits seuls : pas de réindexation du contenu
    assert "Compta/Factures/facture-acme.txt" in visible(c, "bob", "facture ACME montant")

    # téléchargement : contenu lu via SMB, droits revérifiés en direct
    hit = c.get("/search", params={"q": "pénalité contrat Durand"}, headers={"X-User": "alice"}).json()[0]
    url = hit["url"].split("#")[0]
    r = c.get(url, headers={"X-User": "alice"})
    assert r.status_code == 200 and "pénalité".encode() in r.content
    assert c.get(url, headers={"X-User": "claire"}).status_code == 403
