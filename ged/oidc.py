"""Connexion unique par OpenID Connect (Keycloak, ou tout fournisseur OIDC : Entra ID…).

Flux « code d'autorisation » avec PKCE : l'utilisateur s'identifie chez Keycloak (une fois pour
toutes les applications Alambic), qui renvoie un code ; Alambic Search l'échange contre un
jeton d'identité, dont il vérifie la signature (clés publiques JWKS), l'émetteur, le
destinataire, l'échéance et le nonce.

Groupes de l'utilisateur, au choix (section `oidc` de sources.yaml, clé `groupes`) :
- `annuaire` (défaut) : relus dans l'AD à partir de l'identifiant (SID compris : nécessaire
  pour les partages Windows et SharePoint). Keycloak ne sert qu'à authentifier ;
- `jeton` : ceux du jeton (Keycloak les lit dans l'annuaire du client) ; pour une installation
  qui n'a pas accès à l'annuaire (Alambic en SaaS). Pas de SID : droits par dossier, GED.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx
import jwt

log = logging.getLogger(__name__)


class OidcError(Exception):
    """Connexion refusée ou impossible (message destiné à l'utilisateur ou à l'administrateur)."""


@dataclass(frozen=True)
class OidcUser:
    login: str
    name: str
    groups: list[str]
    roles: list[str]  # rôles de royaume et rôles « client:rôle » (tuiles du portail)


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


class OidcClient:
    def __init__(self, issuer: str, client_id: str, client_secret: str | None = None, *,
                 groups_mode: str = "annuaire", groups_claim: str = "groups", public_url: str | None = None,
                 scopes: str = "openid profile email", transport: httpx.BaseTransport | None = None, leeway: int = 60,
                 internal_url: str | None = None):
        if groups_mode not in ("annuaire", "jeton"):
            raise ValueError("oidc.groupes : « annuaire » ou « jeton »")
        self.issuer = issuer.rstrip("/")
        # Adresse de Keycloak vue par le serveur, si elle diffère de celle des navigateurs (Docker :
        # http://keycloak:8180/… contre http://localhost:8180/…). Sert aux appels serveur à serveur
        # (découverte, clés, échange du code) ; l'émetteur des jetons reste l'adresse publique.
        self.internal_url = (internal_url or "").rstrip("/") or None
        self.client_id = client_id
        self.client_secret = client_secret
        self.groups_mode = groups_mode
        self.groups_claim = groups_claim
        self.public_url = (public_url or "").rstrip("/") or None
        self.scopes = scopes
        self.leeway = leeway
        self._http = httpx.Client(transport=transport, timeout=httpx.Timeout(15.0, connect=5.0))
        self._meta: dict | None = None
        self._keys: dict[str, jwt.PyJWK] = {}
        self._lock = threading.Lock()

    # --- Métadonnées et clés ---------------------------------------------------------

    def metadata(self) -> dict:
        if self._meta is None:
            try:
                r = self._http.get(f"{self.internal_url or self.issuer}/.well-known/openid-configuration")
                r.raise_for_status()
            except httpx.HTTPError as exc:
                raise OidcError(f"Fournisseur d'identité injoignable ({self.issuer}) : {exc}") from exc
            meta = r.json()
            if meta.get("issuer", "").rstrip("/") != self.issuer:
                raise OidcError(f"Émetteur inattendu : {meta.get('issuer')} (attendu {self.issuer})")
            if self.internal_url:
                for key in ("token_endpoint", "jwks_uri"):  # appels du serveur : adresse interne
                    if meta.get(key, "").startswith(self.issuer):
                        meta[key] = self.internal_url + meta[key][len(self.issuer):]
            self._meta = meta
        return self._meta

    def _key(self, kid: str | None) -> jwt.PyJWK:
        with self._lock:
            if kid not in self._keys:  # clé inconnue : rotation des clés chez Keycloak, on relit
                r = self._http.get(self.metadata()["jwks_uri"])
                r.raise_for_status()
                self._keys = {k.get("kid"): jwt.PyJWK(k) for k in r.json().get("keys", [])
                              if k.get("use", "sig") == "sig"}
            if kid not in self._keys:
                raise OidcError("Jeton signé par une clé inconnue")
            return self._keys[kid]

    def check(self) -> tuple[bool, str]:
        try:
            meta = self.metadata()
        except OidcError as exc:
            return False, str(exc)
        return True, f"{meta['issuer']} (client {self.client_id}, groupes depuis {'le jeton' if self.groups_mode == 'jeton' else 'l annuaire'})"

    # --- Flux de connexion -----------------------------------------------------------

    def authorize_url(self, redirect_uri: str, state: str, nonce: str, challenge: str) -> str:
        return self.metadata()["authorization_endpoint"] + "?" + urlencode({
            "response_type": "code", "client_id": self.client_id, "redirect_uri": redirect_uri,
            "scope": self.scopes, "state": state, "nonce": nonce,
            "code_challenge": challenge, "code_challenge_method": "S256",
        })

    def exchange(self, code: str, redirect_uri: str, verifier: str) -> dict:
        data = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
                "client_id": self.client_id, "code_verifier": verifier}
        if self.client_secret:
            data["client_secret"] = self.client_secret
        try:
            r = self._http.post(self.metadata()["token_endpoint"], data=data)
        except httpx.HTTPError as exc:
            raise OidcError(f"Fournisseur d'identité injoignable : {exc}") from exc
        if r.status_code != 200:
            raise OidcError(f"Code de connexion refusé ({r.status_code}) : recommencez la connexion")
        return r.json()

    def _decode(self, token: str, *, audience: str | None) -> dict:
        try:
            header = jwt.get_unverified_header(token)
            return jwt.decode(
                token, self._key(header.get("kid")).key, algorithms=["RS256", "RS384", "RS512", "ES256", "ES384", "PS256"],
                audience=audience, issuer=self.metadata()["issuer"], leeway=self.leeway,
                options={"verify_aud": audience is not None, "require": ["exp", "iat", "iss"]},
            )
        except jwt.PyJWTError as exc:
            raise OidcError(f"Jeton invalide : {exc}") from exc

    def validate_id_token(self, token: str, nonce: str) -> dict:
        claims = self._decode(token, audience=self.client_id)
        if not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
            raise OidcError("Jeton rejoué ou d'une autre connexion (nonce)")
        return claims

    def validate_access_token(self, token: str) -> dict:
        """Jeton porteur (API) : signé par le fournisseur ET destiné à Alambic Search
        (aud, ou azp pour un jeton émis à Alambic Search lui-même)."""
        claims = self._decode(token, audience=None)
        aud = claims.get("aud") or []
        aud = [aud] if isinstance(aud, str) else aud
        if self.client_id not in aud and claims.get("azp") != self.client_id:
            raise OidcError("Jeton destiné à une autre application")
        return claims

    def user(self, claims: dict) -> OidcUser:
        from ged.identity import bare_login

        # Keycloak branché sur l'AD : preferred_username = sAMAccountName ; Entra : UPN (alice@corp.fr)
        login = bare_login(str(claims.get("preferred_username") or claims.get("upn") or claims.get("email")
                               or claims["sub"]))
        groups = claims.get(self.groups_claim) or []
        groups = [str(g).rsplit("/", 1)[-1] for g in (groups if isinstance(groups, list) else [groups])]
        roles = list((claims.get("realm_access") or {}).get("roles", []))
        for client, access in (claims.get("resource_access") or {}).items():
            roles += [f"{client}:{r}" for r in access.get("roles", [])]
        return OidcUser(login, str(claims.get("name") or login), groups, roles)

    def logout_url(self, post_logout_redirect_uri: str, id_token: str | None = None) -> str | None:
        endpoint = self.metadata().get("end_session_endpoint")
        if not endpoint:
            return None
        params = {"client_id": self.client_id, "post_logout_redirect_uri": post_logout_redirect_uri}
        if id_token:
            params["id_token_hint"] = id_token
        return endpoint + "?" + urlencode(params)


def new_login_state() -> dict:
    verifier, challenge = pkce_pair()
    return {"state": secrets.token_urlsafe(24), "nonce": secrets.token_urlsafe(24), "verifier": verifier,
            "challenge": challenge, "t": int(time.time())}
