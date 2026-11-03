"""Sources : définies en base (table source_configs), gérées depuis la console.

La base est la seule référence pour la liste des sources. sources.yaml ne sert qu'à préparer
une installation : ses sources sont importées au premier démarrage, puis le fichier n'est plus
lu pour les sources (il garde l'annuaire et Alambic Light). Une source ajoutée plus tard dans
le fichier est signalée dans la console, avec un bouton pour l'importer.

Secrets (mots de passe, clés) : jamais dans la définition de la source. Soit saisis dans la
console et chiffrés à part (table source_secrets, clé SECRETS_KEY hors de la base, voir
ged.secret_box), soit laissés dans l'environnement (password_env : nom de la variable).
Supprimer une source retire ses documents de l'index ; les fichiers ne sont jamais touchés.
"""
from __future__ import annotations

import datetime as dt
import logging
import time
from collections.abc import Callable

import yaml

from ged.config import RUNTIME_SECRETS, ConfigError, build_connector, validate_source
from ged.secret_box import SECRET_FIELD, SecretBox, SecretError

log = logging.getLogger(__name__)

TEST_MAX_FILES = 2000
TEST_MAX_SECONDS = 10.0
IMPORTED = "sources_importees"  # réglage : date du premier import de sources.yaml


class SourceError(Exception):
    """Opération refusée sur une source (message destiné à l'administrateur)."""


class SecretInput:
    """Secret saisi dans le formulaire : nouvelle valeur, effacement, ou rien (on garde l'existant)."""

    def __init__(self, value: str = "", clear: bool = False):
        self.value = value or ""
        self.clear = clear

    def __repr__(self) -> str:  # jamais la valeur dans un journal ou une trace
        return f"SecretInput({'nouveau' if self.value else 'effacer' if self.clear else 'inchangé'})"


def bootstrap(store, yaml_sources: list[dict]) -> list[str]:
    """Premier démarrage : importe les sources de sources.yaml en base, une seule fois.
    Renvoie les noms importés."""
    if store.get_setting(IMPORTED) is not None:
        return []
    known = {c["name"] for c in store.list_source_configs()}
    imported = []
    for raw in yaml_sources:
        try:
            cfg = validate_source(raw)
        except ConfigError as exc:
            log.error("sources.yaml : source ignorée à l'import (%s)", exc)
            continue
        if cfg["name"] not in known:
            store.save_source_config(cfg)
            imported.append(cfg["name"])
    store.set_setting(IMPORTED, {"le": dt.datetime.now().isoformat(timespec="seconds"), "sources": imported})
    if imported:
        log.info("Sources importées de sources.yaml (désormais gérées dans la console) : %s", ", ".join(imported))
    return imported


def load_sources(store, factory: Callable[[dict], object]) -> tuple[dict, dict[str, str], list[dict]]:
    # factory reçoit la définition de la source ; le catalogue y ajoute les secrets déchiffrés
    """Connecteurs des sources enregistrées. Une source inaccessible n'empêche pas les autres :
    elle est renvoyée dans `errors` (nom → message)."""
    connectors, errors = {}, {}
    configs = store.list_source_configs()
    for cfg in configs:
        try:
            connectors[cfg["name"]] = factory(cfg)
        except Exception as exc:  # partage injoignable au démarrage : visible et modifiable dans la console
            errors[cfg["name"]] = f"{type(exc).__name__} : {exc}"
            log.warning("Source %s inaccessible : %s", cfg["name"], exc)
    return connectors, errors, configs


def tag(connector, cfg: dict):
    """Réglages de synchro portés par le connecteur (lus par sync_source)."""
    try:
        connector.analyse_light = cfg.get("analyse_light", True)
    except AttributeError:  # objet sans attributs libres : réglage par défaut
        pass
    return connector


def runtime_config(store, box: SecretBox | None, cfg: dict, plain: str | None = None) -> dict:
    """Définition + secret déchiffré (en mémoire seulement, pour créer le connecteur)."""
    field = SECRET_FIELD.get(cfg.get("type", ""))
    if not field or not cfg.get(f"{field}_chiffre"):
        return cfg
    if plain is None:
        token = store.get_source_secrets(cfg["name"]).get(field)
        if token is None:
            raise ConfigError(f"source {cfg['name']} : secret enregistré introuvable, à ressaisir")
        if box is None:
            raise ConfigError(f"source {cfg['name']} : secret enregistré dans la console, "
                              "mais SECRETS_KEY n'est pas défini")
        plain = box.decrypt(cfg["name"], field, token)
    return {**cfg, RUNTIME_SECRETS: {field: plain}}


