"""Comptes, connexion, rôles et clés d'API : les droits viennent du compte, jamais de l'appelant."""
from __future__ import annotations

import base64
import json

import pytest
from fastapi.testclient import TestClient

from ged.accounts import Accounts, hash_password, parse_key, verify_password
from ged.api import create_app
from ged.identity import StaticResolver
from ged.models import Role
from ged.session import COOKIE, LoginThrottle, Sessions
from ged.sync import sync_source

from .conftest import FakeLLM

PW = "mot-de-passe-solide"
ORIGIN = {"Origin": "http://testserver"}


@pytest.fixture(autouse=True)
def no_header_trust(monkeypatch):
    monkeypatch.setenv("TRUST_IDENTITY_HEADERS", "false")  # comportement de production


@pytest.fixture
def http(connector, store, embedder):
    sync_source(connector, store, embedder)
    identity = StaticResolver({"alice.ad": ["compta"], "bob.ad": ["rh"]})
    app = create_app(store, embedder, FakeLLM("Pénalité de 2 % [1]."), {connector.name: connector},
                     identity=identity, session_secret="test-secret", admin_password="admin-pw-123")
    accounts = Accounts(store, identity)
    accounts.create("alice", "Alice Martin", Role.USER, PW, ad_login="alice.ad")
    accounts.create("bob", "Bob", Role.USER, PW, groups=["rh"])
    accounts.create("gestion", "Gestion", Role.MANAGER, PW)
    return TestClient(app, follow_redirects=False)


def login(http, user="alice", password=PW, suite="/"):
    return http.post("/connexion", data={"identifiant": user, "mot_de_passe": password, "suite": suite},
                     headers=ORIGIN)


def basic(user, password=PW):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}


def test_password_hash_and_key_format():
    h = hash_password("secret-123456")
    assert h.startswith("scrypt$") and "secret" not in h
    assert verify_password("secret-123456", h) and not verify_password("autre", h)
    assert not verify_password("x", "n'importe quoi")
    assert parse_key("als_0a1b2c3d_" + "x" * 40) == ("0a1b2c3d", "x" * 40)
    assert parse_key("Bearer truc") is None and parse_key("als_court_x") is None


def test_bootstrap_admin_only_when_no_account(store):
    a = Accounts(store)
    assert a.bootstrap(None) is None and store.list_users() == []
    admin = a.bootstrap("premier-mdp")
    assert admin.login == "admin" and admin.role == Role.ADMIN and a.authenticate("admin", "premier-mdp")
    assert a.bootstrap("autre") is None  # déjà des comptes : rien ne change
    assert not a.authenticate("admin", "autre")


def test_home_requires_login(http):
    r = http.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/connexion"
    assert "Avec votre compte Alambic Search" in http.get("/connexion").text


def test_login_then_search_with_own_rights(http, store):
    r = login(http)
    assert r.status_code == 303 and r.headers["location"] == "/"
    cookie = r.cookies.get(COOKIE)
    assert cookie and PW not in cookie  # jamais le mot de passe
    set_cookie = r.headers["set-cookie"].lower()
    assert "httponly" in set_cookie and "samesite=lax" in set_cookie
    assert store.get_user("alice").last_login is not None
    assert http.get("/").headers["location"] == "/rechercher"
    page = http.get("/rechercher").text
    assert "Alice Martin" in page and "Se déconnecter" in page and "Mon compte" in page
    assert 'href="/admin"' not in page  # utilisateur : pas de console

    html = http.post("/rechercher", data={"question": "pénalité de retard", "formuler": "true"}).text
    assert "Pénalité de 2 %" in html and "contrat-fournisseur.docx" in html  # compta, par l'AD rattaché
    assert "Réponse en " in html  # temps affiché
    html = http.post("/rechercher", data={"question": "grille des salaires", "formuler": "false"}).text
    assert "salaires.txt" not in html  # RH : pas pour alice


def test_manual_groups_give_rights(http):
    login(http, "bob")  # bob : pas d'AD, groupe « rh » saisi
    hits = http.get("/search", params={"q": "grille des salaires"}).json()
    assert hits and all(h["path"].startswith("RH/") for h in hits)


