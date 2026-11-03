"""Connexion unique par Keycloak (OIDC) : un faux Keycloak signe de vrais jetons RSA."""
from __future__ import annotations

import time
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from ged.api import create_app
from ged.identity import StaticResolver
from ged.oidc import OidcClient
from ged.sync import sync_source

from .conftest import FakeLLM

ISSUER = "https://auth.test/realms/alambic"


@pytest.fixture(autouse=True)
def no_header_trust(monkeypatch):
    monkeypatch.setenv("TRUST_IDENTITY_HEADERS", "false")


class FakeKeycloak:
    def __init__(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = "k1"
        self.codes: dict[str, dict] = {}  # code → {nonce, challenge, user}
        self.users = {
            "alice": {"name": "Alice Martin", "groups": ["/compta"], "roles": {"alambic-light": ["validation"]}},
            "olivier": {"name": "Olivier G", "groups": ["/direction", "/rh"],
                        "roles": {"alambic-search": ["admin"], "alambic-light": ["admin"]}},
            "inconnu": {"name": "Pas dans l'AD", "groups": [], "roles": {}},
        }
        self.next_user = "alice"
        self.secret = "s3cr3t"

    def jwks(self):
        jwk = jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key(), as_dict=True)
        return {"keys": [{**jwk, "kid": self.kid, "use": "sig", "alg": "RS256"}]}

    def token(self, user: str, *, aud="alambic-search", nonce=None, iss=ISSUER, exp=300, key=None, roles=True):
        u = self.users[user]
        now = int(time.time())
        claims = {"iss": iss, "aud": aud, "azp": "alambic-search", "sub": f"id-{user}", "iat": now, "exp": now + exp,
                  "preferred_username": user, "name": u["name"], "groups": u["groups"],
                  "realm_access": {"roles": ["offline_access"]},
                  "resource_access": {c: {"roles": r} for c, r in u["roles"].items()}}
        if nonce:
            claims["nonce"] = nonce
        if not roles:  # jeton d'identité de Keycloak : pas de rôles de clients (mapper « roles » par défaut)
            claims.pop("resource_access")
        return jwt.encode(claims, key or self.key, algorithm="RS256", headers={"kid": self.kid})

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        base = f"{ISSUER}/protocol/openid-connect"
        if url == f"{ISSUER}/.well-known/openid-configuration":
            return httpx.Response(200, json={
                "issuer": ISSUER, "authorization_endpoint": f"{base}/auth", "token_endpoint": f"{base}/token",
                "jwks_uri": f"{base}/certs", "end_session_endpoint": f"{base}/logout"})
        if url == f"{base}/certs":
            return httpx.Response(200, json=self.jwks())
        if url == f"{base}/token":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            pending = self.codes.pop(form.get("code", ""), None)
            if pending is None or form.get("client_secret") != self.secret:
                return httpx.Response(400, json={"error": "invalid_grant"})
            import base64
            import hashlib

            challenge = base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"].encode()).digest()).decode().rstrip("=")
            if challenge != pending["challenge"]:
                return httpx.Response(400, json={"error": "invalid_grant", "error_description": "PKCE"})
            return httpx.Response(200, json={"id_token": self.token(pending["user"], nonce=pending["nonce"], roles=False),
                                             "access_token": self.token(pending["user"])})
        return httpx.Response(404)

    def authorize(self, authorize_url: str) -> str:
        """Ce que fait le navigateur chez Keycloak : l'utilisateur s'identifie, Keycloak redirige avec un code."""
        q = {k: v[0] for k, v in parse_qs(urlsplit(authorize_url).query).items()}
        assert q["code_challenge_method"] == "S256" and q["client_id"] == "alambic-search"
        code = f"code-{len(self.codes)}"
        self.codes[code] = {"nonce": q["nonce"], "challenge": q["code_challenge"], "user": self.next_user}
        return f"{q['redirect_uri']}?code={code}&state={q['state']}"


@pytest.fixture
def kc():
    return FakeKeycloak()


def make_app(kc, store, embedder, connector, *, mode="annuaire", portal=None):
    oidc = OidcClient(ISSUER, "alambic-search", kc.secret, groups_mode=mode, transport=httpx.MockTransport(kc.handler))
    identity = StaticResolver({"alice": ["compta"], "olivier": ["direction", "rh"]})
    app = create_app(store, embedder, FakeLLM("Pénalité de 2 % [1]."), {connector.name: connector},
                     identity=identity, oidc=oidc, session_secret="k", portal=portal or [])
    return TestClient(app, follow_redirects=False)


