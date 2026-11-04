"""Configuration : variables d'environnement + fichier des sources (YAML)."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from ged.connectors import FileSystemConnector


@dataclass(frozen=True)
class Settings:
    database_url: str
    embed_base_url: str
    embed_model: str
    embed_dim: int
    llm_base_url: str
    llm_model: str
    llm_api_key: str  # service en ligne (Mistral AI…) ; vide : LLM auto-hébergé
    sources_file: str
    convert_url: str  # Gotenberg (conversion PDF des fichiers bureautiques), vide = désactivée

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            # DATABASE_PASSWORD(_FILE) complète une URL sans mot de passe (secret Docker en Swarm)
            database_url=_db_url(),
            embed_base_url=os.environ.get("EMBED_BASE_URL", "http://127.0.0.1:11434/v1"),
            embed_model=os.environ.get("EMBED_MODEL", "bge-m3"),
            embed_dim=int(os.environ.get("EMBED_DIM", "1024")),
            # vide : même serveur que les vecteurs (Ollama)
            llm_base_url=os.environ.get("LLM_BASE_URL", "").strip()
            or os.environ.get("EMBED_BASE_URL", "http://127.0.0.1:11434/v1"),
            # Vide : pas de LLM, les questions renvoient les extraits pertinents sans formulation
            llm_model=os.environ.get("LLM_MODEL", "qwen2.5:7b").strip(),
            # LLM_API_KEY_FILE (secret Docker) lu par envfiles ; « aucune » : pas de clé
            llm_api_key="" if os.environ.get("LLM_API_KEY", "").strip().lower() in ("", "aucune", "none")
            else os.environ["LLM_API_KEY"].strip(),
            sources_file=os.environ.get("SOURCES_FILE", "sources.yaml"),
            convert_url=os.environ.get("CONVERT_URL", "").strip(),
        )


def _db_url() -> str:
    from ged.envfiles import with_password

    return with_password(os.environ.get("DATABASE_URL", "postgresql://ged:ged@127.0.0.1:5433/ged"),
                         os.environ.get("DATABASE_PASSWORD"))


class ConfigError(ValueError):
    pass


def _read(path: str | Path) -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}


RUNTIME_SECRETS = "__secrets__"  # secrets déchiffrés, ajoutés en mémoire au moment de créer le connecteur


def _secret(cfg: dict, key: str, where: str) -> str | None:
    """Les mots de passe ne sont jamais écrits dans le YAML ni dans la définition en base :
    seulement le nom de la variable d'environnement (key_env), ou un secret chiffré à part
    (key_chiffre, déchiffré par le catalogue des sources). La variable l'emporte."""
    if key in cfg:
        raise ConfigError(f"{where} : « {key} » ne doit pas figurer dans le fichier, utiliser « {key}_env »")
    var = cfg.get(f"{key}_env")
    if var:
        value = os.environ.get(var)
        if value is None:
            raise ConfigError(f"{where} : la variable d'environnement {var} n'est pas définie")
        return value
    stored = (cfg.get(RUNTIME_SECRETS) or {}).get(key)
    if stored is not None:
        return stored
    if cfg.get(f"{key}_chiffre"):
        raise ConfigError(f"{where} : secret enregistré dans la console, mais SECRETS_KEY n'est pas défini")
    return None


def _has_secret(src: dict, key: str) -> bool:
    return bool(str(src.get(f"{key}_env") or "").strip()) or bool(src.get(f"{key}_chiffre"))


_VAR = re.compile(r"\$\{(\w+)\}|\$(\w+)")


def _expand(value: str, where: str) -> str:
    """Remplace ${VAR} par la variable d'environnement : un même sources.yaml sert en local et dans Docker."""
    def sub(m: re.Match) -> str:
        name = m.group(1) or m.group(2)
        if name not in os.environ:
            raise ConfigError(f"{where} : la variable d'environnement {name} n'est pas définie (voir .env)")
        return os.environ[name]

    return _VAR.sub(sub, str(value))


SOURCE_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")


def source_extensions(path: str | Path | None, extra_extensions: frozenset[str] = frozenset()) -> frozenset[str]:
    """Formats indexés : de base, + bureautique (conversion PDF), + images (avec Alambic Light)."""
    from ged.alambic_light import IMAGE_EXTENSIONS
    from ged.connectors.filesystem import DEFAULT_EXTENSIONS

    light = bool(path) and bool(_read(path).get("alambic_light"))
    return DEFAULT_EXTENSIONS | extra_extensions | (IMAGE_EXTENSIONS if light else frozenset())