class SourceCatalog:
    def __init__(self, store, connectors: dict, manager, factory: Callable[[dict], object] = build_connector,
                 yaml_sources: list[dict] | None = None, box: SecretBox | None = None):
        self.store = store
        self.box = box  # None : pas de SECRETS_KEY, les secrets restent dans l'environnement
        self.connectors = connectors  # partagé avec l'API, la console et les synchros
        self.manager = manager
        self.factory = factory
        self.yaml_sources = list(yaml_sources or [])
        self.configs: dict[str, dict] = {}  # sources en base (modifiables)
        self.errors: dict[str, str] = {}  # sources inaccessibles au démarrage

    def load(self) -> None:
        bootstrap(self.store, self.yaml_sources)
        connectors, self.errors, configs = load_sources(self.store, lambda cfg: tag(self.factory(self._runtime(cfg)), cfg))
        for cfg in configs:
            self.configs[cfg["name"]] = cfg
            if cfg["name"] in connectors:
                self.connectors[cfg["name"]] = connectors[cfg["name"]]
                self._schedule(cfg)

    def names(self) -> list[str]:
        return sorted(set(self.connectors) | set(self.configs))

    def editable(self, name: str) -> bool:
        return name in self.configs

    def pending_yaml(self) -> list[str]:
        """Sources de sources.yaml absentes de la base (ajoutées au fichier après l'import)."""
        names = []
        for raw in self.yaml_sources:
            name = str(raw.get("name") or "")
            if name and name not in self.configs and name not in self.connectors:
                names.append(name)
        return names

    def _schedule(self, cfg: dict) -> None:
        minutes = cfg.get("synchro_minutes")
        if minutes:
            self.manager.schedule[cfg["name"]] = minutes
        else:
            self.manager.schedule.pop(cfg["name"], None)
        self.manager.ensure_scheduler()

    def _activate(self, cfg: dict, plain: str | None = None) -> None:
        self.connectors[cfg["name"]] = tag(self.factory(self._runtime(cfg, plain)), cfg)
        self._schedule(cfg)
        self.errors.pop(cfg["name"], None)

    # --- Secrets chiffrés -----------------------------------------------------------

    def has_secret(self, name: str) -> bool:
        cfg = self.configs.get(name) or {}
        field = SECRET_FIELD.get(cfg.get("type", ""))
        return bool(field and cfg.get(f"{field}_chiffre"))

    def _runtime(self, cfg: dict, plain: str | None = None) -> dict:
        return runtime_config(self.store, self.box, cfg, plain)

    def _resolve_secret(self, raw: dict, secret: SecretInput | None, existing: str | None):
        """→ (définition avec l'indicateur key_chiffre, secret en clair ou None, jeton chiffré à garder ou None)."""
        raw = {k: v for k, v in raw.items() if not k.endswith("_chiffre")}
        name = str(raw.get("name") or "")
        field = SECRET_FIELD.get(str(raw.get("type") or ""))
        if not field:
            return raw, None, None
        secret = secret or SecretInput()
        tokens = self.store.get_source_secrets(existing) if existing else {}
        token, plain = tokens.get(field), None
        if secret.value:
            if self.box is None:
                raise SourceError("Pour enregistrer un mot de passe dans la console, définissez SECRETS_KEY "
                                  "(python -m ged.secret_box cle) ; sinon indiquez une variable d'environnement.")
            token, plain = self.box.encrypt(name, field, secret.value), secret.value
        elif secret.clear:
            token = None
        elif token is not None and self.box is not None:
            try:
                plain = self.box.decrypt(name, field, token)
            except SecretError as exc:
                raise SourceError(str(exc)) from exc
        if token is not None:
            raw[f"{field}_chiffre"] = True
        return raw, plain, token

    # --- Opérations de la console --------------------------------------------------

    def test(self, raw: dict, secret: SecretInput | None = None) -> dict:
        """Essaie la source sans rien enregistrer : accessible ? combien de fichiers ?"""
        name = str(raw.get("name") or "")
        try:
            raw, plain, _ = self._resolve_secret(raw, secret, name if name in self.configs else None)
            cfg = validate_source(raw)
            connector = self.factory(self._runtime(cfg, plain))
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        count, sample, t0 = 0, [], time.monotonic()
        try:
            for ref in connector.list_documents():
                count += 1
                if len(sample) < 5:
                    sample.append(ref.path)
                if count >= TEST_MAX_FILES or time.monotonic() - t0 > TEST_MAX_SECONDS:
                    return {"ok": True, "count": count, "partial": True, "sample": sample}
        except Exception as exc:
            return {"ok": False, "error": f"Parcours impossible : {type(exc).__name__} : {exc}", "count": count}
        return {"ok": True, "count": count, "partial": False, "sample": sample,
                "rules_empty": cfg["type"] in ("filesystem", "ftp", "s3") and not cfg.get("acl")}

    def save(self, raw: dict, *, editing: str | None = None, secret: SecretInput | None = None) -> dict:
        """Crée (editing=None) ou modifie la source `editing`. Active tout de suite.
        `secret` : mot de passe saisi (chiffré avant enregistrement), effacé, ou inchangé."""
        raw, plain, token = self._resolve_secret(raw, secret, editing if editing in self.configs else None)
        cfg = validate_source(raw)
        name = cfg["name"]
        if editing is not None and name != editing:
            raise SourceError("Le nom d'une source ne se modifie pas (il identifie ses documents dans l'index).")
        if editing is None and (name in self.configs or name in self.connectors):
            raise SourceError(f"Une source « {name} » existe déjà.")
        if editing is not None and name not in self.configs:
            raise SourceError(f"Source inconnue : {name}")
        if self.manager.is_running(name):
            raise SourceError("Synchronisation en cours : attendez qu'elle se termine.")
        try:
            self._activate(cfg, plain)  # échoue si la source est inaccessible : rien n'est enregistré
        except ConfigError as exc:
            raise SourceError(str(exc)) from exc
        except Exception as exc:  # dossier introuvable, serveur injoignable, compte refusé…
            raise SourceError(f"Source inaccessible : {exc}") from exc
        self.store.save_source_config(cfg)
        for field in set(SECRET_FIELD.values()):  # le secret du type de la source, rien d'autre
            keep = field == SECRET_FIELD.get(cfg["type"]) and token is not None
            if keep or editing is not None:
                self.store.set_source_secret(name, field, token if keep else None)
        self.configs[name] = cfg
        log.info("Source %s %s depuis la console", name, "modifiée" if editing else "ajoutée")
        return cfg

    def import_pending_yaml(self) -> tuple[list[str], list[str]]:
        """Importe les sources de sources.yaml qui ne sont pas encore en base.
        Renvoie (importées, refusées avec leur raison)."""
        done, failed = [], []
        for raw in self.yaml_sources:
            if str(raw.get("name") or "") not in self.pending_yaml():
                continue
            try:
                done.append(self.save(raw)["name"])
            except (SourceError, ConfigError) as exc:
                failed.append(f"{raw.get('name')} : {exc}")
        return done, failed

    def delete(self, name: str) -> int:
        """Retire la source et ses documents de l'index. Renvoie le nombre de documents retirés."""
        if name not in self.configs:
            raise SourceError(f"Source inconnue : {name}")
        if self.manager.is_running(name):
            raise SourceError("Synchronisation en cours : attendez qu'elle se termine.")
        self.connectors.pop(name, None)
        self.manager.schedule.pop(name, None)
        self.errors.pop(name, None)
        self.configs.pop(name, None)
        self.store.delete_source_config(name)
        removed = self.store.purge_source(name)
        log.warning("Source %s supprimée depuis la console : %d document(s) retiré(s) de l'index", name, removed)
        return removed

    def export_yaml(self) -> str:
        """Liste des sources au format de sources.yaml (sauvegarde, versionnement, autre installation)."""
        chiffres = sorted(n for n in self.configs if self.has_secret(n))
        header = (f"# Sources d'Alambic Search, exportées le {dt.datetime.now():%d/%m/%Y à %H:%M}.\n"
                  "# Aucun mot de passe ici : seulement les variables qui les contiennent (password_env).\n")
        if chiffres:
            header += ("# Secrets chiffrés en base, non exportés (à ressaisir dans la console ou à fournir "
                       f"par une variable *_env) : {', '.join(chiffres)}\n")
        sources = [{k: v for k, v in self.configs[n].items() if not k.endswith("_chiffre")} for n in sorted(self.configs)]
        body = yaml.safe_dump({"sources": sources},
                              allow_unicode=True, sort_keys=False, default_flow_style=False)
        return header + body
