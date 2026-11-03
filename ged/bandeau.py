"""Contenu du bandeau commun Alambic (alambic-ui) : applications accessibles, client, utilisateur.

Le même bandeau existe dans Alambic Light : marque, sélecteur d'applications (groupées par
produit, selon les rôles Keycloak), nom du client, aide, utilisateur et déconnexion.
"""
from __future__ import annotations

import os

PRODUCT = "search"
SEARCH_GROUP = "Alambic Search"


def _initials(name: str) -> str:
    parts = [p for p in name.replace(".", " ").split() if p]
    return ("".join(p[0] for p in parts[:2]) or "?").upper()


def _group_of(app: dict) -> str:
    if app.get("groupe"):
        return str(app["groupe"])
    role = app.get("role") or ""
    if role.startswith("alambic-light:"):
        return "Alambic Light"
    if role.startswith("alambic-search:") or app.get("url", "").startswith("/"):
        return SEARCH_GROUP
    return "Autres applications"


def build_context(request, auth, portal: list[dict], *, admin_client_id: str | None = None) -> dict:
    """Contexte du gabarit _bandeau.html pour la requête en cours."""
    path = request.url.path
    session = auth.session(request) if auth is not None else None
    roles = set((session or {}).get("r", []))
    in_admin = path.startswith("/admin")  # la console a déjà vérifié l'accès (mot de passe ou rôle)
    is_admin = in_admin or (admin_client_id is not None and f"{admin_client_id}:admin" in roles)

    apps: list[dict] = []
    if session is not None:
        apps.append({"nom": "Rechercher", "url": "/rechercher", "description": "Questions et listes de documents"})
    if is_admin:
        apps.append({"nom": "Administration", "url": "/admin", "description": "Sources, documents, bac à sable"})
    for app in portal:
        if app.get("url") == "/admin":
            continue  # déjà proposée ci-dessus, selon les droits réels
        if app.get("role") and app["role"] not in roles:
            continue
        apps.append(app)

    groups: dict[str, list[dict]] = {}
    for app in apps:
        url = app["url"]
        current = url.startswith("/") and (path == url or (url != "/" and path.startswith(url + "/")))
        groups.setdefault(_group_of(app), []).append({
            "nom": app["nom"], "url": url, "description": app.get("description") or "",
            "current": current, "externe": not url.startswith("/"),
        })
    ordered = sorted(groups.items(), key=lambda kv: (kv[0] != SEARCH_GROUP, kv[0]))

    user = None
    if session is not None:
        user = {"name": session.get("n") or session["u"], "login": session["u"], "initials": _initials(session.get("n") or session["u"])}
    elif in_admin:
        user = {"name": "Administrateur", "login": "admin", "initials": "A", "basic": True}

    return {
        "product": PRODUCT,
        "home": "/" if session is not None else ("/admin" if in_admin else "/"),
        "groups": [{"titre": t, "apps": a} for t, a in ordered],
        "client": os.environ.get("CLIENT_NAME") or None,
        "help_url": os.environ.get("HELP_URL") or None,
        "user": user,
        "portal": session is not None and len(apps) > 1,
    }
