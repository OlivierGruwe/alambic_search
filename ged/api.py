"""API HTTP du POC + console d'administration (/admin).

AUTHENTIFICATION : comptes d'Alambic Search (console, page Utilisateurs), avec trois façons de
se présenter (ged/auth.py) :
- session : page /connexion, puis cookie signé ;
- clé d'API : `Authorization: Bearer als_…` ou `X-API-Key: als_…` (créée dans « Mon compte ») ;
- identifiant et mot de passe : HTTP Basic.
Les droits sur les documents viennent du compte (AD rattaché et/ou groupes saisis par
l'administrateur) : l'appelant ne peut pas les choisir.

En-têtes `X-User` / `X-User-Groups` : acceptés SEULEMENT si TRUST_IDENTITY_HEADERS=true, c.-à-d.
derrière un proxy de confiance qui les pose lui-même. Désactivés par défaut.
"""
from __future__ import annotations

import logging
import mimetypes
import re
import time
from contextlib import asynccontextmanager
from dataclasses import asdict
from urllib.parse import quote

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from ged.admin import SyncManager
from ged.answer import ask, doc_path
from ged.embeddings import Embedder
from ged.llm import LLM
from ged.models import RunStatus
from ged.store import Store
from ged.sync import MassDeletionGuard, sync_source


class Echange(BaseModel):
    question: str = Field(max_length=2000)
    reponse: str = Field(default="", max_length=8000)


class AskRequest(BaseModel):
    question: str = Field(min_length=3, max_length=2000)
    k: int = Field(default=8, ge=1, le=30)
    types: list[str] = Field(default_factory=list, max_length=50,
                             description="Limiter aux types de documents Alambic (ex. FACTURE)")
    formuler: bool = Field(default=True, description="false : extraits pertinents sans formulation par le LLM")
    historique: list[Echange] = Field(default_factory=list, max_length=10,
                                      description="Échanges précédents (questions de suivi) : la question est "
                                                  "d'abord rendue autonome par le LLM")
    par_document: bool = Field(default=False, description="true : un résultat par document (k documents distincts, "
                               "passages regroupés) ; les citations [n] désignent alors des documents")


def _types(value: str) -> frozenset[str] | None:
    return frozenset(t.strip() for t in value.split(",") if t.strip()) or None


def _document_json(d) -> dict:
    return {"n": d.n, "path": d.path, "type": d.label, "url": d.url, "extrait": d.best.text,
            "score": round(d.best.score, 5), "pages": [{"page": p, "url": u} for p, u in d.pages]}


def resolve_principals(identity, login: str, groups: str) -> frozenset[str]:
    """Principaux d'un utilisateur : ceux de l'annuaire (si configuré) + groupes déclarés."""
    extra = frozenset(g.strip() for g in groups.split(",") if g.strip())
    if identity is not None and login.strip():
        return identity.principals(login) | extra
    return extra


