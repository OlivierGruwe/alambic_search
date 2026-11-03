"""Connecteur partage Windows (SMB), sans montage réseau.

- Identité stable : le FileId du serveur (survit aux renommages et déplacements).
- Droits : la DACL NTFS de chaque fichier, lue avec le compte de service et
  traduite en SID autorisés / refusés en lecture (voir ged.ntsecurity).
- Coût maîtrisé : la DACL est mise en cache et relue quand le ChangeTime du fichier
  bouge (NTFS le met à jour lors d'un changement de droits), et dans tous les cas
  au-delà de `acl_refresh_hours` : certains serveurs (Samba notamment) ne font pas
  évoluer ChangeTime quand seuls les droits changent. `invalidate_acls()` force une
  relecture complète (synchro « complète » de la console).

Le compte de service n'a besoin que de la lecture (données + « lire les autorisations »).
"""
from __future__ import annotations

import logging
import ntpath
import time
from collections.abc import Iterator
from pathlib import PurePosixPath

import smbclient
from smbclient._io import SMBFileTransaction, SMBRawIO
from smbprotocol.file_info import InfoType
from smbprotocol.open import FilePipePrinterAccessMask, SMB2QueryInfoRequest, SMB2QueryInfoResponse

from ged.connectors.filesystem import DEFAULT_EXCLUDED_DIRS, DEFAULT_EXTENSIONS
from ged.models import Acl, DocumentRef
from ged.ntsecurity import acl_from_security_descriptor

log = logging.getLogger(__name__)

_DACL_SECURITY_INFORMATION = 0x00000004
_SD_MAX = 64 * 1024


class SmbConnector:
    def __init__(
        self,
        name: str,
        server: str,
        share: str,
        path: str = "",
        *,
        username: str | None = None,
        password: str | None = None,
        port: int = 445,
        encrypt: bool = False,
        acl_refresh_hours: float = 24.0,
        extensions: frozenset[str] = DEFAULT_EXTENSIONS,
        excluded_dirs: frozenset[str] = DEFAULT_EXCLUDED_DIRS,
    ):
        self.name = name
        self.server = server
        self.share = share
        self.subpath = path.strip("\\/").replace("/", "\\")
        self.port = port
        self.username = username
        self.extensions = frozenset(e.lower() for e in extensions)
        self.excluded_dirs = excluded_dirs
        self.base = rf"\\{server}\{share}" + (rf"\{self.subpath}" if self.subpath else "")
        self._conn = {"port": port}
        smbclient.register_session(server, username=username, password=password, port=port, encrypt=encrypt)
        self.acl_ttl = acl_refresh_hours * 3600
        self._ctime: dict[str, float] = {}  # doc_id -> ChangeTime vu au dernier listing
        self._acl_cache: dict[str, tuple[float, float, Acl]] = {}  # doc_id -> (ChangeTime, lu à, droits)

    def describe(self) -> dict:
        return {
            "type": "Partage Windows (SMB, droits NTFS)",
            "location": self.base,
            "account": self.username or "(session courante)",
            "acl_refresh": f"relus si modifiés, et au moins toutes les {self.acl_ttl / 3600:g} h",
            "extensions": sorted(self.extensions),
        }

    # --- Listing -----------------------------------------------------------------

    def list_documents(self) -> Iterator[DocumentRef]:
        # Toute erreur de parcours remonte : un listing incomplet ne doit pas déclencher de suppressions.
        stack = [self.base]
        while stack:
            folder = stack.pop()
            for entry in smbclient.scandir(folder, **self._conn):
                if entry.is_dir():
                    if not entry.name.startswith(".") and entry.name not in self.excluded_dirs:
                        stack.append(ntpath.join(folder, entry.name))
                    continue
                if entry.name.startswith("~$") or entry.name.startswith("."):
                    continue
                if PurePosixPath(entry.name).suffix.lower() not in self.extensions:
                    continue
                info = entry.smb_info
                doc_id = str(info.file_id)
                self._ctime[doc_id] = info.change_time.timestamp()
                yield DocumentRef(
                    source=self.name,
                    doc_id=doc_id,
                    path=self._relative(ntpath.join(folder, entry.name)),
                    mtime=info.last_write_time.timestamp(),
                    size=info.end_of_file,
                )

    def _relative(self, unc: str) -> str:
        return unc[len(self.base) :].lstrip("\\").replace("\\", "/")

    def _unc(self, rel: str) -> str:
        parts = [p for p in rel.split("/") if p]
        if any(p in ("..", ".") for p in parts):
            raise PermissionError(f"Chemin hors de la source : {rel}")
        return ntpath.join(self.base, *parts)

    # --- Lecture ------------------------------------------------------------------

    def read_bytes(self, ref: DocumentRef) -> bytes:
        with smbclient.open_file(self._unc(ref.path), mode="rb", share_access="rwd", **self._conn) as f:
            return f.read()

    def locate(self, doc_id: str, hint_path: str | None = None) -> DocumentRef | None:
        if hint_path:
            try:
                st = smbclient.stat(self._unc(hint_path), **self._conn)
                if str(st.st_ino) == doc_id:
                    return DocumentRef(self.name, doc_id, hint_path, st.st_mtime, st.st_size)
            except (OSError, PermissionError):
                pass
        for ref in self.list_documents():  # déplacé depuis l'indexation : on le cherche
            if ref.doc_id == doc_id:
                return ref
        return None

    # --- Droits -------------------------------------------------------------------

    def acl(self, ref: DocumentRef) -> Acl:
        ctime = self._ctime.get(ref.doc_id)
        cached = self._acl_cache.get(ref.doc_id)
        now = time.monotonic()
        if cached and ctime is not None and cached[0] == ctime and now - cached[1] < self.acl_ttl:
            return cached[2]
        acl = acl_from_security_descriptor(self._read_security_descriptor(self._unc(ref.path)))
        if ctime is not None:
            self._acl_cache[ref.doc_id] = (ctime, now, acl)
        return acl

    def invalidate_acls(self) -> None:
        """Oublie les droits en cache : la prochaine synchro relit toutes les DACL."""
        self._acl_cache.clear()

    def _read_security_descriptor(self, unc: str) -> bytes:
        raw = SMBRawIO(unc, mode="r", desired_access=FilePipePrinterAccessMask.READ_CONTROL, **self._conn)
        with SMBFileTransaction(raw) as transaction:
            req = SMB2QueryInfoRequest()
            req["info_type"] = InfoType.SMB2_0_INFO_SECURITY
            req["file_info_class"] = 0
            req["additional_information"] = _DACL_SECURITY_INFORMATION
            req["output_buffer_length"] = _SD_MAX
            req["file_id"] = transaction.raw.fd.file_id

            def receive(request):
                response = transaction.raw.fd.connection.receive(request)
                query = SMB2QueryInfoResponse()
                query.unpack(response["data"].get_value())
                return query["buffer"].get_value()

            transaction += (req, receive)
        return transaction.results[0]
