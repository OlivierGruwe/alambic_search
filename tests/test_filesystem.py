import pytest

from ged.connectors import FileSystemConnector


def paths(connector):
    return sorted(r.path for r in connector.list_documents())


def test_listing_filters_lock_files_and_extensions(connector):
    assert paths(connector) == [
        "Compta/Factures/facture-acme.pdf",
        "Compta/contrat-fournisseur.docx",
        "Public/horaires.txt",
        "RH/salaires.txt",
    ]


def test_acl_most_specific_rule(connector):
    acl = {r.path: connector.acl(r).allow for r in connector.list_documents()}
    assert acl["Compta/Factures/facture-acme.pdf"] == {"compta", "direction"}  # hérite de Compta
    assert acl["RH/salaires.txt"] == {"rh"}
    assert acl["Public/horaires.txt"] == {"tous"}


def test_default_deny_without_rule(share):
    c = FileSystemConnector("p", share, {"Public": ["tous"]})
    acl = {r.path: c.acl(r).allow for r in c.list_documents()}
    assert acl["RH/salaires.txt"] == frozenset()


def test_rename_keeps_doc_id(connector, share):
    before = {r.path: r.doc_id for r in connector.list_documents()}
    (share / "Public/horaires.txt").rename(share / "Public/horaires-mairie.txt")
    after = {r.path: r.doc_id for r in connector.list_documents()}
    assert after["Public/horaires-mairie.txt"] == before["Public/horaires.txt"]


def test_path_traversal_refused(connector):
    with pytest.raises(PermissionError):
        connector._safe_path("../../etc/passwd")


def test_locate_with_and_without_hint(connector):
    ref = next(r for r in connector.list_documents() if r.path.endswith("salaires.txt"))
    assert connector.locate(ref.doc_id, hint_path=ref.path) == ref
    assert connector.locate(ref.doc_id, hint_path="mauvais/chemin.txt") == ref
    assert connector.locate("0:0") is None
