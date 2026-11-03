import pytest
from ldap3 import MOCK_SYNC, OFFLINE_AD_2012_R2, Connection, Server

from ged.identity import AUTHENTICATED_USERS, BUILTIN_USERS, LdapResolver, StaticResolver
from ged.ntsecurity import EVERYONE, sid_to_bytes

DOM = "S-1-5-21-111-222-333"
ALICE, COMPTA, DOMAIN_USERS, FINANCE = f"{DOM}-1104", f"{DOM}-1201", f"{DOM}-513", f"{DOM}-1300"
BASE = "DC=corp,DC=local"


def test_static_resolver():
    r = StaticResolver({"Alice": ["compta"]}, names={COMPTA: "compta"})
    assert r.principals("alice") == {"compta", EVERYONE, AUTHENTICATED_USERS, "u:alice"}
    assert r.principals("inconnu") == frozenset()
    assert r.display_name(EVERYONE) == "Tout le monde"
    assert r.display_name(COMPTA) == "compta"


@pytest.fixture
def ad():
    server = Server("dc", get_info=OFFLINE_AD_2012_R2)
    calls = {"n": 0}

    def factory():
        calls["n"] += 1
        conn = Connection(server, user="cn=svc," + BASE, password="x", client_strategy=MOCK_SYNC)
        s = conn.strategy
        s.add_entry("cn=svc," + BASE, {"userPassword": "x", "objectClass": "user"})
        s.add_entry(f"CN=Alice,{BASE}", {
            "objectClass": ["top", "person", "user"], "sAMAccountName": "alice",
            "objectSid": sid_to_bytes(ALICE),
            # groupes transitifs : compta (direct), finance (compta est membre de finance), Domain Users
            "tokenGroups": [sid_to_bytes(COMPTA), sid_to_bytes(FINANCE), sid_to_bytes(DOMAIN_USERS)],
        })
        for sid, name in ((COMPTA, "compta"), (FINANCE, "finance"), (DOMAIN_USERS, "Domain Users")):
            s.add_entry(f"CN={name},{BASE}", {"objectClass": ["top", "group"], "sAMAccountName": name,
                                              "cn": name, "objectSid": sid_to_bytes(sid)})
        conn.bind()
        return conn

    return LdapResolver("ldaps://dc", BASE, "svc", "x", connection_factory=factory), calls


def test_ldap_principals_include_nested_groups(ad):
    resolver, _ = ad
    p = resolver.principals("alice")
    assert {ALICE, COMPTA, FINANCE, DOMAIN_USERS, EVERYONE, AUTHENTICATED_USERS, BUILTIN_USERS} <= p
    assert {"compta", "finance"} <= p  # noms utilisables dans les règles par dossier


def test_ldap_unknown_user_and_injection(ad):
    resolver, _ = ad
    assert resolver.principals("personne") == frozenset()
    assert resolver.principals("*") == frozenset()
    assert resolver.principals("alice)(cn=*") == frozenset()


def test_ldap_results_are_cached(ad):
    resolver, calls = ad
    resolver.principals("alice")
    n = calls["n"]
    resolver.principals("Alice")
    assert calls["n"] == n


def test_ldap_display_names(ad):
    resolver, _ = ad
    assert resolver.display_name(FINANCE) == "finance"
    assert resolver.display_name(EVERYONE) == "Tout le monde"
    assert resolver.display_name(f"{DOM}-9999") == f"{DOM}-9999"  # SID inconnu : affiché tel quel
    assert resolver.display_name("compta") == "compta"


# --- Connexion des utilisateurs (vérification du mot de passe par l'AD) --------------------

def test_ldap_authenticate_binds_as_the_user(ad):
    resolver, _ = ad
    seen = []

    def bind_check(dn, password):
        seen.append(dn)
        return password == "Secret!1"

    resolver._bind_check = bind_check
    for login in ("alice", "CORP\\alice", "alice@corp.local", " ALICE "):
        account = resolver.authenticate(login, "Secret!1")
        assert account is not None and account.login == "alice"
    assert seen[0] == f"CN=Alice,{BASE}"
    assert resolver.authenticate("alice", "mauvais") is None


def test_ldap_authenticate_refuses_empty_password_and_injection(ad):
    resolver, _ = ad
    resolver._bind_check = lambda dn, pw: True  # même si l'AD acceptait : jamais appelé
    assert resolver.authenticate("alice", "") is None  # bind anonyme : réussirait côté LDAP
    assert resolver.authenticate("*", "x") is None
    assert resolver.authenticate("alice)(objectClass=*", "x") is None
    assert resolver.authenticate("inconnu", "x") is None


def test_static_authenticate():
    r = StaticResolver({"alice": ["compta"]}, password="demo")
    assert r.authenticate("Alice", "demo").login == "alice"
    assert r.authenticate("alice", "autre") is None and r.authenticate("bob", "demo") is None
    assert StaticResolver({"alice": ["compta"]}).authenticate("alice", "") is None  # sans mot de passe : fermé
