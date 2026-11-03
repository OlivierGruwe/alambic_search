"""Connecteur FTP / FTPS.

Parcours par MLSD (RFC 3659 : date, taille, type, identifiant unique), que proposent les
serveurs actuels (vsftpd, ProFTPD, Pure-FTPd, FileZilla Server, IIS). FTPS explicite (AUTH TLS)
recommandé : en FTP simple, le mot de passe circule en clair.

Droits : FTP n'en expose pas d'exploitables (seulement ceux du compte de service) ; ils sont
définis dossier par dossier dans la source, comme pour un dossier monté.

Identité : fait « unique » de MLSD s'il existe (survit aux renommages), sinon le chemin
(un renommage = suppression + ajout).
"""
from __future__ import annotations

import datetime as dt
import ftplib
import io
import posixpath
import threading
from collections.abc import Iterator

from ged.connectors.ecm import indexable
from ged.connectors.filesystem import DEFAULT_EXCLUDED_DIRS, DEFAULT_EXTENSIONS
from ged.connectors.rules import FolderRules
from ged.models import Acl, DocumentRef


def _mlsd_time(value: str | None) -> float:
    if not value:
        return 0.0
    return dt.datetime.strptime(value[:14], "%Y%m%d%H%M%S").replace(tzinfo=dt.UTC).timestamp()


class FtpConnector:
    def __init__(self, name: str, host: str, root: str = "", *, username: str = "anonymous", password: str = "",
                 port: int = 21, tls: bool = True, acl_rules: dict[str, list[str]] | None = None,
                 extensions: frozenset[str] = DEFAULT_EXTENSIONS, timeout: float = 30.0):
        self.name = name
        self.host = host
        self.port = port
        self.root = "/" + root.strip("/") if root.strip("/") else "/"
        self.username = username
        self.tls = tls
        self.extensions = frozenset(e.lower() for e in extensions)
        self._password = password
        self._timeout = timeout
        self._rules = FolderRules(acl_rules)
        self._ftp: ftplib.FTP | None = None
        self._lock = threading.Lock()  # une connexion FTP ne se partage pas entre deux commandes
        with self._lock:
            self._connect()  # échoue tout de suite si l'hôte, le compte ou le dossier sont faux

    def describe(self) -> dict:
        return {
            "type": "FTP" + ("S (TLS)" if self.tls else " (non chiffré)"),
            "location": f"{'ftps' if self.tls else 'ftp'}://{self.host}:{self.port}{self.root}",
            "account": self.username,
            "rules": self._rules.describe(),
            "extensions": sorted(self.extensions),
        }

    def _connect(self) -> ftplib.FTP:
        if self._ftp is not None:
            try:
                self._ftp.voidcmd("NOOP")
                return self._ftp
            except (ftplib.Error, OSError, EOFError, AttributeError):  # AttributeError : connexion fermée
                self._ftp = None
        ftp = ftplib.FTP_TLS(timeout=self._timeout) if self.tls else ftplib.FTP(timeout=self._timeout)
        ftp.connect(self.host, self.port)
        try:
            ftp.login(self.username, self._password)
        except ftplib.error_perm as exc:
            raise PermissionError(f"Le serveur FTP refuse le compte {self.username} : {exc}") from exc
        if self.tls:
            ftp.prot_p()  # données chiffrées aussi, pas seulement la connexion de commande
        try:
            ftp.cwd(self.root)
        except ftplib.error_perm as exc:
            raise FileNotFoundError(f"Dossier FTP introuvable : {self.root}") from exc
        self._ftp = ftp
        return ftp

    def _mlsd(self, folder: str) -> list[tuple[str, dict]]:
        with self._lock:
            ftp = self._connect()
            try:
                return list(ftp.mlsd(folder, facts=["type", "size", "modify", "unique"]))
            except ftplib.error_perm as exc:
                if str(exc).startswith("500") or str(exc).startswith("502"):
                    raise OSError("Le serveur FTP ne connaît pas MLSD (RFC 3659) : liste fiable impossible") from exc
                raise

    def _rel(self, full: str) -> str:
        return posixpath.relpath(full, self.root) if self.root != "/" else full.lstrip("/")

    def list_documents(self) -> Iterator[DocumentRef]:
        stack = [self.root]
        while stack:
            folder = stack.pop()
            for fname, facts in self._mlsd(folder):
                kind = facts.get("type", "")
                if kind in ("cdir", "pdir") or fname in (".", ".."):
                    continue
                full = posixpath.join(folder, fname)
                if kind == "dir":
                    if not fname.startswith(".") and fname not in DEFAULT_EXCLUDED_DIRS:
                        stack.append(full)
                    continue
                if kind != "file" or not indexable(fname, self.extensions):
                    continue
                rel = self._rel(full)
                yield DocumentRef(self.name, f"ftp:{facts['unique']}" if facts.get("unique") else rel, rel,
                                  _mlsd_time(facts.get("modify")), int(facts.get("size") or 0))

    def _safe(self, rel: str) -> str:
        parts = [p for p in rel.split("/") if p]
        if any(p in ("..", ".") for p in parts):
            raise PermissionError(f"Chemin hors de la source : {rel}")
        return posixpath.join(self.root, *parts)

    def read_bytes(self, ref: DocumentRef) -> bytes:
        buf = io.BytesIO()
        with self._lock:
            self._connect().retrbinary(f"RETR {self._safe(ref.path)}", buf.write)
        return buf.getvalue()

    def locate(self, doc_id: str, hint_path: str | None = None) -> DocumentRef | None:
        if hint_path and not doc_id.startswith("ftp:"):
            doc_id = hint_path  # l'identifiant EST le chemin
        for ref in self.list_documents():
            if ref.doc_id == doc_id:
                return ref
        return None

    def acl(self, ref: DocumentRef) -> Acl:
        return self._rules.acl(ref.path)
