"""Connexion des utilisateurs : les groupes viennent de l'annuaire, jamais de l'utilisateur."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from ged.api import create_app
from ged.identity import StaticResolver
from ged.session import COOKIE, LoginThrottle, Sessions
from ged.sync import sync_source

from .conftest import FakeLLM


@pytest.fixture(autouse=True)
def no_header_trust(monkeypatch):
    monkeypatch.setenv("TRUST_IDENTITY_HEADERS", "false")  # comportement de production


@pytest.fixture
def http(connector, store, embedder):
    sync_source(connector, store, embedder)
    identity = StaticResolver({"alice": ["compta"], "bob": ["rh"]}, password="demo")
    app = create_app(store, embedder, FakeLLM("Pénalité de 2 % [1]."), {connector.name: connector},
                     identity=identity, session_secret="test-secret")
    return TestClient(app, follow_redirects=False)


def login(http, user="alice", password="demo", suite="/"):
    return http.post("/connexion", data={"identifiant": user, "mot_de_passe": password, "suite": suite})


def test_home_requires_login(http):
    r = http.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/connexion"
    assert "Avec votre compte Windows" in http.get("/connexion").text


def test_login_then_search_with_own_rights(http):
    r = login(http)
    assert r.status_code == 303 and r.headers["location"] == "/"
    cookie = r.cookies.get(COOKIE)
    assert cookie and "demo" not in cookie  # jamais le mot de passe
    set_cookie = r.headers["set-cookie"].lower()
    assert "httponly" in set_cookie and "samesite=lax" in set_cookie
    page = http.get("/").text
    assert "alice" in page and "Se déconnecter" in page and "groupes" not in page.lower()

    html = http.post("/rechercher", data={"question": "pénalité de retard", "formuler": "true"}).text
    assert "Pénalité de 2 %" in html and "contrat-fournisseur.docx" in html
    html = http.post("/rechercher", data={"question": "grille des salaires", "formuler": "false"}).text
    assert "salaires.txt" not in html  # RH : pas pour alice


def test_api_uses_session_and_ignores_headers(http):
    # sans session, en-têtes refusés : on ne se déclare pas membre de « rh »
    assert http.get("/search", params={"q": "salaires"}, headers={"X-User-Groups": "rh"}).status_code == 401
    assert http.post("/ask", json={"question": "salaires ?"}, headers={"X-User": "bob"}).status_code == 401
    login(http, "bob")
    hits = http.get("/search", params={"q": "grille des salaires"}, headers={"X-User-Groups": "compta"}).json()
    assert hits and all(h["path"].startswith("RH/") for h in hits)  # droits de bob, en-tête ignoré


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
    r = login(http, suite="https://pirate.example/")
    assert r.headers["location"] == "/"
    r = login(http, suite="//pirate.example/")
    assert r.headers["location"] == "/"


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
    import base64
    import json

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


def test_no_identity_configured(connector, store, embedder):
    http = TestClient(create_app(store, embedder, FakeLLM(), {connector.name: connector}), follow_redirects=False)
    assert "pas configurée" in http.get("/connexion").text
    assert login(http).status_code == 503


def test_list_mode_for_logged_user(http, store):
    login(http)
    assert "au moins 2" in http.post("/rechercher", data={"mode": "criteres", "valeur": "x"}).text
    html = http.post("/rechercher", data={"mode": "criteres", "valeur": "durand"}).text
    # pas de données extraites ici (sans Alambic Light) : le contrat apparaît dans les mentions
    assert "Aucun document trouvé" in html and "contrat-fournisseur.docx" in html
