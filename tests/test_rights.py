"""Page Droits : nature des principaux, ce qu'un utilisateur voit, principaux que personne n'a."""
from __future__ import annotations

from fastapi.testclient import TestClient

from ged.api import create_app
from ged.identity import StaticResolver
from ged.models import Acl, Chunk, DocumentRef, Status
from ged.ntsecurity import EVERYONE
from ged.rights import classify, user_warnings
from ged.store import MemoryStore

SID_COMPTA = "S-1-5-21-1-2-3-1105"
SID_GONE = "S-1-5-21-1-2-3-9999"


def doc(store, embedder, source, name, allow, deny=()):
    ref = DocumentRef(source, name, name, 1.0, 1)
    store.upsert_document(ref, "h" + name, Acl(frozenset(allow), frozenset(deny)), Status.INDEXED,
                          [Chunk(0, 1, name)], embedder.embed([name]))


def test_classify():
    names = {SID_COMPTA: "Compta"}.get
    display = lambda p: names(p, p)  # noqa: E731
    assert classify(EVERYONE).code == "special"
    assert classify(SID_COMPTA, display).code == "sid"
    assert classify(SID_GONE, display).code == "unknown-sid" and classify(SID_GONE, display).orphan
    assert classify("u:alice").code == "user" and classify("compta").code == "name"
    assert classify("ged:GROUP_site_rh_SiteManager").orphan and classify("entra:abc").orphan


def test_warnings_for_token_users():
    token_user = frozenset({"compta", "u:alice", EVERYONE})
    w = user_warnings(token_user, {"sid", "name"})
    assert any("Aucun SID" in x for x in w)
    assert user_warnings(frozenset({EVERYONE, "u:bob"}), {"name"})[0].startswith("Aucun groupe")
    assert user_warnings(frozenset({SID_COMPTA, "Compta", "u:a", EVERYONE}), {"sid", "name"}) == []


def test_rights_page(embedder):
    store = MemoryStore()
    doc(store, embedder, "partage", "facture.pdf", {SID_COMPTA})
    doc(store, embedder, "partage", "ancien.pdf", {SID_GONE})
    doc(store, embedder, "ged", "site.pdf", {"ged:GROUP_site_rh_SiteManager"})
    doc(store, embedder, "ged", "note.pdf", {"compta", EVERYONE}, deny={"u:bob"})
    identity = StaticResolver({"alice": [SID_COMPTA, "compta"]}, names={SID_COMPTA: "Compta"})
    c = TestClient(create_app(store, embedder, None, {}, admin_password="pw", identity=identity))
    auth = ("admin", "pw")

    html = c.get("/admin/droits", auth=auth).text
    assert "D'où viennent les principaux" in html and 'href="/admin/droits" aria-current="page"' in html
    assert "Groupe propre à la GED" in html and "SID inconnu de l" in html and "Compta" in html
    assert "2 que personne n" not in html and "1 que personne n" in html  # une par source

    html = c.get("/admin/droits", params={"login": "alice"}, auth=auth).text
    assert "Ce qu'il voit" in html and "Aucun SID" not in html
    assert store.visible_counts(identity.principals("alice")) == {"partage": 1, "ged": 1}

    html = c.get("/admin/droits", params={"groups": "compta"}, auth=auth).text  # groupes saisis seulement
    assert "Aucun SID Windows" in html and "Tout le monde" in html
    html = c.get("/admin/droits", params={"login": "inconnu"}, auth=auth).text
    assert "inconnu de l" in html


def test_principal_counts_pg_and_memory(embedder):
    import os

    from ged.store import PgStore

    stores = [MemoryStore()]
    if os.environ.get("GED_TEST_DSN"):
        pg = PgStore(os.environ["GED_TEST_DSN"], embedder.dim, pool_size=1)
        pg.init()
        pg.purge_source("droits-test")
        stores.append(pg)
    for s in stores:
        doc(s, embedder, "droits-test", "a.pdf", {"compta", EVERYONE}, deny={"u:bob"})
        doc(s, embedder, "droits-test", "b.pdf", {"compta"})
        rows = {(p, a, d) for src, p, a, d in s.principal_counts() if src == "droits-test"}
        assert rows == {("compta", 2, 0), (EVERYONE, 1, 0), ("u:bob", 0, 1)}
        assert s.visible_counts(frozenset({"compta"})).get("droits-test") == 2
        assert s.visible_counts(frozenset({"compta", "u:bob"})).get("droits-test") == 1
        s.purge_source("droits-test")
    for s in stores[1:]:
        s.close()
