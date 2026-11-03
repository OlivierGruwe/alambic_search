"""Résolution des identités : qui est l'utilisateur, à quels groupes appartient-il ?

Un « principal » est ce que comparent les droits des documents :
- un SID Windows (ACL NTFS lues par le connecteur SMB) ;
- un nom de groupe (règles par dossier du connecteur file system).

`LdapResolver` interroge l'Active Directory : l'attribut calculé `tokenGroups`
donne en une requête TOUS les groupes de l'utilisateur, y compris les groupes
imbriqués et le groupe principal (Utilisateurs du domaine).
`StaticResolver` sert aux démonstrations et aux tests.
"""
from __future__ import annotations

import hmac
import logging
import threading
import time
from collections.abc import Callable
from typing import Protocol

from ged.ntsecurity import EVERYONE, sid_from_bytes, sid_to_bytes

log = logging.getLogger(__name__)

AUTHENTICATED_USERS = "S-1-5-11"
BUILTIN_USERS = "S-1-5-32-545"
WELL_KNOWN = {
    EVERYONE: "Tout le monde",
    AUTHENTICATED_USERS: "Utilisateurs authentifiés",
    BUILTIN_USERS: "Utilisateurs (local)",
    "S-1-5-32-544": "Administrateurs (local)",
    "S-1-5-18": "Système",
    "S-1-3-0": "Créateur propriétaire",
}


class IdentityResolver(Protocol):
    def principals(self, login: str) -> frozenset[str]:
        """Tous les principaux d'un utilisateur. Ensemble vide si l'utilisateur est inconnu."""
        ...

    def display_name(self, principal: str) -> str:
        """Nom lisible d'un SID (ou le principal tel quel)."""
        ...

    def authenticate(self, login: str, password: str) -> Account | None:
        """Vérifie identifiant et mot de passe. None si refusé. Le mot de passe n'est jamais gardé."""
        ...


class Account:
    """Utilisateur authentifié : identifiant (sAMAccountName) et nom affiché."""

    __slots__ = ("login", "name")

    def __init__(self, login: str, name: str | None = None):
        self.login = login
        self.name = name or login

    def __eq__(self, other) -> bool:
        return isinstance(other, Account) and (self.login, self.name) == (other.login, other.name)

    def __repr__(self) -> str:
        return f"Account({self.login!r}, {self.name!r})"


def bare_login(login: str) -> str:
    """« CORP\\alice » ou « alice@corp.local » → « alice » (sAMAccountName)."""
    login = login.strip()
    if "\\" in login:
        login = login.rsplit("\\", 1)[1]
    if "@" in login:
        login = login.split("@", 1)[0]
    return login


def _valid_login(login: str) -> bool:
    """Pas de caractères qui changeraient le sens d'un filtre LDAP (injection)."""
    return bool(login) and len(login) <= 104 and not any(c in login for c in "*()\\\0/,=+<>;\"")


class StaticResolver:
    """Utilisateurs et groupes déclarés dans la configuration (démonstration, tests)."""

    def __init__(self, users: dict[str, list[str]], names: dict[str, str] | None = None,
                 password: str | None = None):
        self.users = {u.lower(): frozenset(g) for u, g in users.items()}
        self.names = {**WELL_KNOWN, **(names or {})}
        self._password = password  # démonstration : même mot de passe pour tous, lu dans l'environnement

    def authenticate(self, login: str, password: str) -> Account | None:
        login = bare_login(login).lower()
        if not self._password or login not in self.users or not password:
            return None
        if not hmac.compare_digest(password.encode(), self._password.encode()):
            return None
        return Account(login)

    def principals(self, login: str) -> frozenset[str]:
        groups = self.users.get(login.strip().lower())
        if groups is None:
            return frozenset()
        return groups | {EVERYONE, AUTHENTICATED_USERS, f"u:{login.strip().lower()}"}

    def display_name(self, principal: str) -> str:
        return self.names.get(principal, principal)


class _TTLCache:
    def __init__(self, ttl: float):
        self.ttl, self._data, self._lock = ttl, {}, threading.Lock()

    def get(self, key):
        with self._lock:
            hit = self._data.get(key)
            if hit and hit[0] > time.monotonic():
                return hit[1]
            return None

    def put(self, key, value):
        with self._lock:
            self._data[key] = (time.monotonic() + self.ttl, value)


