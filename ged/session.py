"""Session de l'utilisateur connecté : cookie signé (HMAC-SHA256), sans stockage côté serveur.

Le cookie ne contient que l'identifiant, le nom affiché, une empreinte courte du mot de passe et
l'échéance : jamais le mot de passe, jamais les groupes ni le rôle. Compte, rôle et groupes sont
relus à chaque requête : un compte désactivé, un rôle retiré ou un mot de passe changé prennent
effet aussitôt.

Cookie HttpOnly (illisible par JavaScript), SameSite=Lax (pas envoyé par un formulaire posté
depuis un autre site), Secure si COOKIE_SECURE=true (à activer derrière HTTPS).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import threading
import time

log = logging.getLogger(__name__)

COOKIE = "alambic_session"


class Sessions:
    def __init__(self, secret: str | None, *, hours: float = 8.0, secure: bool = False):
        if not secret:
            secret = secrets.token_urlsafe(32)
            log.warning("SESSION_SECRET non défini : clé de session aléatoire, les utilisateurs devront "
                        "se reconnecter à chaque redémarrage")
        self._key = secret.encode()
        self.max_age = int(hours * 3600)
        self.secure = secure

    def _sign(self, payload: bytes) -> str:
        return base64.urlsafe_b64encode(hmac.new(self._key, payload, hashlib.sha256).digest()).decode().rstrip("=")

    # --- Jetons signés génériques -----------------------------------------------------------

    def pack(self, data: dict, max_age: int) -> str:
        payload = json.dumps({**data, "exp": int(time.time()) + max_age}, separators=(",", ":"),
                             ensure_ascii=False).encode()
        body = base64.urlsafe_b64encode(payload).decode().rstrip("=")
        return f"{body}.{self._sign(payload)}"

    def unpack(self, token: str | None) -> dict | None:
        """Contenu d'un jeton intact et non expiré, sinon None."""
        if not token or "." not in token or len(token) > 8192:
            return None
        body, sig = token.rsplit(".", 1)
        try:
            payload = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        except ValueError:
            return None
        if not hmac.compare_digest(sig, self._sign(payload)):
            return None
        try:
            data = json.loads(payload)
        except ValueError:
            return None
        if not isinstance(data, dict) or data.get("exp", 0) < time.time():
            return None
        return data

    # --- Session de l'utilisateur ------------------------------------------------------

    def issue(self, login: str, name: str, *, pw: str = "") -> str:
        """`pw` : empreinte courte du mot de passe (accounts.password_tag) ; un changement de mot de
        passe rend la session caduque."""
        return self.pack({"u": login, "n": name, "pw": pw}, self.max_age)

    def read(self, token: str | None) -> dict | None:
        """{"u": login, "n": nom, "pw": empreinte} si le cookie est intact et non expiré."""
        data = self.unpack(token)
        return data if data and data.get("u") else None

    def set_cookie(self, response, login: str, name: str, **extra) -> None:
        response.set_cookie(COOKIE, self.issue(login, name, **extra), max_age=self.max_age, httponly=True,
                            samesite="lax", secure=self.secure, path="/")

    def clear_cookie(self, response) -> None:
        response.delete_cookie(COOKIE, path="/", httponly=True, samesite="lax", secure=self.secure)


class LoginThrottle:
    """Freine les essais de mots de passe : après `max_failures` échecs en `window` secondes pour
    un même identifiant (ou une même adresse), refus pendant `window` secondes.

    Volontairement plus strict que le verrouillage de l'AD (souvent 5 à 10 essais) : quelqu'un qui
    essaie des mots de passe ici ne doit pas pouvoir verrouiller les comptes Windows des collègues.
    """

    def __init__(self, max_failures: int = 5, window: float = 900.0, clock=time.monotonic):
        self.max_failures = max_failures
        self.window = window
        self._clock = clock
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, key: str) -> list[float]:
        now = self._clock()
        kept = [t for t in self._failures.get(key, []) if now - t < self.window]
        if kept:
            self._failures[key] = kept
        else:
            self._failures.pop(key, None)
        return kept

    def blocked(self, *keys: str) -> bool:
        with self._lock:
            return any(len(self._recent(k)) >= self.max_failures for k in keys)

    def failed(self, *keys: str) -> None:
        with self._lock:
            for k in keys:
                self._failures.setdefault(k, []).append(self._clock())

    def succeeded(self, *keys: str) -> None:
        with self._lock:
            for k in keys:
                self._failures.pop(k, None)