def test_api_ignores_identity_headers(http):
    assert http.get("/search", params={"q": "salaires"}, headers={"X-User-Groups": "rh"}).status_code == 401
    assert http.post("/ask", json={"question": "salaires ?"}, headers={"X-User": "bob"}).status_code == 401
    login(http)
    hits = http.get("/search", params={"q": "grille des salaires"}, headers={"X-User-Groups": "rh"}).json()
    assert not any(h["path"].startswith("RH/") for h in hits)  # droits d'alice, en-tête ignoré


def test_api_with_basic_auth(http):
    r = http.get("/search", params={"q": "grille des salaires"}, headers=basic("bob"))
    assert r.status_code == 200 and any(h["path"].startswith("RH/") for h in r.json())
    r = http.get("/search", params={"q": "x"}, headers=basic("bob", "faux"))
    assert r.status_code == 401 and "Basic" in r.headers["www-authenticate"]
    for _ in range(5):
        http.get("/search", params={"q": "x"}, headers=basic("alice", "faux"))
    assert http.get("/search", params={"q": "x"}, headers=basic("alice")).status_code == 429  # freiné


def test_api_keys_lifecycle(http, store):
    login(http, "bob")
    page = http.post("/compte/cles", data={"nom": "Intranet", "jours": "30"}, headers=ORIGIN).text
    key = page.split('<code id="new-key">', 1)[1].split("<", 1)[0]
    assert key.startswith("als_") and "Copiez cette clé maintenant" in page
    stored = store.list_api_keys(store.get_user("bob").id)[0]
    assert stored.name == "Intranet" and key.split("_")[2] not in stored.key_hash  # seule l'empreinte

    api = TestClient(http.app)
    for headers in ({"Authorization": f"Bearer {key}"}, {"X-API-Key": key}):
        hits = api.get("/search", params={"q": "grille des salaires"}, headers=headers).json()
        assert hits and all(h["path"].startswith("RH/") for h in hits)  # droits de bob
    assert api.post("/ask", json={"question": "grille des salaires", "formuler": False},
                    headers={"X-API-Key": key}).status_code == 200
    assert store.get_api_key(stored.prefix).last_used is not None
    assert api.get("/search", params={"q": "x"}, headers={"X-API-Key": key[:-2] + "zz"}).status_code == 401

    assert http.post(f"/compte/cles/{stored.id}/revoquer", headers=ORIGIN).status_code == 200
    r = api.get("/search", params={"q": "x"}, headers={"X-API-Key": key})
    assert r.status_code == 401 and "révoquée" in r.json()["detail"]


def test_expired_key_and_disabled_account(store, http):
    accounts = Accounts(store)
    bob = store.get_user("bob")
    _, key = accounts.new_api_key(bob, "court", days=1)
    api = TestClient(http.app)
    assert api.get("/search", params={"q": "x"}, headers={"X-API-Key": key}).status_code == 200
    k = store.list_api_keys(bob.id)[0]
    k.expires_at = 1.0
    store.api_keys[k.id] = k  # MemoryStore : échéance passée
    assert api.get("/search", params={"q": "x"}, headers={"X-API-Key": key}).status_code == 401
    _, key2 = accounts.new_api_key(bob, "autre")
    login(http, "bob")
    bob.active = False
    store.update_user(bob)
    assert api.get("/search", params={"q": "x"}, headers={"X-API-Key": key2}).status_code == 401
    assert http.get("/rechercher").status_code == 303  # session fermée aussitôt
    assert login(http, "bob").status_code == 401


def test_change_password_closes_other_sessions(http, store):
    other = TestClient(http.app, follow_redirects=False)
    login(other)
    login(http)
    r = http.post("/compte/mot-de-passe", data={"actuel": "faux", "nouveau": "x" * 12, "confirmation": "x" * 12},
                  headers=ORIGIN)
    assert "Mot de passe actuel incorrect" in r.text
    r = http.post("/compte/mot-de-passe", data={"actuel": PW, "nouveau": "court", "confirmation": "court"},
                  headers=ORIGIN)
    assert "trop court" in r.text
    r = http.post("/compte/mot-de-passe", data={"actuel": PW, "nouveau": "nouveau-mdp-1", "confirmation": "nouveau-mdp-1"},
                  headers=ORIGIN)
    assert "Mot de passe changé" in r.text
    assert http.get("/rechercher").status_code == 200  # cette session continue
    assert other.get("/rechercher").status_code == 303  # l'autre est fermée
    assert login(http, password="nouveau-mdp-1").status_code == 303


