"""Décodage des descripteurs de sécurité Windows (MS-DTYP 2.4) en droits de lecture.

On ne garde que ce qui compte pour la recherche : qui a le droit de LIRE le
contenu d'un fichier, et qui en est explicitement privé.

Choix de sécurité assumé : un refus explicite l'emporte toujours. Windows, lui,
évalue les ACE dans l'ordre et un « autorisé » explicite placé avant un « refusé »
hérité peut gagner. Notre lecture est donc plus stricte : au pire, un document
lisible reste caché, jamais l'inverse.
"""
from __future__ import annotations

import struct

from ged.models import Acl

EVERYONE = "S-1-1-0"

# Types d'ACE gérés
_ALLOWED = 0x00
_DENIED = 0x01
# Drapeaux d'ACE
_INHERIT_ONLY = 0x08  # ne s'applique qu'aux enfants, pas à l'objet lui-même
# Contrôle du descripteur
_DACL_PRESENT = 0x0004
# Droits qui permettent de lire le contenu
FILE_READ_DATA = 0x00000001
GENERIC_ALL = 0x10000000
GENERIC_READ = 0x80000000
_READ_RIGHTS = FILE_READ_DATA | GENERIC_ALL | GENERIC_READ


class SecurityDescriptorError(ValueError):
    pass


def sid_from_bytes(data: bytes, offset: int = 0) -> tuple[str, int]:
    """SID binaire -> (« S-1-5-21-… », longueur en octets)."""
    if len(data) < offset + 8:
        raise SecurityDescriptorError("SID tronqué")
    revision, count = data[offset], data[offset + 1]
    authority = int.from_bytes(data[offset + 2 : offset + 8], "big")
    end = offset + 8 + 4 * count
    if len(data) < end:
        raise SecurityDescriptorError("SID tronqué")
    subs = struct.unpack_from(f"<{count}I", data, offset + 8)
    return "-".join(["S", str(revision), str(authority), *map(str, subs)]), end - offset


def sid_to_bytes(sid: str) -> bytes:
    """« S-1-5-21-… » -> SID binaire (recherche LDAP par objectSid, tests)."""
    parts = sid.split("-")
    if len(parts) < 3 or parts[0].upper() != "S":
        raise ValueError(f"SID invalide : {sid}")
    revision, authority, subs = int(parts[1]), int(parts[2]), [int(p) for p in parts[3:]]
    return bytes([revision, len(subs)]) + authority.to_bytes(6, "big") + struct.pack(f"<{len(subs)}I", *subs)


def acl_from_security_descriptor(sd: bytes) -> Acl:
    """Descripteur de sécurité auto-relatif -> droits de lecture."""
    if len(sd) < 20:
        raise SecurityDescriptorError("Descripteur de sécurité tronqué")
    _rev, _sbz1, control, _owner, _group, _sacl, dacl_offset = struct.unpack_from("<BBHIIII", sd, 0)

    # Pas de DACL, ou DACL nulle : Windows accorde l'accès complet à tout le monde
    if not control & _DACL_PRESENT or dacl_offset == 0:
        return Acl(frozenset({EVERYONE}))

    if len(sd) < dacl_offset + 8:
        raise SecurityDescriptorError("DACL tronquée")
    _arev, _sbz, _size, ace_count, _sbz2 = struct.unpack_from("<BBHHH", sd, dacl_offset)

    allow: set[str] = set()
    deny: set[str] = set()
    pos = dacl_offset + 8
    for _ in range(ace_count):
        if len(sd) < pos + 4:
            raise SecurityDescriptorError("ACE tronquée")
        ace_type, flags, ace_size = struct.unpack_from("<BBH", sd, pos)
        if ace_size < 4:
            raise SecurityDescriptorError("Taille d'ACE invalide")
        if ace_type in (_ALLOWED, _DENIED) and not flags & _INHERIT_ONLY:
            (mask,) = struct.unpack_from("<I", sd, pos + 4)
            if mask & _READ_RIGHTS:
                sid, _ = sid_from_bytes(sd, pos + 8)
                (allow if ace_type == _ALLOWED else deny).add(sid)
        # Les autres types (objet, callback, audit) ne concernent pas un fichier : ignorés.
        pos += ace_size
    return Acl(frozenset(allow), frozenset(deny))


def build_security_descriptor(
    allow: list[tuple[str, int]] = (), deny: list[tuple[str, int]] = (), inherit_only: list[str] = ()
) -> bytes:
    """Construit un descripteur auto-relatif (tests et démonstration)."""
    aces = b""
    for ace_type, entries in ((_DENIED, deny), (_ALLOWED, allow)):
        for sid, mask in entries:
            body = struct.pack("<I", mask) + sid_to_bytes(sid)
            aces += struct.pack("<BBH", ace_type, 0, 4 + len(body)) + body
    for sid in inherit_only:
        body = struct.pack("<I", 0x001F01FF) + sid_to_bytes(sid)
        aces += struct.pack("<BBH", _ALLOWED, _INHERIT_ONLY, 4 + len(body)) + body
    count = len(allow) + len(deny) + len(inherit_only)
    dacl = struct.pack("<BBHHH", 2, 0, 8 + len(aces), count, 0) + aces
    header = struct.pack("<BBHIIII", 1, 0, 0x8000 | _DACL_PRESENT, 0, 0, 0, 20)
    return header + dacl
