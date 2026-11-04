"""Secrets Docker : toute variable NOM peut être fournie par un fichier, via NOM_FILE.

En Docker Swarm, les secrets sont montés en fichiers (/run/secrets/<nom>) ; jamais dans les
variables d'environnement visibles par `docker inspect`. Exemple :
    ADMIN_PASSWORD_FILE=/run/secrets/search_admin_password
→ ADMIN_PASSWORD prend le contenu du fichier (sans le saut de ligne final).
Une variable déjà définie l'emporte sur son _FILE. Seuls les fichiers sous /run/secrets/ (là où
Docker monte les secrets) sont lus : beaucoup d'outils posent des variables en _FILE
(PIP_CONFIG_FILE…) qui ne sont pas des secrets de l'application.
"""
from __future__ import annotations

import os
from urllib.parse import quote, urlsplit, urlunsplit

SECRETS_DIR = "/run/secrets/"


def load_env_files(environ=None, secrets_dir: str | None = None) -> list[str]:
    """Charge les NOM_FILE dans NOM. Renvoie les noms chargés (jamais les valeurs)."""
    env = os.environ if environ is None else environ
    loaded = []
    for key in [k for k in env if k.endswith("_FILE") and len(k) > 5]:
        name = key[:-5]
        path = env[key] or ""
        if env.get(name) or not path.startswith(secrets_dir or SECRETS_DIR):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                env[name] = f.read().rstrip("\r\n")
        except OSError as exc:
            raise SystemExit(f"{key} : fichier illisible ({path}) : {exc}") from exc
        loaded.append(name)
    return loaded


def with_password(url: str, password: str | None) -> str:
    """DATABASE_URL sans mot de passe + DATABASE_PASSWORD(_FILE) → URL complète."""
    if not password:
        return url
    parts = urlsplit(url)
    if parts.password or not parts.username:
        return url
    host = parts.hostname or ""
    netloc = f"{parts.username}:{quote(password, safe='')}@{host}" + (f":{parts.port}" if parts.port else "")
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