def test_roles_gate_console_and_users(http):
    login(http)  # utilisateur
    assert http.get("/admin", headers={"accept": "application/json"}).status_code == 403
    login(http, "gestion")
    assert http.get("/admin").status_code == 200
    assert 'href="/admin/utilisateurs"' not in http.get("/admin").text
    assert http.get("/admin/utilisateurs").status_code == 403
    assert http.post("/sources/partage/sync", headers=basic("alice")).status_code == 403
    assert http.post("/sources/partage/sync", headers=basic("gestion")).status_code == 200
    login(http, "admin", "admin-pw-123")
    assert 'href="/admin/utilisateurs"' in http.get("/admin").text


def test_console_redirects_browser_to_login(http):
    r = http.get("/admin/documents?p=2", headers={"accept": "text/html"})
    assert r.status_code == 303 and r.headers["location"] == "/connexion?suite=/admin/documents%3Fp%3D2"
    assert http.get("/admin").status_code == 401  # client d'API : 401


def test_admin_manages_accounts(http, store):
    login(http, "admin", "admin-pw-123")
    r = http.post("/admin/utilisateurs", headers=ORIGIN, data={
        "login": "carol", "nom": "Carol", "role": "gestionnaire", "mot_de_passe": "carol-mdp-123",
        "ad_login": "", "groupes": "compta, direction"})
    assert r.status_code == 303
    carol = store.get_user("carol")
    assert carol.role == Role.MANAGER and carol.groups == ["compta", "direction"]
    assert r.headers["location"] == f"/admin/utilisateurs/{carol.id}?cree=1"
    page = http.get(r.headers["location"]).text
    assert "Compte créé" in page and "direction" in page
    # identifiant déjà pris, mot de passe trop court
    r = http.post("/admin/utilisateurs", headers=ORIGIN, data={"login": "carol", "role": "utilisateur"})
    assert "déjà pris" in r.text
    r = http.post("/admin/utilisateurs", headers=ORIGIN, data={"login": "dan", "mot_de_passe": "court"})
    assert "trop court" in r.text and store.get_user("dan") is None
    # modification, désactivation, clé pour une application, suppression
    http.post(f"/admin/utilisateurs/{carol.id}", headers=ORIGIN,
              data={"nom": "Carol D.", "role": "utilisateur", "ad_login": "alice.ad", "groupes": ""})
    carol = store.get_user("carol")
    assert carol.role == Role.USER and not carol.active and carol.ad_login == "alice.ad" and carol.groups == []
    page = http.post(f"/admin/utilisateurs/{carol.id}/cles", headers=ORIGIN, data={"nom": "Export"}).text
    assert '<code id="new-key">als_' in page
    http.post(f"/admin/utilisateurs/{carol.id}/supprimer", headers=ORIGIN)
    assert store.get_user("carol") is None and store.list_api_keys(carol.id) == []
    assert "alice" in http.get("/admin/utilisateurs").text


def test_admin_cannot_lock_everyone_out(http, store):
    login(http, "admin", "admin-pw-123")
    me = store.get_user("admin")
    r = http.post(f"/admin/utilisateurs/{me.id}", headers=ORIGIN, data={"nom": "A", "role": "utilisateur", "actif": "true"})
    assert "propre rôle" in r.text and store.get_user("admin").role == Role.ADMIN
    assert "propre compte" in http.post(f"/admin/utilisateurs/{me.id}/supprimer", headers=ORIGIN).text
    other = TestClient(http.app, follow_redirects=False)
    accounts = Accounts(store)
    accounts.create("admin2", "", Role.ADMIN, PW)
    login(other, "admin2")
    # admin2 peut retirer admin… mais pas le dernier administrateur
    other.post(f"/admin/utilisateurs/{me.id}", headers=ORIGIN, data={"nom": "A", "role": "utilisateur", "actif": "true"})
    assert store.get_user("admin").role == Role.USER
    a2 = store.get_user("admin2")
    login(http, "admin2")
    with pytest.raises(Exception, match="dernier"):
        accounts.guard_last_admin(a2, Role.USER)