def validate_source(src: dict) -> dict:
    """Contrôle une définition de source (sources.yaml ou console) et la renvoie normalisée."""
    name = str(src.get("name") or "").strip()
    if not SOURCE_NAME.match(name):
        raise ConfigError(f"nom de source « {name} » : minuscules, chiffres, - et _ seulement (ex. partage-compta)")
    kind = src.get("type")
    where = f"source {name}"
    out: dict = {"name": name, "type": kind}
    if kind in ("filesystem", "ftp", "s3"):  # droits définis dossier par dossier
        acl = src.get("acl") or {}
        if not isinstance(acl, dict):
            raise ConfigError(f"{where} : acl doit associer un dossier à une liste de groupes")
        out["acl"] = {str(k).strip("/"): [str(g).strip() for g in (v or []) if str(g).strip()] for k, v in acl.items()}
    if kind == "filesystem":
        if not str(src.get("root") or "").strip():
            raise ConfigError(f"{where} : dossier racine (root) obligatoire")
        out["root"] = str(src["root"]).strip()
    elif kind == "ftp":
        if not str(src.get("host") or "").strip():
            raise ConfigError(f"{where} : serveur (host) obligatoire")
        if "password" in src:
            raise ConfigError(f"{where} : « password » ne doit pas figurer en clair, utiliser « password_env »")
        out.update({k: str(src.get(k) or "").strip() for k in ("host", "root", "username", "password_env")})
        out["username"] = out["username"] or "anonymous"
        out["port"] = int(_number(src.get("port") or 21, where, "port"))
        out["tls"] = _bool(src.get("tls", True))
    elif kind == "smb":
        for key in ("server", "share"):
            if not str(src.get(key) or "").strip():
                raise ConfigError(f"{where} : {key} obligatoire")
        if "password" in src:
            raise ConfigError(f"{where} : « password » ne doit pas figurer en clair, utiliser « password_env »")
        out.update({k: str(src.get(k) or "").strip() for k in ("server", "share", "path", "username", "password_env")})
        try:
            out["port"] = int(src.get("port") or 445)
            out["acl_refresh_hours"] = float(src.get("acl_refresh_hours") or 24)
        except (TypeError, ValueError):
            raise ConfigError(f"{where} : port et acl_refresh_hours sont des nombres") from None
        out["encrypt"] = bool(src.get("encrypt", False))
    elif kind in ("alfresco", "cmis"):
        for key in ("url", "username"):
            if not str(src.get(key) or "").strip():
                raise ConfigError(f"{where} : {key} obligatoire")
        if "password" in src:
            raise ConfigError(f"{where} : « password » ne doit pas figurer en clair, utiliser « password_env »")
        if not _has_secret(src, "password"):
            raise ConfigError(f"{where} : mot de passe obligatoire (saisi dans la console, ou variable password_env)")
        keys = ("url", "root", "username", "password_env") + (("repository",) if kind == "cmis" else ())
        out.update({k: str(src.get(k) or "").strip() for k in keys})
        _check_url(out["url"], where)
        out["acl_refresh_hours"] = _number(src.get("acl_refresh_hours") or 24, where, "acl_refresh_hours")
    elif kind == "s3":
        if not str(src.get("bucket") or "").strip():
            raise ConfigError(f"{where} : bucket obligatoire")
        if "secret_access_key" in src:
            raise ConfigError(f"{where} : « secret_access_key » ne doit pas figurer en clair, "
                              "utiliser « secret_access_key_env »")
        out.update({k: str(src.get(k) or "").strip() for k in
                    ("bucket", "prefix", "endpoint_url", "region", "access_key_id", "secret_access_key_env")})
        if out["endpoint_url"]:
            _check_url(out["endpoint_url"], where)
        if out["access_key_id"] and not _has_secret(src, "secret_access_key"):
            raise ConfigError(f"{where} : secret obligatoire avec une clé d'accès (saisi dans la console, "
                              "ou variable secret_access_key_env)")
        out["path_style"] = _bool(src.get("path_style", bool(out["endpoint_url"])))
    elif kind == "sharepoint":
        for key in ("site_url", "tenant_id", "client_id"):
            if not str(src.get(key) or "").strip():
                raise ConfigError(f"{where} : {key} obligatoire")
        if not _has_secret(src, "client_secret"):
            raise ConfigError(f"{where} : secret de l'application obligatoire (saisi dans la console, "
                              "ou variable client_secret_env)")
        if "client_secret" in src:
            raise ConfigError(f"{where} : « client_secret » ne doit pas figurer en clair, utiliser « client_secret_env »")
        out.update({k: str(src.get(k) or "").strip()
                    for k in ("site_url", "library", "folder", "tenant_id", "client_id", "client_secret_env")})
        out["library"] = out["library"] or "Documents"
        _check_url(out["site_url"], where)
        groups = src.get("site_groups") or {}
        if not isinstance(groups, dict):
            raise ConfigError(f"{where} : site_groups associe un groupe de site à une liste de groupes AD")
        out["site_groups"] = {str(k).strip(): [str(g).strip() for g in (v or []) if str(g).strip()]
                              for k, v in groups.items() if str(k).strip()}
        out["acl_refresh_hours"] = _number(src.get("acl_refresh_hours") or 24, where, "acl_refresh_hours")
    else:
        raise ConfigError(f"{where} : type « {kind} » non géré (filesystem, smb, ftp, s3, alfresco, cmis, sharepoint)")
    if not _bool(src.get("analyse_light", True)):
        out["analyse_light"] = False  # texte seul : pas de type ni de données (OCR des scans seulement)
    if _bool(src.get("ocr_ia", False)):
        out["ocr_ia"] = True  # texte seul : scans lus par l'IA d'Alambic Light (ocr=ia)
    from ged.secret_box import SECRET_FIELD

    field = SECRET_FIELD.get(kind)
    if field and src.get(f"{field}_chiffre"):
        out[f"{field}_chiffre"] = True
    minutes = src.get("synchro_minutes")
    if minutes not in (None, "", 0, "0"):
        try:
            minutes = float(minutes)
        except (TypeError, ValueError):
            raise ConfigError(f"{where} : synchro_minutes doit être un nombre") from None
        if minutes < 5:
            raise ConfigError(f"{where} : synchro_minutes au moins 5 (une synchro parcourt toute la source)")
        out["synchro_minutes"] = minutes
    return out


