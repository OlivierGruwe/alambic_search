"""Comptes d'Alambic Search : mots de passe, clés d'API, rôles et droits sur les documents.

- Mot de passe : empreinte scrypt (sel aléatoire), jamais le mot de passe ; vérification en temps
  constant. Une empreinte vide interdit la connexion par mot de passe.
- Clé d'API : « als_<préfixe>_<secret> ». Le préfixe (public) retrouve la clé, seule l'empreinte
  SHA-256 du secret est gardée : la clé n'est montrée qu'une fois, à sa création.
- Droits sur les documents d'un compte : groupes saisis à la main + principaux de son identifiant
  AD (SID, noms de groupes) lus dans l'annuaire s'il est rattaché. Plus « Tout le monde ».
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
import secrets
import threading
import time

from ged.identity import AUTHENTICATED_USERS
from ged.models import ApiKey, Role, User
from ged.ntsecurity import EVERYONE

log = logging.getLogger("alambic_search.comptes")

KEY_PREFIX = "als_"
LOGIN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,63}$")
MIN_PASSWORD = 10

# scrypt : ~50 ms par vérification, 16 Mo de mémoire (résiste aux cartes graphiques)
_N, _R, _P = 2**14, 8, 1


class AccountError(ValueError):
    """Saisie refusée (identifiant pris, mot de passe trop court…) : message pour l'utilisateur."""


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731
    return f"scrypt${_N}${_R}${_P}${b64(salt)}${b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, digest = stored.split("$")
        if algo != "scrypt":
            return False
        expected = base64.b64decode(digest)
        got = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                             dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, expected)


def check_password(password: str) -> None:
    if len(password) < MIN_PASSWORD:
        raise AccountError(f"Mot de passe trop court : {MIN_PASSWORD} caractères au moins.")


def password_tag(user: User) -> str:
    """Empreinte courte du mot de passe, mise dans la session : changer le mot de passe ferme les
    sessions ouvertes ailleurs."""
    return hashlib.sha256(user.password_hash.encode()).hexdigest()[:10]


