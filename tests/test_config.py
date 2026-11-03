import pytest

from ged.config import ConfigError, load_connectors, load_identity
from ged.identity import LdapResolver, StaticResolver


def write(tmp_path, text):
    p = tmp_path / "sources.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_filesystem_and_static_identity(tmp_path, share):
    p = write(tmp_path, f"""
identity:
  type: static
  users:
    alice: [compta]
sources:
  - name: partage
    type: filesystem
    root: "{share.as_posix()}"
    acl:
      Compta: [compta]
""")
    assert set(load_connectors(p)) == {"partage"}
    resolver = load_identity(p)
    assert isinstance(resolver, StaticResolver) and "compta" in resolver.principals("alice")


def test_no_identity_section(tmp_path, share):
    p = write(tmp_path, f"sources:\n  - name: p\n    type: filesystem\n    root: \"{share.as_posix()}\"\n")
    assert load_identity(p) is None


def test_ldap_identity_reads_password_from_env(tmp_path, monkeypatch):
    p = write(tmp_path, """
identity:
  type: ldap
  url: ldaps://dc.corp.local
  base_dn: DC=corp,DC=local
  bind_user: CORP\\\\svc-alambic
  bind_password_env: LDAP_PASSWORD
""")
    monkeypatch.setenv("LDAP_PASSWORD", "s3cret")
    assert isinstance(load_identity(p), LdapResolver)
    monkeypatch.delenv("LDAP_PASSWORD")
    with pytest.raises(ConfigError, match="LDAP_PASSWORD"):
        load_identity(p)


def test_password_in_file_is_refused(tmp_path):
    p = write(tmp_path, """
sources:
  - name: commun
    type: smb
    server: fichiers
    share: Commun
    password: en-clair
""")
    with pytest.raises(ConfigError, match="password_env"):
        load_connectors(p)


def test_unknown_source_type(tmp_path):
    p = write(tmp_path, "sources:\n  - name: x\n    type: webdav\n")
    with pytest.raises(ConfigError, match="webdav"):
        load_connectors(p)


def test_root_from_environment_variable(tmp_path, share, monkeypatch):
    p = write(tmp_path, "sources:\n  - name: p\n    type: filesystem\n    root: ${DOCUMENTS_DIR}\n")
    monkeypatch.setenv("DOCUMENTS_DIR", share.as_posix())
    assert load_connectors(p)["p"].root == share.resolve()
    monkeypatch.delenv("DOCUMENTS_DIR")
    with pytest.raises(ConfigError, match="DOCUMENTS_DIR"):
        load_connectors(p)
