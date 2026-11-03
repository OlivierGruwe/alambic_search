import struct

import pytest

from ged.models import Acl
from ged.ntsecurity import (
    EVERYONE,
    FILE_READ_DATA,
    GENERIC_READ,
    SecurityDescriptorError,
    acl_from_security_descriptor,
    build_security_descriptor,
    sid_from_bytes,
    sid_to_bytes,
)

COMPTA = "S-1-5-21-1004336348-1177238915-682003330-1105"
RH = "S-1-5-21-1004336348-1177238915-682003330-1106"
STAGIAIRE = "S-1-5-21-1004336348-1177238915-682003330-1201"
FULL = 0x001F01FF
WRITE_ONLY = 0x00000002  # FILE_WRITE_DATA


def test_sid_round_trip():
    for sid in (EVERYONE, "S-1-5-11", "S-1-5-32-545", COMPTA):
        data = sid_to_bytes(sid)
        assert sid_from_bytes(data) == (sid, len(data))


def test_known_binary_sid():
    # Administrateurs intégrés : S-1-5-32-544
    raw = bytes([1, 2, 0, 0, 0, 0, 0, 5]) + struct.pack("<II", 32, 544)
    assert sid_from_bytes(raw)[0] == "S-1-5-32-544"


def test_read_rights_allow_and_deny():
    sd = build_security_descriptor(
        allow=[(COMPTA, FULL), (RH, GENERIC_READ), ("S-1-5-32-545", WRITE_ONLY)],
        deny=[(STAGIAIRE, FILE_READ_DATA)],
    )
    acl = acl_from_security_descriptor(sd)
    assert acl.allow == {COMPTA, RH}  # le droit d'écriture seul ne donne pas la lecture
    assert acl.deny == {STAGIAIRE}


def test_deny_wins():
    acl = Acl(frozenset({COMPTA}), frozenset({STAGIAIRE}))
    assert acl.permits(frozenset({COMPTA}))
    assert not acl.permits(frozenset({COMPTA, STAGIAIRE}))  # stagiaire membre de la compta
    assert not acl.permits(frozenset({RH}))


def test_inherit_only_aces_are_ignored():
    sd = build_security_descriptor(allow=[(COMPTA, FULL)], inherit_only=["S-1-3-0"])  # CREATOR OWNER
    assert acl_from_security_descriptor(sd).allow == {COMPTA}


def test_null_dacl_means_everyone():
    no_dacl = struct.pack("<BBHIIII", 1, 0, 0x8000, 0, 0, 0, 0)
    assert acl_from_security_descriptor(no_dacl) == Acl(frozenset({EVERYONE}))


def test_empty_dacl_means_nobody():
    acl = acl_from_security_descriptor(build_security_descriptor())
    assert acl == Acl() and not acl.permits(frozenset({EVERYONE}))


def test_truncated_descriptor():
    sd = build_security_descriptor(allow=[(COMPTA, FULL)])
    with pytest.raises(SecurityDescriptorError):
        acl_from_security_descriptor(sd[:-6])
    with pytest.raises(SecurityDescriptorError):
        acl_from_security_descriptor(b"\x01\x00")