def _key_digest(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def parse_key(raw: str) -> tuple[str, str] | None:
    """(préfixe, secret) d'une clé bien formée, sinon None."""
    raw = raw.strip()
    if not raw.startswith(KEY_PREFIX):
        return None
    parts = raw[len(KEY_PREFIX):].split("_", 1)
    if len(parts) != 2 or len(parts[0]) != 8 or len(parts[1]) < 20:
        return None
    return parts[0], parts[1]


def split_groups(text: str) -> list[str]:
    """Groupes saisis : séparés par des virgules ou des retours à la ligne, sans doublons."""
    out: list[str] = []
    for g in re.split(r"[,\n]", text or ""):
        g = g.strip()
        if g and g not in out:
            out.append(g[:200])
    return out[:200]


class Accounts:
    def __init__(self, store, identity=None, clock=time.time):
        self.store = store
        self.identity = identity
        self._clock = clock
        self._verified: dict[tuple[str, str], float] = {}  # (login, empreinte) → échéance (API en Basic)
        self._lock = threading.Lock()

    # --- Connexion ----------------------------------------------------------------------

    def authenticate(self, login: str, password: str) -> User | None:
        """Compte actif dont le mot de passe correspond, sinon None (temps de calcul comparable)."""
        user = self.store.get_user(login.strip()) if login.strip() else None
        if user is None or not user.active or not user.password_hash:
            verify_password(password, _DUMMY)  # même durée qu'un vrai essai : n'indique pas si le compte existe
            return None
        cache_key = (user.login, hashlib.sha256((password + user.password_hash).encode()).hexdigest())
        with self._lock:
            if self._verified.get(cache_key, 0) > self._clock():
                return user
        if not verify_password(password, user.password_hash):
            return None
        with self._lock:  # API appelée en HTTP Basic : pas 50 ms de scrypt à chaque appel
            if len(self._verified) > 1000:
                self._verified.clear()
            self._verified[cache_key] = self._clock() + 60
        return user

    def from_api_key(self, raw: str) -> User | None:
        parsed = parse_key(raw)
        if parsed is None:
            return None
        key = self.store.get_api_key(parsed[0])
        if key is None or not hmac.compare_digest(key.key_hash, _key_digest(parsed[1])):
            return None
        if not key.usable(self._clock()):
            return None
        user = self.store.get_user_by_id(key.user_id)
        if user is None or not user.active:
            return None
        if key.last_used is None or self._clock() - key.last_used > 300:
            self.store.touch_api_key(key.id)  # au plus toutes les 5 minutes
        return user

    def from_session(self, session: dict) -> User | None:
        user = self.store.get_user(session.get("u", ""))
        if user is None or not user.active or session.get("pw") != password_tag(user):
            return None
        return user

    # --- Droits ---------------------------------------------------------------------------

    def principals(self, user: User) -> frozenset[str]:
        """Principaux du compte. Lève une exception si l'annuaire est injoignable (compte rattaché)."""
        out = set(user.groups) | {EVERYONE, AUTHENTICATED_USERS, f"u:{user.login.lower()}"}
        if user.ad_login and self.identity is not None:
            ad = self.identity.principals(user.ad_login)
            out |= ad
        return frozenset(out)

    # --- Gestion -----------------------------------------------------------------------

    def create(self, login: str, name: str, role: str, password: str = "", ad_login: str = "",
               groups: list[str] | None = None) -> User:
        login = login.strip()
        if not LOGIN_RE.match(login):
            raise AccountError("Identifiant : lettres, chiffres, . _ - @ (64 caractères au plus), sans espace.")
        if self.store.get_user(login) is not None:
            raise AccountError(f"L'identifiant « {login} » est déjà pris.")
        if role not in Role.ORDER:
            raise AccountError("Rôle inconnu.")
        if password:
            check_password(password)
        user = User(0, login, name.strip() or login, role, hash_password(password) if password else "",
                    ad_login.strip(), groups or [])
        user.id = self.store.create_user(user)
        log.info("Compte créé : %s (%s)", login, role)
        return user

    def set_password(self, user: User, password: str) -> User:
        check_password(password)
        user.password_hash = hash_password(password)
        self.store.update_user(user)
        log.info("Mot de passe changé : %s", user.login)
        return user

    def admins(self) -> list[User]:
        return [u for u in self.store.list_users() if u.role == Role.ADMIN and u.active]

    def guard_last_admin(self, user: User, new_role: str | None = None, active: bool = True,
                         deleting: bool = False) -> None:
        """Il reste toujours au moins un administrateur actif."""
        if user.role != Role.ADMIN or not user.active:
            return
        if deleting or not active or (new_role is not None and new_role != Role.ADMIN):
            if len(self.admins()) <= 1:
                raise AccountError("C'est le dernier administrateur actif : créez-en un autre d'abord.")

    def new_api_key(self, user: User, name: str, days: int | None = None) -> tuple[ApiKey, str]:
        """Crée une clé ; renvoie la clé et sa valeur complète (montrée une seule fois)."""
        name = name.strip()[:100] or "Clé d'API"
        prefix = secrets.token_hex(4)
        secret = secrets.token_urlsafe(32)
        expires = self._clock() + days * 86400 if days else None
        key = ApiKey(0, user.id, name, prefix, _key_digest(secret), expires_at=expires)
        key.id = self.store.create_api_key(key)
        log.info("Clé d'API créée pour %s : %s (%s…)", user.login, name, prefix)
        return key, f"{KEY_PREFIX}{prefix}_{secret}"

    def bootstrap(self, admin_password: str | None) -> User | None:
        """Premier démarrage : sans aucun compte, crée « admin » avec ADMIN_PASSWORD."""
        if self.store.list_users():
            return None
        if not admin_password:
            log.warning("Aucun compte : définissez ADMIN_PASSWORD (compte « admin » créé au démarrage) "
                        "ou lancez « alambic-search compte admin --role admin ».")
            return None
        user = User(0, "admin", "Administrateur", Role.ADMIN, hash_password(admin_password))
        user.id = self.store.create_user(user)
        log.info("Premier démarrage : compte « admin » créé (mot de passe ADMIN_PASSWORD)")
        return user


_DUMMY = hash_password(secrets.token_hex(8))