class LdapResolver:
    def __init__(
        self,
        url: str,
        base_dn: str,
        bind_user: str,
        bind_password: str,
        *,
        cache_ttl: float = 300.0,
        connection_factory: Callable | None = None,
        bind_check: Callable[[str, str], bool] | None = None,
    ):
        self.url = url
        self.base_dn = base_dn
        self._bind = (bind_user, bind_password)
        self._factory = connection_factory or self._default_connection
        self._bind_check = bind_check or self._default_bind_check
        self._principals = _TTLCache(cache_ttl)  # appartenances : peuvent changer, cache court
        self._names = _TTLCache(cache_ttl * 12)  # noms de SID : très stables

    def _default_connection(self):
        from ldap3 import Connection, Server

        return Connection(Server(self.url, get_info=None), user=self._bind[0], password=self._bind[1],
                          auto_bind=True, read_only=True, receive_timeout=10)

    def _default_bind_check(self, dn: str, password: str) -> bool:
        """Connexion à l'annuaire AU NOM de l'utilisateur : c'est l'AD qui valide le mot de passe
        (compte désactivé, verrouillé ou expiré : refusé par l'AD lui-même)."""
        from ldap3 import Connection, Server

        conn = Connection(Server(self.url, get_info=None), user=dn, password=password,
                          read_only=True, receive_timeout=10)
        try:
            return bool(conn.bind())
        finally:
            conn.unbind()

    def authenticate(self, login: str, password: str) -> Account | None:
        login = bare_login(login)
        # Mot de passe vide : un « bind » LDAP anonyme RÉUSSIT, il faut le refuser avant
        if not _valid_login(login) or not password:
            return None
        conn = self._factory()
        try:
            conn.search(self.base_dn, f"(&(objectClass=user)(sAMAccountName={login}))",
                        attributes=["sAMAccountName", "displayName"])
            if len(conn.entries) != 1:
                return None
            entry = conn.entries[0]
            dn = entry.entry_dn
            account = Account(str(entry.sAMAccountName.value or login),
                              str(entry.displayName.value) if entry.displayName.value else None)
        finally:
            conn.unbind()
        return account if self._bind_check(dn, password) else None

    def principals(self, login: str) -> frozenset[str]:
        login = login.strip()
        if not _valid_login(login):
            return frozenset()
        cached = self._principals.get(login.lower())
        if cached is not None:
            return cached
        from ldap3 import BASE

        conn = self._factory()
        try:
            conn.search(self.base_dn, f"(&(objectClass=user)(sAMAccountName={login}))",
                        attributes=["objectSid", "sAMAccountName"])
            if not conn.entries:
                result: frozenset[str] = frozenset()
            else:
                entry = conn.entries[0]
                user_sid = _sid_value(entry.objectSid.raw_values[0])
                conn.search(entry.entry_dn, "(objectClass=*)", search_scope=BASE, attributes=["tokenGroups"])
                groups = [_sid_value(v) for v in conn.entries[0].tokenGroups.raw_values] if conn.entries else []
                # u:login : l'utilisateur lui-même, tel que le nomment les GED (Alfresco, CMIS)
                sids = {user_sid, *groups, EVERYONE, AUTHENTICATED_USERS, BUILTIN_USERS,
                        f"u:{str(entry.sAMAccountName.value or login).lower()}"}
                # Noms des groupes : permettent aussi les règles par dossier écrites avec des noms AD
                names = {self._lookup_name(conn, s) for s in groups}
                result = frozenset(sids | {n for n in names if n and not n.startswith("S-1-")})
        finally:
            conn.unbind()
        self._principals.put(login.lower(), result)
        return result

    def display_name(self, principal: str) -> str:
        if not principal.startswith("S-1-"):
            return principal
        if principal in WELL_KNOWN:
            return WELL_KNOWN[principal]
        cached = self._names.get(principal)
        if cached is not None:
            return cached
        try:
            conn = self._factory()
            try:
                name = self._lookup_name(conn, principal)
            finally:
                conn.unbind()
        except Exception:  # annuaire injoignable : on affiche le SID plutôt que d'échouer
            log.warning("Résolution du SID %s impossible", principal, exc_info=True)
            return principal
        return name

    def _lookup_name(self, conn, sid: str) -> str:
        cached = self._names.get(sid)
        if cached is not None:
            return cached
        from ldap3.utils.conv import escape_bytes

        conn.search(self.base_dn, f"(objectSid={escape_bytes(sid_to_bytes(sid))})",
                    attributes=["sAMAccountName", "cn"])
        name = sid
        if conn.entries:
            e = conn.entries[0]
            name = str(e.sAMAccountName.value or e.cn.value or sid)
        self._names.put(sid, name)
        return name


def _sid_value(raw) -> str:
    if isinstance(raw, bytes):
        return sid_from_bytes(raw)[0]
    return str(raw)
