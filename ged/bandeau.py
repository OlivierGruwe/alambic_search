"""Contenu du bandeau (charte alambic-ui) : marque, client, aide, utilisateur et son menu."""
from __future__ import annotations

import os

from fastapi import HTTPException

from ged.models import Role

PRODUCT = "search"


def _initials(name: str) -> str:
    parts = [p for p in name.replace(".", " ").split() if p]
    return ("".join(p[0] for p in parts[:2]) or "?").upper()


def build_context(request, auth) -> dict:
    """Contexte du gabarit _bandeau.html pour la requête en cours."""
    path = request.url.path
    try:
        user = auth.user(request) if auth is not None else None
    except HTTPException:
        user = None
    has_session = auth is not None and auth.session(request) is not None and user is not None

    links: list[dict] = []
    if user is not None:
        links.append({"nom": "Rechercher", "url": "/rechercher", "description": "Questions et listes de documents"})
        if Role.at_least(user.role, Role.MANAGER):
            links.append({"nom": "Console", "url": "/admin", "description": "Sources, documents, droits, comptes"})
        if has_session:
            links.append({"nom": "Mon compte", "url": "/compte", "description": "Mot de passe, clés d'API"})
    for link in links:
        url = link["url"]
        link["current"] = path == url or path.startswith(url + "/")

    return {
        "product": PRODUCT,
        "home": "/rechercher" if user is not None else "/",
        "client": os.environ.get("CLIENT_NAME") or None,
        "help_url": os.environ.get("HELP_URL") or None,
        "user": None if user is None else {
            "name": user.name or user.login, "login": user.login, "initials": _initials(user.name or user.login),
            "role": Role.LABELS.get(user.role, user.role), "role_code": user.role, "session": has_session,
        },
        "links": links,
    }