def sso_login(http, kc, user="alice", suite="/"):
    kc.next_user = user
    r = http.get(f"/connexion?suite={suite}")
    assert r.status_code == 303 and r.headers["location"].startswith(f"{ISSUER}/protocol/openid-connect/auth")
    callback = kc.authorize(r.headers["location"])
    return http.get(callback.replace("http://testserver", ""))


def test_login_with_keycloak_then_search_with_ad_rights(kc, connector, store, embedder):
    sync_source(connector, store, embedder)
    http = make_app(kc, store, embedder, connector)
    r = sso_login(http, kc)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert "Alice Martin" in http.get("/").text
    html = http.post("/rechercher", data={"question": "pénalité de retard", "formuler": "true"}).text
    assert "contrat-fournisseur.docx" in html
    html = http.post("/rechercher", data={"question": "grille des salaires", "formuler": "false"}).text
    assert "salaires.txt" not in html


def test_password_form_is_disabled_with_sso(kc, connector, store, embedder):
    http = make_app(kc, store, embedder, connector)
    r = http.post("/connexion", data={"identifiant": "alice", "mot_de_passe": "x"})
    assert r.status_code == 405


def test_state_nonce_and_replay_are_checked(kc, connector, store, embedder):
    http = make_app(kc, store, embedder, connector)
    r = http.get("/connexion")
    callback = kc.authorize(r.headers["location"]).replace("http://testserver", "")
    assert http.get(callback.replace("state=", "state=x")).status_code == 400  # state falsifié
    assert http.get(callback).status_code == 303
    http.cookies.clear()
    assert http.get(callback).status_code == 400  # rejoué : plus de connexion en cours


def test_user_absent_from_directory_is_refused(kc, connector, store, embedder):
    http = make_app(kc, store, embedder, connector)
    r = sso_login(http, kc, "inconnu")
    assert r.status_code == 403 and "annuaire" in r.text


def test_groups_from_token_mode(kc, connector, store, embedder):
    sync_source(connector, store, embedder)
    http = make_app(kc, store, embedder, connector, mode="jeton")
    sso_login(http, kc, "inconnu")  # pas dans l'annuaire, mais le jeton suffit en mode « jeton »
    kc.users["inconnu"]["groups"] = ["/rh"]
    http.cookies.clear()
    sso_login(http, kc, "inconnu")
    html = http.post("/rechercher", data={"question": "grille des salaires", "formuler": "false"}).text
    assert "salaires.txt" in html  # groupe « rh » venu du jeton (« /rh » → « rh »)


def test_portal_tiles_follow_roles(kc, connector, store, embedder):
    portal = [
        {"nom": "Alambic Light — validation", "url": "https://light.test/validation", "description": "", "role": "alambic-light:validation"},
        {"nom": "Alambic Light — types", "url": "https://light.test/ui/types", "description": "", "role": "alambic-light:admin"},
    ]
    http = make_app(kc, store, embedder, connector, portal=portal)
    sso_login(http, kc, "alice")
    page = http.get("/").text
    assert "Rechercher dans les documents" in page and "light.test/validation" in page
    assert "light.test/ui/types" not in page  # pas le rôle admin
    assert "Portail" in http.get("/rechercher").text


def test_admin_console_by_keycloak_role(kc, connector, store, embedder):
    http = make_app(kc, store, embedder, connector)
    sso_login(http, kc, "alice")
    assert http.get("/admin").status_code in (401, 503)  # pas le rôle
    http.cookies.clear()
    sso_login(http, kc, "olivier")
    assert http.get("/admin").status_code == 200  # rôle alambic-search:admin, sans mot de passe


def test_single_logout_goes_to_keycloak(kc, connector, store, embedder):
    http = make_app(kc, store, embedder, connector)
    sso_login(http, kc)
    r = http.post("/deconnexion")
    q = parse_qs(urlsplit(r.headers["location"]).query)
    assert r.headers["location"].startswith(f"{ISSUER}/protocol/openid-connect/logout")
    assert q["post_logout_redirect_uri"] == ["http://testserver/connexion?deconnecte=1"]
    assert "id_token_hint" in q and q["client_id"] == ["alambic-search"]
    assert "Vous êtes déconnecté" in http.get("/connexion?deconnecte=1").text