def test_admin_resets_password(http, store):
    login(http, "admin", "admin-pw-123")
    alice = store.get_user("alice")
    other = TestClient(http.app, follow_redirects=False)
    login(other)
    r = http.post(f"/admin/utilisateurs/{alice.id}/mot-de-passe", headers=ORIGIN, data={"mot_de_passe": "remis-a-zero-1"})
    assert "Mot de passe défini" in r.text
    assert other.get("/rechercher").status_code == 303
    assert login(other, password="remis-a-zero-1").status_code == 303


def test_document_link_requires_login_then_returns(http, store):
    doc = store.list_documents(q="contrat-fournisseur")[0][0]
    url = f"/documents/partage/{doc.doc_id}"
    r = http.get(url, headers={"accept": "text/html"})
    assert r.status_code == 303 and r.headers["location"].startswith("/connexion?suite=/documents/partage/")
    r = login(http, suite=url)
    assert r.headers["location"] == url
    assert http.get(url).status_code == 200
    login(http, "bob")
    assert http.get(url).status_code == 403  # droits revérifiés pour le compte connecté


def test_bad_password_and_open_redirect(http):
    r = login(http, password="non")
    assert r.status_code == 401 and "Identifiant ou mot de passe incorrect" in r.text
    assert COOKIE not in r.cookies
    assert login(http, "inconnu").status_code == 401
    assert login(http, suite="https://pirate.example/").headers["location"] == "/"
    assert login(http, suite="//pirate.example/").headers["location"] == "/"


def test_login_form_rejects_other_origin(http):
    r = http.post("/connexion", data={"identifiant": "alice", "mot_de_passe": PW},
                  headers={"Origin": "https://pirate.example"})
    assert r.status_code == 403


def test_throttle_after_repeated_failures(http):
    for _ in range(5):
        assert login(http, password="non").status_code == 401
    r = login(http)  # bon mot de passe, mais bloqué
    assert r.status_code == 429 and "quart d" in r.text


def test_logout(http):
    login(http)
    r = http.post("/deconnexion")
    assert r.status_code == 303 and r.headers["location"] == "/connexion?deconnecte=1"
    http.cookies.clear()
    assert http.get("/").status_code == 303


def test_tampered_or_expired_cookie_is_rejected():
    s = Sessions("k", hours=8)
    token = s.issue("alice", "Alice")
    assert s.read(token)["u"] == "alice"
    body, sig = token.rsplit(".", 1)
    forged = base64.urlsafe_b64encode(json.dumps({"u": "admin", "n": "x", "exp": 9e12}).encode()).decode().rstrip("=")
    assert s.read(f"{forged}.{sig}") is None
    assert Sessions("autre-cle").read(token) is None
    assert Sessions("k", hours=-1).read(Sessions("k", hours=-1).issue("alice", "A")) is None


def test_throttle_window():
    t = [0.0]
    th = LoginThrottle(max_failures=2, window=60, clock=lambda: t[0])
    th.failed("u:a")
    th.failed("u:a")
    assert th.blocked("u:a")
    t[0] = 61
    assert not th.blocked("u:a")


def test_no_account_explains_bootstrap(connector, store, embedder):
    http = TestClient(create_app(store, embedder, FakeLLM(), {connector.name: connector}), follow_redirects=False)
    assert "ADMIN_PASSWORD" in http.get("/connexion").text
    assert login(http).status_code == 401


def test_list_mode_for_logged_user(http):
    login(http)
    assert "au moins 2" in http.post("/rechercher", data={"mode": "criteres", "valeur": "x"}).text
    html = http.post("/rechercher", data={"mode": "criteres", "valeur": "durand"}).text
    assert "Aucun document trouvé" in html and "contrat-fournisseur.docx" in html


def test_cli_creates_and_resets_account(store):
    from argparse import Namespace

    from ged.cli import _account

    answers = iter(["secours-mdp-1", "secours-mdp-1", "nouveau-mdp-22", "nouveau-mdp-22"])
    assert _account(store, Namespace(login="ops", role="admin", nom=""), lambda _: next(answers)) == 0
    assert store.get_user("ops").role == Role.ADMIN
    ops = store.get_user("ops")
    ops.active = False
    store.update_user(ops)
    assert _account(store, Namespace(login="ops", role=None, nom=""), lambda _: next(answers)) == 0
    assert store.get_user("ops").active and Accounts(store).authenticate("ops", "nouveau-mdp-22")