def _number(value, where: str, key: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{where} : {key} doit être un nombre") from None


def _bool(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "oui", "yes", "on")
    return bool(value)


def _check_url(url: str, where: str) -> None:
    if not re.match(r"^https?://[^/\s]+", url) and not url.startswith("${"):
        raise ConfigError(f"{where} : adresse invalide « {url} » (attendu : https://serveur…)")


def build_connector(src: dict, extensions: frozenset[str] | None = None):
    """Connecteur d'une source validée. Lève ConfigError/OSError si la source est inaccessible."""
    from ged.connectors.filesystem import DEFAULT_EXTENSIONS

    extensions = extensions or DEFAULT_EXTENSIONS
    runtime = src.get(RUNTIME_SECRETS)
    src = validate_source(src)
    if runtime:
        src[RUNTIME_SECRETS] = runtime
    name, where = src["name"], f"source {src['name']}"
    if src["type"] == "filesystem":
        return FileSystemConnector(name, _expand(src["root"], where), src["acl"], extensions)
    if src["type"] == "ftp":
        from ged.connectors.ftp import FtpConnector

        return FtpConnector(
            name, _expand(src["host"], where), _expand(src.get("root", ""), where), username=src["username"],
            password=_secret(src, "password", where) or "", port=src["port"], tls=src["tls"],
            acl_rules=src["acl"], extensions=extensions,
        )
    if src["type"] == "s3":
        from ged.connectors.s3 import S3Connector

        return S3Connector(
            name, _expand(src["bucket"], where), _expand(src.get("prefix", ""), where),
            endpoint_url=_expand(src.get("endpoint_url", ""), where) or None, region=src.get("region") or None,
            access_key_id=src.get("access_key_id") or None,
            secret_access_key=_secret(src, "secret_access_key", where), path_style=src["path_style"],
            acl_rules=src["acl"], extensions=extensions,
        )
    if src["type"] == "alfresco":
        from ged.connectors.alfresco import AlfrescoConnector

        return AlfrescoConnector(
            name, _expand(src["url"], where), _expand(src.get("root", ""), where),
            username=src["username"], password=_secret(src, "password", where) or "",
            acl_refresh_hours=src["acl_refresh_hours"], extensions=extensions,
        )
    if src["type"] == "cmis":
        from ged.connectors.cmis import CmisConnector

        return CmisConnector(
            name, _expand(src["url"], where), _expand(src.get("root", ""), where),
            repository=src.get("repository", ""), username=src["username"],
            password=_secret(src, "password", where) or "", acl_refresh_hours=src["acl_refresh_hours"],
            extensions=extensions,
        )
    if src["type"] == "sharepoint":
        from ged.connectors.sharepoint import SharePointConnector

        return SharePointConnector(
            name, _expand(src["site_url"], where), src.get("library", "Documents"), src.get("folder", ""),
            tenant_id=_expand(src["tenant_id"], where), client_id=_expand(src["client_id"], where),
            client_secret=_secret(src, "client_secret", where) or "", site_groups=src.get("site_groups"),
            acl_refresh_hours=src["acl_refresh_hours"], extensions=extensions,
        )
    from ged.connectors.smb import SmbConnector

    return SmbConnector(
        name, _expand(src["server"], where), _expand(src["share"], where), _expand(src.get("path", ""), where),
        username=src.get("username") or None, password=_secret(src, "password", where),
        port=src["port"], encrypt=src["encrypt"], acl_refresh_hours=src["acl_refresh_hours"],
        extensions=extensions,
    )


def load_connectors(path: str | Path, extra_extensions: frozenset[str] = frozenset()) -> dict:
    """Sources déclarées dans sources.yaml. `extra_extensions` : formats rendus indexables par un
    service (conversion bureautique)."""
    extensions = source_extensions(path, extra_extensions)
    connectors = {}
    for src in _read(path).get("sources", []):
        if "name" not in src:
            raise KeyError("name")
        connector = build_connector(src, extensions)
        connectors[connector.name] = connector
    return connectors


def read_sources(path: str | Path) -> list[dict]:
    """Définitions brutes des sources de sources.yaml (pour la console)."""
    return list(_read(path).get("sources", []) or [])


def load_converter(settings: Settings):
    """Conversion PDF des fichiers bureautiques (CONVERT_URL), ou None."""
    if not settings.convert_url:
        return None
    from ged.convert import OfficeConverter

    return OfficeConverter(settings.convert_url)


def load_analyzer(path: str | Path):
    """Client Alambic Light déclaré dans la section `alambic_light`, ou None."""
    cfg = _read(path).get("alambic_light")
    if not cfg:
        return None
    from ged.alambic_light import AlambicLight

    where = "alambic_light"
    envoi = str(cfg.get("envoyer", "tous"))
    if envoi not in ("tous", "scans"):
        raise ConfigError(f"{where} : envoyer vaut « tous » ou « scans », pas « {envoi} »")
    try:
        return AlambicLight(
            _expand(cfg.get("url", ""), where),
            api_key=_secret(cfg, "api_key", where),
            client_id=cfg.get("client_id"),
            client_secret=_secret(cfg, "client_secret", where),
            ia=bool(cfg.get("ia", True)),
            send_all=envoi == "tous",
            concurrency=int(cfg.get("concurrence", 2)),
            max_wait=float(cfg.get("delai_max", 300)),
            # Lien « Valider dans Alambic Light » de la console ; "" pour ne pas l'afficher
            validation_link=(_expand(str(cfg["lien_validation"]), where) if cfg.get("lien_validation")
                             else ("" if cfg.get("lien_validation") == "" else None)),
            validation_roles=tuple(cfg.get("roles_validation") or ("alambic-light:validation", "alambic-light:admin")),
        )
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc


def load_schedule(path: str | Path) -> dict[str, float]:
    """Synchros automatiques : `synchro_minutes` de chaque source (absent ou 0 = manuelle)."""
    out = {}
    for src in _read(path).get("sources", []):
        minutes = validate_source(src).get("synchro_minutes")
        if minutes:
            out[src["name"]] = minutes
    return out


def load_identity(path: str | Path):
    """Résolveur d'identités déclaré dans la section `identity`, ou None."""
    cfg = _read(path).get("identity")
    if not cfg:
        return None
    from ged.identity import LdapResolver, StaticResolver

    kind = cfg.get("type")
    if kind == "ldap":
        return LdapResolver(
            cfg["url"], cfg["base_dn"], cfg["bind_user"], _secret(cfg, "bind_password", "identity") or "",
            cache_ttl=float(cfg.get("cache_ttl", 300)),
        )
    if kind == "static":
        return StaticResolver(cfg.get("users") or {}, cfg.get("names") or {},
                              password=_secret(cfg, "password", "identity"))
    raise ConfigError(f"identity : type « {kind} » non géré (ldap, static)")
