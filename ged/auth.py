"""Qui est l'appelant, et quels sont ses droits ? Un seul endroit pour l'API et les pages.

Ordre : session (cookie, après connexion par mot de passe AD ou par Keycloak), puis jeton
porteur OIDC (API appelée par une autre application), puis en-têtes X-User si l'API est derrière
un proxy de confiance (TRUST_IDENTITY_HEADERS). Sinon : 401.
"""
from __future__ import annotations

from fastapi import HTTPException, Request

from ged.identity import AUTHENTICATED_USERS, bare_login
from ged.ntsecurity import EVERYONE
from ged.session import COOKIE, Sessions


def token_principals(login: str, groups: list[str]) -> frozenset[str]:
    """Groupes venus du jeton OIDC (pas d'annuaire joignable) : noms de groupes, sans SID."""
    return frozenset(groups) | {EVERYONE, AUTHENTICATED_USERS, f"u:{login.lower()}"}


class Auth:
    def __init__(self, sessions: Sessions, identity=None, oidc=None, *, trust_headers: bool = False):
        self.sessions = sessions
        self.identity = identity
        self.oidc = oidc
        self.trust_headers = trust_headers

    @property
    def can_login(self) -> bool:
        return self.oidc is not None or self.identity is not None

    def session(self, request: Request) -> dict | None:
        return self.sessions.read(request.cookies.get(COOKIE))

    def principals(self, session: dict) -> frozenset[str]:
        """Droits de l'utilisateur d'une session. Lève une exception si l'annuaire est injoignable."""
        if "g" in session:  # groupes du jeton (oidc.groupes: jeton)
            return token_principals(session["u"], session["g"])
        if self.identity is None:
            return frozenset()
        return self.identity.principals(bare_login(session["u"]))

    def request_principals(self, request: Request, x_user: str = "", x_user_groups: str = "") -> frozenset[str]:
        session = self.session(request)
        if session is not None:
            try:
                return self.principals(session)
            except Exception as exc:
                raise HTTPException(503, "Annuaire injoignable : droits non vérifiables") from exc
        bearer = request.headers.get("authorization", "")
        if self.oidc is not None and bearer.lower().startswith("bearer "):
            from ged.oidc import OidcError

            try:
                user = self.oidc.user(self.oidc.validate_access_token(bearer[7:].strip()))
            except OidcError as exc:
                raise HTTPException(401, str(exc), headers={"WWW-Authenticate": "Bearer"}) from exc
            if self.oidc.groups_mode == "jeton":
                return token_principals(user.login, user.groups)
            return self.principals({"u": user.login})
        if self.trust_headers:
            from ged.api import resolve_principals

            return resolve_principals(self.identity, x_user, x_user_groups)
        raise HTTPException(401, "Connexion requise (/connexion)")
