"""Qui est l'appelant, quel est son rôle, et quels documents peut-il lire ? Un seul endroit pour
l'API, les pages et la console.

Ordre : clé d'API (Authorization: Bearer als_… ou X-API-Key), puis identifiant et mot de passe
(HTTP Basic), puis session (cookie, après connexion sur /connexion), puis en-têtes X-User si l'API
est derrière un proxy de confiance (TRUST_IDENTITY_HEADERS). Sinon : 401.
"""
from __future__ import annotations

import logging

from fastapi import HTTPException, Request
from fastapi.security.utils import get_authorization_scheme_param

from ged.accounts import Accounts
from ged.models import Role, User
from ged.session import COOKIE, LoginThrottle, Sessions

log = logging.getLogger("alambic_search.auth")
BASIC_REALM = {"WWW-Authenticate": 'Basic realm="Alambic Search"'}


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


class Auth:
    def __init__(self, sessions: Sessions, accounts: Accounts, identity=None, *, trust_headers: bool = False,
                 throttle: LoginThrottle | None = None):
        self.sessions = sessions
        self.accounts = accounts
        self.identity = identity
        self.trust_headers = trust_headers
        self.throttle = throttle or LoginThrottle()

    def session(self, request: Request) -> dict | None:
        return self.sessions.read(request.cookies.get(COOKIE))

    def _basic(self, request: Request, value: str) -> User:
        import base64
        import binascii

        try:
            login, _, password = base64.b64decode(value).decode().partition(":")
        except (binascii.Error, UnicodeDecodeError) as exc:
            raise HTTPException(401, "En-tête Authorization invalide", headers=BASIC_REALM) from exc
        keys = (f"u:{login.strip().lower()}", f"ip:{client_ip(request)}")
        if self.throttle.blocked(*keys):
            raise HTTPException(429, "Trop d'essais infructueux. Réessayez dans un quart d'heure.")
        user = self.accounts.authenticate(login, password)
        if user is None:
            self.throttle.failed(*keys)
            log.warning("API : identifiants refusés pour %r depuis %s", login[:60], client_ip(request))
            raise HTTPException(401, "Identifiant ou mot de passe incorrect", headers=BASIC_REALM)
        return user

    def user(self, request: Request) -> User | None:
        """Compte de l'appelant, ou None sans aucune authentification. Identifiants présentés mais
        faux : 401 (pas de repli silencieux sur une autre méthode)."""
        if hasattr(request.state, "user"):
            return request.state.user
        user = None
        scheme, value = get_authorization_scheme_param(request.headers.get("authorization", ""))
        api_key = request.headers.get("x-api-key", "")
        if scheme.lower() == "bearer" or api_key:  # identifiants explicites : priorité sur le cookie
            user = self.accounts.from_api_key(value if scheme.lower() == "bearer" else api_key)
            if user is None:
                raise HTTPException(401, "Clé d'API invalide, expirée ou révoquée",
                                    headers={"WWW-Authenticate": "Bearer"})
        elif scheme.lower() == "basic" and value:
            user = self._basic(request, value)
        else:
            session = self.session(request)
            if session is not None:
                user = self.accounts.from_session(session)
        request.state.user = user
        return user

    def principals(self, user: User) -> frozenset[str]:
        try:
            return self.accounts.principals(user)
        except Exception as exc:
            log.error("Annuaire injoignable (droits de %s) : %s", user.login, exc)
            raise HTTPException(503, "Annuaire injoignable : droits non vérifiables") from exc

    def request_principals(self, request: Request, x_user: str = "", x_user_groups: str = "") -> frozenset[str]:
        user = self.user(request)
        if user is not None:
            return self.principals(user)
        if self.trust_headers:
            from ged.api import resolve_principals

            return resolve_principals(self.identity, x_user, x_user_groups)
        raise HTTPException(401, "Connexion requise : session (/connexion), clé d'API ou identifiant et mot de passe",
                            headers=BASIC_REALM)

    def require(self, request: Request, role: str) -> User:
        user = self.user(request)
        if user is None:
            raise HTTPException(401, "Connexion requise", headers=BASIC_REALM)
        if not Role.at_least(user.role, role):
            raise HTTPException(403, f"Rôle « {Role.LABELS[role]} » requis")
        return user