def test_api_accepts_keycloak_bearer_tokens_only_for_this_app(kc, connector, store, embedder):
    sync_source(connector, store, embedder)
    http = make_app(kc, store, embedder, connector)
    ok = http.get("/search", params={"q": "pénalité"}, headers={"Authorization": f"Bearer {kc.token('alice')}"})
    assert ok.status_code == 200 and ok.json()
    other_app = kc.token("alice", aud="alambic-light")
    other_app = jwt.encode({**jwt.decode(other_app, options={"verify_signature": False}), "azp": "alambic-light"},
                           kc.key, algorithm="RS256", headers={"kid": kc.kid})
    assert http.get("/search", params={"q": "x"}, headers={"Authorization": f"Bearer {other_app}"}).status_code == 401
    forged = kc.token("alice", key=rsa.generate_private_key(public_exponent=65537, key_size=2048))
    assert http.get("/search", params={"q": "x"}, headers={"Authorization": f"Bearer {forged}"}).status_code == 401
    expired = kc.token("alice", exp=-3600)
    assert http.get("/search", params={"q": "x"}, headers={"Authorization": f"Bearer {expired}"}).status_code == 401


def test_load_oidc_and_portal_config(tmp_path, monkeypatch):
    from ged.config import ConfigError, load_oidc, load_portal

    monkeypatch.setenv("OIDC_SECRET", "x")
    p = tmp_path / "s.yaml"
    p.write_text(f"""
oidc:
  issuer: {ISSUER}
  client_id: alambic-search
  client_secret_env: OIDC_SECRET
  groupes: jeton
portail:
  - nom: Alambic Light
    url: https://light.test/ui
    role: alambic-light:validation
""", encoding="utf-8")
    oidc = load_oidc(p)
    assert oidc.groups_mode == "jeton" and oidc.client_secret == "x"
    assert load_portal(p) == [{"nom": "Alambic Light", "url": "https://light.test/ui", "description": "",
                               "role": "alambic-light:validation"}]
    p.write_text("portail:\n  - nom: X\n    url: javascript:alert(1)\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="url invalide"):
        load_portal(p)


def test_internal_url_for_server_calls(kc):
    """Docker : navigateur sur localhost:8180, serveur sur keycloak:8180 ; émetteur inchangé."""
    internal = "http://keycloak:8180/realms/alambic"
    seen = []

    def handler(request):
        seen.append(str(request.url))
        rewritten = str(request.url).replace(internal, ISSUER)
        return kc.handler(httpx.Request(request.method, rewritten, content=request.content, headers=request.headers))

    oidc = OidcClient(ISSUER, "alambic-search", kc.secret, internal_url=internal, transport=httpx.MockTransport(handler))
    meta = oidc.metadata()
    assert meta["authorization_endpoint"].startswith(ISSUER)  # navigateur : adresse publique
    assert meta["token_endpoint"].startswith(internal) and meta["jwks_uri"].startswith(internal)
    claims = oidc.validate_access_token(kc.token("alice"))
    assert claims["iss"] == ISSUER and all(u.startswith(internal) for u in seen)


def test_bandeau_lists_apps_by_product_and_roles(kc, connector, store, embedder):
    portal = [{"nom": "Validation", "url": "https://light.test/validation", "description": "", "role": "alambic-light:validation"},
              {"nom": "Types de documents", "url": "https://light.test/ui/types", "description": "", "role": "alambic-light:admin"}]
    http = make_app(kc, store, embedder, connector, portal=portal)
    sso_login(http, kc, "alice")
    html = http.get("/rechercher").text
    assert '<p class="al-pop-group">Alambic Search</p>' in html and '<p class="al-pop-group">Alambic Light</p>' in html
    assert "light.test/validation" in html and "light.test/ui/types" not in html
    assert ">Administration<" not in html.replace("<strong>", ">").replace("</strong>", "<")  # pas admin
    assert "Se déconnecter" in html and "Alice Martin" in html and ">AM<" in html
    http.cookies.clear()
    sso_login(http, kc, "olivier")
    html = http.get("/rechercher").text
    assert "<strong>Administration</strong>" in html and "light.test/ui/types" in html