def create_app(
    store: Store, embedder: Embedder, llm: LLM | None, connectors: dict, *,
    admin_password: str | None = None, identity=None, analyzer=None, converter=None,
    schedule: dict[str, float] | None = None,
    session_secret: str | None = None, cookie_secure: bool = False, trust_headers: bool | None = None,
    source_factory=None, yaml_sources: list[dict] | None = None, secret_box=None, reranker=None, scheduler: bool = True, auto_sync_empty: bool = False,
) -> FastAPI:
    import os

    from ged.session import LoginThrottle, Sessions

    if trust_headers is None:
        trust_headers = os.environ.get("TRUST_IDENTITY_HEADERS", "").lower() in ("1", "true", "oui", "yes")
    sessions = Sessions(session_secret, secure=cookie_secure)
    from ged.accounts import Accounts
    from ged.auth import Auth

    accounts = Accounts(store, identity)
    accounts.bootstrap(admin_password)  # premier démarrage : compte « admin » (ADMIN_PASSWORD)
    auth = Auth(sessions, accounts, identity, trust_headers=trust_headers, throttle=LoginThrottle())

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if scheduler:  # SCHEDULER_ENABLED=false : instance web seule (Swarm : le service worker planifie)
            manager.start_scheduler()
        yield
        # Arrêt (mise à jour, redémarrage par Swarm) : synchros en cours interrompues proprement AVANT
        # de fermer les clients HTTP, sinon leurs documents passent en erreur (« client has been closed »)
        import asyncio

        await asyncio.to_thread(manager.shutdown, float(os.environ.get("SHUTDOWN_WAIT", "25")))
        if hasattr(store, "close"):  # rend les connexions du pool à l'arrêt
            store.close()
        for client in (analyzer, converter, reranker, llm):
            if client is not None and hasattr(client, "close"):
                client.close()

    app = FastAPI(title="Alambic Search", lifespan=lifespan)
    manager = SyncManager(store, embedder, connectors, analyzer, converter, schedule,
                          auto_sync_empty=auto_sync_empty and scheduler)
    from ged.config import build_connector
    from ged.sources import SourceCatalog

    catalog = SourceCatalog(store, connectors, manager, source_factory or build_connector, yaml_sources,
                            box=secret_box)
    catalog.load()  # sources en base (et import de sources.yaml au premier démarrage)
    app.state.catalog = catalog

    slow_log = logging.getLogger("alambic_search.http")

    @app.middleware("http")
    async def timing(request: Request, call_next):
        """Durée de chaque requête : en-tête Server-Timing, et journal au-delà de 500 ms."""
        t0 = time.perf_counter()
        response = await call_next(request)
        ms = (time.perf_counter() - t0) * 1000
        response.headers["Server-Timing"] = f"app;dur={ms:.0f}"
        if ms > 500 and not request.url.path.startswith("/admin/static"):
            slow_log.warning("Requête lente : %s %s %.0f ms", request.method, request.url.path, ms)
        return response

    def user_groups(request: Request, x_user: str = Header(default=""),
                    x_user_groups: str = Header(default="")) -> frozenset[str]:
        """Principaux de l'appelant : session, clé d'API, HTTP Basic, ou en-têtes de confiance (ged/auth.py)."""
        return auth.request_principals(request, x_user, x_user_groups)

    from ged.thumbs import PLACEHOLDER, ThumbCache, fit_width, render

    thumbs = ThumbCache()

    def thumbnail(source: str, doc_id: str, page: int, width: int, groups: frozenset[str] | None) -> Response:
        """Vignette d'une page. Droits : ceux de l'index (les mêmes que pour afficher l'extrait) ;
        `groups` None : console d'administration. Document inconnu ou non autorisé : 404."""
        state = store.get_document(source, doc_id)
        if state is None or (groups is not None and not state.acl.permits(groups)):
            raise HTTPException(404, "Document introuvable")
        width, page = fit_width(width), max(1, page)
        headers = {"Cache-Control": "private, max-age=3600", "ETag": f'"{state.content_hash[:16]}-{page}-{width}"'}
        key = (source, doc_id, state.content_hash, page, width)
        image = thumbs.get(key)
        if image is None:
            connector = connectors.get(source)
            ref = connector.locate(doc_id, hint_path=state.path) if connector is not None else None
            try:
                from ged.thumbs import MAX_BYTES

                if ref is None or ref.size > MAX_BYTES:
                    raise ValueError("fichier absent ou trop gros")
                image = render(connector.read_bytes(ref), ref.path, page, width, converter)
            except Exception as exc:  # fichier abîmé, source injoignable, conversion en échec
                reason = f"{type(exc).__name__} : {exc}"
                logging.getLogger("alambic_search.vignettes").warning(
                    "Vignette impossible pour %s/%s : %s", source, state.path, reason)
                image = None
            else:
                reason = "format sans vignette" if image is None else ""
            if image is None:
                extra = {"Cache-Control": "private, max-age=300"}
                if groups is None:  # console : la raison, pour diagnostiquer (onglet Réseau du navigateur)
                    extra["X-Vignette-Erreur"] = quote(reason[:300])
                return Response(PLACEHOLDER, media_type="image/svg+xml", headers=extra)
            thumbs.put(key, image)
        return Response(image, media_type="image/webp", headers=headers)

    from ged.ui import build_router, mount_static

    mount_static(app)  # logo, polices, css, htmx : publics
    app.include_router(build_router(store, embedder, llm, connectors, manager, auth, identity, analyzer,
                                    converter, catalog, thumbnail, reranker))

    @app.get("/health")
    async def health():
        """Contrôle de santé (Swarm). Asynchrone : ne passe pas par le pool de fils des requêtes,
        donc répond même quand l'instance est occupée (synchro, questions longues)."""
        from ged import version

        return {"status": "ok", "version": version(), "sources": sorted(connectors), "llm": llm is not None}

    from ged.models import Role

    def require_api_manager(request: Request) -> None:
        """Opérations de gestion par l'API : rôle gestionnaire ou administrateur (session, clé
        d'API ou identifiant et mot de passe)."""
        auth.require(request, Role.MANAGER)

    @app.post("/sources/{name}/sync", dependencies=[Depends(require_api_manager)])
    def sync(name: str):
        """Synchro bloquante (scripts, cron). La console utilise la version en arrière-plan."""
        connector = connectors.get(name)
        if connector is None:
            raise HTTPException(404, f"Source inconnue : {name}")
        lock = None if manager.is_running(name) else store.sync_lock(name)  # verrou partagé entre instances
        if lock is None:
            raise HTTPException(409, "Une synchronisation de cette source est déjà en cours")
        try:
            run_id = store.start_run(name)
            try:
                report = sync_source(connector, store, embedder, analyzer=analyzer, converter=converter)
            except MassDeletionGuard as exc:
                store.finish_run(run_id, RunStatus.BLOCKED, message=str(exc))
                raise HTTPException(409, str(exc)) from exc
            except Exception as exc:
                store.finish_run(run_id, RunStatus.FAILED, message=f"{type(exc).__name__} : {exc}")
                raise
            store.finish_run(run_id, RunStatus.PARTIAL if report.errors else RunStatus.OK, asdict(report))
            return asdict(report)
        finally:
            lock.release()

    def document_url(h) -> str:
        """Lien de citation : un fichier bureautique s'ouvre en PDF, à la page citée."""
        office = converter is not None and converter.accepts(h.path)
        return f"/documents/{h.source}/{doc_path(h.doc_id)}{'?pdf=true' if office else ''}#page={h.page}"

    from ged.bandeau import build_context
    from ged.convert import OFFICE_EXTENSIONS
    from ged.thumbs import supported as thumb_supported
    from ged.ui import templates
    from ged.web import build_user_router

    def thumb_url(source: str, doc_id: str, path: str, page: int | None = 1, *, admin: bool = False) -> str | None:
        """Lien de la vignette d'une page, ou None si le format n'en a pas (texte…)."""
        if not thumb_supported(path, OFFICE_EXTENSIONS if converter is not None else frozenset()):
            return None
        return f"{'/admin' if admin else ''}/vignettes/{source}/{doc_path(doc_id)}?page={page or 1}"

    templates.env.globals["thumb_url"] = thumb_url
    from ged import version_label

    templates.env.globals["app_version"] = version_label()
    templates.env.globals["bandeau"] = lambda request: build_context(request, auth)

    app.include_router(build_user_router(store, embedder, llm, auth, document_url, reranker=reranker))

    def _criteria_json(r) -> dict:
        return {
            "filtre": {"type": r.doc_type, "champ": r.field, "valeur": r.value},
            "complet": r.complete,
            "documents": [
                {"path": m.path, "type": m.doc_type, "libelle": m.label,
                 "url": f"/documents/{m.source}/{doc_path(m.doc_id)}",
                 "champs": [{"champ": k, "valeur": v, "aVerifier": c} for k, v, c in m.fields]}
                for m in r.documents
            ],
            "mentions": [
                {"path": m.path, "type": m.doc_type, "libelle": m.label, "page": m.page, "extrait": m.excerpt,
                 "url": f"/documents/{m.source}/{doc_path(m.doc_id)}#page={m.page}"}
                for m in r.mentions
            ],
        }

    @app.get("/recherche")
    def criteria(valeur: str, type: str = "", champ: str = "", mentions: bool = True,
                 groups: frozenset[str] = Depends(user_groups)):
        """Recherche par critères : TOUS les documents dont un champ extrait contient `valeur`
        (mot entier, sans casse ni accents), du type demandé ; plus, à part, les documents dont
        seul le texte la mentionne. Ex. : /recherche?type=FACTURE&valeur=GRUWE"""
        from ged.criteria import search_by_criteria

        if len(valeur.strip()) < 2:
            raise HTTPException(422, "valeur : au moins 2 caractères")
        r = search_by_criteria(store, groups, valeur, doc_type=type or None, field=champ or None, mentions=mentions)
        return _criteria_json(r)

    @app.get("/search")
    def search(q: str, k: int = 8, types: str = "", par_document: bool = False,
               groups: frozenset[str] = Depends(user_groups)):
        """`types` : codes Alambic séparés par des virgules (ex. FACTURE,CONTRAT).
        `par_document=true` : un résultat par document (k documents distincts, pages trouvées)."""
        from ged.answer import dedupe, group_by_document

        k = min(max(k, 1), 30)
        if par_document:
            hits = dedupe(store.search(q, embedder.embed([q])[0], groups, k=max(k * 4, reranker.candidates if reranker else 0),
                                       doc_types=_types(types)))
            if reranker is not None:
                hits = reranker.rerank(q, hits)
            return [_document_json(d) for d in group_by_document(hits, k, document_url)]
        hits = store.search(q, embedder.embed([q])[0], groups, k=max(k, reranker.candidates if reranker else 0),
                            doc_types=_types(types))
        if reranker is not None:
            hits = reranker.rerank(q, dedupe(hits))
        hits = hits[:k]
        return [
            {"path": h.path, "page": h.page, "type": h.doc_label, "score": round(h.score, 5), "excerpt": h.text[:300],
             "url": document_url(h)}
            for h in hits
        ]

    @app.post("/ask")
    def ask_question(req: AskRequest, groups: frozenset[str] = Depends(user_groups)):
        t0 = time.perf_counter()
        answer = ask(req.question, groups, store, embedder, llm, k=req.k, doc_types=frozenset(req.types) or None,
                     generate=req.formuler, url_for=document_url, per_document=req.par_document, reranker=reranker,
                     history=[e.model_dump() for e in req.historique])
        ms = int((time.perf_counter() - t0) * 1000)
        store.log_question(groups, req.question, len(answer.hits), len(answer.citations), ms)
        return {
            "text": answer.text,
            "formulee": answer.generated,
            "duree_ms": ms,  # temps total de la réponse
            "duree_llm_ms": answer.llm_ms,  # dont attente du LLM (rédaction, critères, suivi)
            "etapes_ms": answer.steps,  # vecteur, recherche, reclassement
            # question de suivi rendue autonome (null si la question se suffisait)
            "question_comprise": answer.reformulated,
            # identifiants de la question (n° de permis, de facture…) : seuls les documents qui les contiennent
            "identifiants": list(answer.identifiers),
            "identifiants_introuvables": list(answer.identifiers_absent),
            "citations": [asdict(c) for c in answer.citations],
            "extraits": [
                {"n": i, "path": h.path, "page": h.page, "type": h.doc_label, "text": h.text}
                for i, h in enumerate(answer.hits, 1)
            ] if not answer.generated and answer.documents is None else [],
            # par_document : un élément par document ([n] des citations = document n)
            "documents": [_document_json(d) for d in answer.documents] if answer.documents is not None else None,
            # Question de liste traduite en critères : liste exhaustive (voir /recherche)
            "liste": _criteria_json(answer.criteria) if answer.criteria is not None else None,
        }

    @app.get("/vignettes/{source}/{doc_id:path}")
    def document_thumbnail(source: str, doc_id: str, page: int = 1, l: int = 160,  # noqa: E741
                           groups: frozenset[str] = Depends(user_groups)):
        """Aperçu (WebP) de la page `page`, largeur `l` (160, 320 ou 480 px)."""
        return thumbnail(source, doc_id, page, l, groups)

    @app.get("/documents/{source}/{doc_id:path}")
    def open_document(request: Request, source: str, doc_id: str, pdf: bool = False,
                      x_user: str = Header(default=""), x_user_groups: str = Header(default="")):
        """`pdf=true` : fichier bureautique converti à la volée, pour ouvrir la page citée (#page=N)."""
        try:
            groups = user_groups(request, x_user, x_user_groups)
        except HTTPException as exc:
            if exc.status_code == 401 and "text/html" in request.headers.get("accept", ""):
                from ged.web import login_redirect  # lien ouvert sans être connecté : connexion puis retour

                return login_redirect(request.url.path + (f"?{request.url.query}" if request.url.query else ""))
            raise
        connector = connectors.get(source)
        state = store.get_document(source, doc_id)
        if connector is None or state is None:
            raise HTTPException(404, "Document introuvable")
        # Revérification EN DIRECT dans la source : les droits ont pu changer depuis l'indexation.
        ref = connector.locate(doc_id, hint_path=state.path)
        if ref is None:
            raise HTTPException(404, "Document absent de la source")
        if not connector.acl(ref).permits(groups):
            raise HTTPException(403, "Accès refusé")
        filename = ref.path.rsplit("/", 1)[-1]
        if pdf and converter is not None and converter.accepts(filename):
            from ged.convert import ConversionFailed

            try:
                content = converter.to_pdf(filename, connector.read_bytes(ref))
            except ConversionFailed as exc:
                raise HTTPException(502, f"Conversion en PDF impossible : {exc}") from exc
            return Response(content, media_type="application/pdf", headers={
                "Content-Disposition": f"inline; filename*=UTF-8''{quote(filename + '.pdf')}",
                "Cache-Control": "private, no-store",
            })
        if hasattr(connector, "absolute_path"):  # fichier local : envoyé directement
            return FileResponse(connector.absolute_path(ref), filename=filename)
        media = mimetypes.guess_type(filename)[0] or "application/octet-stream"  # SMB : lu via le connecteur
        return Response(connector.read_bytes(ref), media_type=media,
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"})

    return app


def app_from_env() -> FastAPI:
    """Point d'entrée uvicorn : `uvicorn ged.api:app_from_env --factory`."""
    import logging
    import os

    from ged.envfiles import load_env_files

    load_env_files()  # secrets Docker : ADMIN_PASSWORD_FILE=/run/secrets/… → ADMIN_PASSWORD

    import psycopg
    import yaml
    from psycopg_pool import PoolTimeout

    from ged.config import (
        ConfigError,
        Settings,
        build_connector,
        load_analyzer,
        load_converter,
        load_identity,
        read_sources,
        source_extensions,
    )
    from ged.convert import OFFICE_EXTENSIONS
    from ged.embeddings import OpenAICompatEmbedder
    from ged.llm import load_llm
    from ged.store import PgStore

    log = logging.getLogger("alambic_search")
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(message)s")

    s = Settings.from_env()
    store = PgStore(s.database_url, s.embed_dim)
    safe_url = re.sub(r"//([^:/@]+):[^@]*@", r"//\1:***@", s.database_url)
    log.info("Connexion à la base %s…", safe_url)
    # Base pas encore prête (Swarm : services démarrés ensemble, initialisation de PostgreSQL) :
    # on patiente DB_WAIT secondes plutôt que de planter et redémarrer en boucle.
    deadline = time.monotonic() + float(os.environ.get("DB_WAIT", "90"))
    while True:
        try:
            store.init()
            break
        except (psycopg.OperationalError, PoolTimeout) as exc:
            store.close()  # pool neuf à l'essai suivant
            detail = str(exc).splitlines()[0]
            if time.monotonic() >= deadline:
                raise SystemExit(
                    f"\nImpossible de se connecter à PostgreSQL ({safe_url}).\n"
                    "  - En local : lancer la base du projet avec `make db` (port 5433),\n"
                    "    ou corriger DATABASE_URL dans .env (voir .env.example).\n"
                    "  - Swarm : vérifier le service base (make stack-ps, docker service ps <stack>_db).\n"
                    "  - La base doit disposer de l'extension pgvector.\n"
                    f"Détail : {detail}\n"
                ) from None
            log.warning("Base pas encore joignable (%s) : nouvel essai dans 5 s", detail)
            time.sleep(5)
    log.info("Base prête. Lecture de la configuration %s…", s.sources_file)
    try:
        converter = load_converter(s)
        extra = OFFICE_EXTENSIONS if converter else frozenset()
        extensions = source_extensions(s.sources_file, extra)
        identity = load_identity(s.sources_file)
        from ged.config import _read

        for old in ("oidc", "portail"):
            if os.path.isfile(s.sources_file) and _read(s.sources_file).get(old):
                log.warning("sources.yaml : section « %s » ignorée (connexion unique et portail retirés : "
                            "comptes gérés dans la console, page Utilisateurs)", old)
        analyzer = load_analyzer(s.sources_file)
        # Sources : en base. sources.yaml n'est lu que pour l'import du premier démarrage.
        yaml_sources = read_sources(s.sources_file) if os.path.isfile(s.sources_file) else []
    except yaml.YAMLError as exc:
        raise SystemExit(
            f"\nsources.yaml n'est pas un YAML valide ({s.sources_file}) :\n{exc}\n"
            "Astuce : écrire les chemins Windows avec des / (D:/documents), pas des \\.\n"
        ) from None
    except KeyError as exc:
        raise SystemExit(f"\nConfiguration invalide ({s.sources_file}) : clé obligatoire manquante {exc}\n") from None
    except IsADirectoryError:
        raise SystemExit(
            f"\n{s.sources_file} est un dossier, pas un fichier. Docker l'a sans doute créé à la place\n"
            "d'un sources.yaml introuvable : supprimer ce dossier, recréer le fichier\n"
            "(cp sources.example.yaml sources.yaml) puis relancer.\n"
        ) from None
    except (ConfigError, OSError) as exc:
        raise SystemExit(f"\nConfiguration invalide ({s.sources_file}) : {exc}\n") from None
    llm = load_llm(s)
    if llm is None:
        log.info("Réponses : extraits pertinents sans formulation (LLM_MODEL vide)")
    elif llm.remote:
        log.info("Réponses : formulées par %s — service en ligne : la question et les extraits autorisés "
                 "lui sont envoyés (les vecteurs restent calculés en local)", llm.describe())
    else:
        log.info("Réponses : formulées par %s", llm.describe())
    try:
        import pypdfium2  # noqa: F401

        log.info("Vignettes : PDF et images%s", " ; bureautique via la conversion PDF" if converter else "")
    except ImportError:
        log.warning("Vignettes des PDF indisponibles : pypdfium2 absent (uv sync, ou reconstruire l'image Docker)")
    from ged.rerank import load_reranker

    reranker = load_reranker()
    if reranker is not None:
        ok, detail = reranker.check()  # n'empêche pas le démarrage : repli sur l'ordre hybride
        (log.info if ok else log.warning)("Reclassement des résultats : %s", detail)
    else:
        log.info("Reclassement des résultats non configuré (RERANK_URL) : ordre de la recherche hybride")
    if converter is not None:
        ok, detail = converter.check()
        (log.info if ok else log.warning)("Conversion PDF des fichiers bureautiques : %s", detail)
    else:
        log.info("Conversion PDF non configurée (CONVERT_URL) : .docx sans pagination, autres formats Office ignorés")
    if analyzer is not None:
        log.info("Alambic Light : %s (OCR, type et données ; envoi : %s)", analyzer.url,
                 "tous les PDF et images" if analyzer.send_all else "documents sans texte")
        result = analyzer.check()  # n'empêche pas le démarrage : la console reste utilisable
        for label, ok, detail in result["etapes"]:
            (log.info if ok else log.warning)("  Alambic Light, %s : %s", label.lower(), detail)
        if not result["ok"]:
            log.warning("Alambic Light inutilisable en l'état : les documents à analyser passeront en erreur")
    else:
        log.info("Alambic Light non configuré : les scans resteront « OCR requis »")
    from ged.secret_box import SecretBox, SecretError

    try:
        secret_box = SecretBox.from_env()
    except (SecretError, OSError) as exc:
        raise SystemExit(f"SECRETS_KEY : {exc}") from exc
    if secret_box is not None:
        log.info("Secrets des sources : chiffrés en base (AES-256-GCM, clé %s)", secret_box.key_id)
    else:
        log.info("Secrets des sources : variables d'environnement seulement (SECRETS_KEY non défini)")
    app = create_app(
        store,
        OpenAICompatEmbedder(s.embed_base_url, s.embed_model, s.embed_dim),
        llm,
        {},  # sources : chargées depuis la base par la console (SourceCatalog)
        admin_password=os.environ.get("ADMIN_PASSWORD") or None,
        session_secret=os.environ.get("SESSION_SECRET") or None,
        cookie_secure=os.environ.get("COOKIE_SECURE", "").lower() in ("1", "true", "oui", "yes"),
        identity=identity,
        analyzer=analyzer,
        converter=converter,
        source_factory=lambda cfg: build_connector(cfg, extensions),
        yaml_sources=yaml_sources,
        secret_box=secret_box,
        reranker=reranker,
        scheduler=os.environ.get("SCHEDULER_ENABLED", "true").lower() not in ("0", "false", "non", "no", "off"),
        # index vide (base perdue sans sauvegarde) : toutes les sources resynchronisées au démarrage
        auto_sync_empty=os.environ.get("AUTO_SYNC_EMPTY", "true").lower() not in ("0", "false", "non", "no", "off"),
    )
    log.info("Sources : %s. Console : /admin", ", ".join(app.state.catalog.names()) or "aucune (à ajouter dans la console)")
    return app
