"""Console d'administration : tableau de bord, sources et synchros, documents, bac à sable.

Rendu serveur (Jinja2) + HTMX pour les mises à jour partielles. Aucune ressource
externe : polices, htmx et logo sont servis localement (déploiement souverain,
réseau fermé).

Accès : comptes de rôle gestionnaire ou administrateur (session, ou identifiant et mot de passe
en HTTP Basic, ou clé d'API). La page Utilisateurs est réservée aux administrateurs.
"""
from __future__ import annotations

import datetime as dt
import math
import time
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ged.admin import SyncManager
from ged.answer import ask
from ged.models import RunStatus, Status

HERE = Path(__file__).parent
PAGE_SIZE = 50
PAGE_SIZES = (25, 50, 100, 200)
RUNS_PAGE = 10

DOC_STATUS = {
    Status.INDEXED: ("Indexé", "ok"),
    Status.OCR_REQUIRED: ("OCR requis", "warn"),
    Status.ERROR: ("En erreur", "bad"),
    Status.UNSUPPORTED: ("Non pris en charge", "muted"),
}
RUN_STATUS = {
    RunStatus.RUNNING: ("En cours", "run"),
    RunStatus.OK: ("Terminée", "ok"),
    RunStatus.PARTIAL: ("Terminée avec erreurs", "warn"),
    RunStatus.BLOCKED: ("Bloquée par le garde-fou", "bad"),
    RunStatus.FAILED: ("Échec", "bad"),
    RunStatus.INTERRUPTED: ("Interrompue", "muted"),
}
# Ordre d'affichage de la barre de distillation
PIPELINE = [Status.INDEXED, Status.OCR_REQUIRED, Status.ERROR, Status.UNSUPPORTED]

templates = Jinja2Templates(directory=str(HERE / "templates"))


def _when(ts: float | None) -> str:
    if ts is None:
        return "—"
    d = dt.datetime.fromtimestamp(ts)
    today = dt.date.today()
    if d.date() == today:
        return f"aujourd'hui à {d:%H:%M}"
    if d.date() == today - dt.timedelta(days=1):
        return f"hier à {d:%H:%M}"
    return f"{d:%d/%m/%Y à %H:%M}"


def _duration(start: float, end: float | None) -> str:
    secs = (end or time.time()) - start
    if secs < 60:
        return f"{secs:.0f} s"
    if secs < 3600:
        return f"{secs // 60:.0f} min {secs % 60:02.0f} s"
    return f"{secs // 3600:.0f} h {secs % 3600 // 60:02.0f} min"


def _size(n: int) -> str:
    for unit in ("o", "Ko", "Mo", "Go"):
        if n < 1024 or unit == "Go":
            return f"{n:.0f} {unit}" if unit == "o" else f"{n:.1f} {unit}".replace(".", ",")
        n /= 1024
    return str(n)


def _num(n: int | float) -> str:
    return f"{n:,.0f}".replace(",", " ")


# Libellés de la légende (pluriel, casse correcte)
DOC_LEGEND = {
    Status.INDEXED: "indexés",
    Status.OCR_REQUIRED: "à passer en OCR",
    Status.ERROR: "en erreur",
    Status.UNSUPPORTED: "non pris en charge",
}


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _dirname(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else ""


def _cites(text: str, cited: set[int]):
    """Réponse du LLM en HTML sûr : texte échappé d'abord, puis gras (**…**), puces et
    références [n] en pastilles. Une dernière ligne faite seulement de références (« [1], [3] »,
    ajoutée par certains modèles) est retirée : les sources sont listées dessous."""
    import re

    from markupsafe import Markup, escape

    lines = str(text).rstrip().splitlines()
    while lines and re.fullmatch(r"[\s,;.\-–]*(\[\d+\][\s,;.et\-–]*)+", lines[-1]):
        lines.pop()
    html = str(escape("\n".join(lines).strip()))
    html = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", html)
    html = re.sub(r"(?m)^\s*[-*•]\s+", "• ", html)
    return Markup(re.sub(r"\[(\d+)\]", lambda m: f'<span class="cite">{m.group(1)}</span>', html))


templates.env.filters.update(when=_when, size=_size, num=_num, basename=_basename, dirname=_dirname, cites=_cites)
templates.env.globals.update(DOC_STATUS=DOC_STATUS, DOC_LEGEND=DOC_LEGEND, RUN_STATUS=RUN_STATUS, duration=_duration,
                             pname=lambda p: p)  # remplacé par le résolveur d'identités s'il existe


def build_router(
    store, embedder, llm, connectors: dict, manager: SyncManager, auth, identity=None,
    analyzer=None, converter=None, catalog=None, thumbnail=None, reranker=None,
) -> APIRouter:
    from ged.models import Role

    accounts = auth.accounts

    def resolve_principals(identity, login: str, groups: str) -> frozenset[str]:
        """Principaux à tester (bac à sable, Droits) : d'un compte d'Alambic Search, sinon d'un
        identifiant de l'annuaire ; plus les groupes saisis."""
        from ged.api import resolve_principals as from_directory

        extra = frozenset(g.strip() for g in groups.split(",") if g.strip())
        account = store.get_user(login.strip()) if login.strip() else None
        if account is not None:
            return accounts.principals(account) | extra
        return from_directory(identity, login, groups)

    # Noms lisibles des SID dans toute la console (annuaire), sinon le principal tel quel
    resolve = identity.display_name if identity is not None else (lambda p: p)

    def pname(principal: str) -> str:
        name = resolve(principal)
        # SID sans nom (compte supprimé, annuaire injoignable) : forme courte, SID complet en infobulle
        if name.startswith("S-1-") and name.count("-") > 4:
            return f"compte inconnu …{name.rsplit('-', 1)[-1]}"
        return name

    templates.env.globals["pname"] = pname
    templates.env.globals["has_identity"] = identity is not None
    templates.env.globals["has_llm"] = llm is not None
    templates.env.globals["llm_name"] = llm.describe() if llm is not None and hasattr(llm, "describe") else None
    templates.env.globals["reranker"] = reranker.describe() if reranker is not None else None
    templates.env.globals["converter_url"] = converter.url if converter is not None else None
    templates.env.globals["light"] = analyzer.describe() if analyzer is not None else None
    from ged.alambic_light import FICHE_CHUNK_NO, _human, _render

    templates.env.globals.update(FICHE_CHUNK_NO=FICHE_CHUNK_NO)

    def light_validation(request: Request, meta) -> dict | None:
        """Bouton « Valider dans Alambic Light » d'un document dont des données sont à vérifier.
        Alambic Light demande ses propres identifiants et vérifie lui-même le droit de valider."""
        if analyzer is None or meta is None or not meta.light_uuid or meta.validated or not meta.to_check:
            return None
        if meta.light_status in ("EXPIRE", "EFFACE"):
            return None
        return {"url": analyzer.validation_url(meta.light_uuid), "allowed": None,
                "roles": ", ".join(analyzer.validation_roles)}

    templates.env.globals["light_validation"] = light_validation
    templates.env.filters.update(field_name=_human, field_value=lambda v: _render(v) or "—")

    def require_console(request: Request) -> None:
        """Gestionnaire ou administrateur. Navigateur sans session : page de connexion, puis retour."""
        user = auth.user(request)
        if user is None and request.method == "GET" and "text/html" in request.headers.get("accept", "") \
                and not request.headers.get("HX-Request"):
            from urllib.parse import quote

            target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
            raise HTTPException(303, "Connexion requise", headers={"Location": f"/connexion?suite={quote(target)}"})
        auth.require(request, Role.MANAGER)

    def same_origin(request: Request) -> None:
        """Anti-CSRF : un formulaire posté depuis un autre site ne doit rien pouvoir modifier (le
        navigateur renvoie seul le cookie de session et les identifiants HTTP Basic)."""
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return
        origin = request.headers.get("origin") or request.headers.get("referer")
        if origin:
            from urllib.parse import urlsplit

            if urlsplit(origin).netloc != request.headers.get("host", ""):
                raise HTTPException(403, "Requête refusée : elle ne vient pas de la console.")

    # pages HTML de la console : hors du schéma OpenAPI (qui ne décrit que l'API)
    router = APIRouter(prefix="/admin", include_in_schema=False,
                       dependencies=[Depends(require_console), Depends(same_origin)])

    def page(request: Request, name: str, nav: str, **ctx) -> HTMLResponse:
        me = auth.user(request)
        return templates.TemplateResponse(request, name, {"nav": nav, "me": me,
                                                          "is_admin": me is not None and me.role == Role.ADMIN, **ctx})

    # --- Tableau de bord -----------------------------------------------------------

    @router.get("", response_class=HTMLResponse)
    def dashboard(request: Request):
        stats = store.stats()
        totals = {s: sum(c.get(s, 0) for c in stats.values()) for s in PIPELINE}
        total = sum(totals.values())
        segments = [
            {"status": s, "count": totals[s], "pct": (100 * totals[s] / total) if total else 0}
            for s in PIPELINE if totals[s]
        ]
        activity = store.question_activity(14)
        asked = sum(a[1] for a in activity)
        unanswered = sum(a[2] for a in activity)
        peak = max([a[1] for a in activity] + [1])
        sources = []
        for name, conn in sorted(connectors.items()):
            c = stats.get(name, {})
            runs = store.list_runs(name, limit=1)
            sources.append({
                "name": name, "info": conn.describe() if hasattr(conn, "describe") else {},
                "indexed": c.get(Status.INDEXED, 0),
                "todo": c.get(Status.OCR_REQUIRED, 0) + c.get(Status.ERROR, 0),
                "last": runs[0] if runs else None, "running": manager.is_running(name),
                "progress": manager.progress(name),
            })
        todo, todo_total = store.list_documents(status=Status.ERROR, limit=6)
        ocr, ocr_total = store.list_documents(status=Status.OCR_REQUIRED, limit=max(0, 6 - len(todo)))
        return page(
            request, "dashboard.html", "dashboard",
            total=total, segments=segments, activity=activity, asked=asked, unanswered=unanswered,
            peak=peak, sources=sources, todo=todo + ocr, todo_total=todo_total + ocr_total,
        )

    # --- Sources et synchros -------------------------------------------------------

    def source_ctx(name: str, p: int = 1) -> dict:
        conn = connectors.get(name)
        runs_total = store.count_runs(name)
        runs_pages = max(1, math.ceil(runs_total / RUNS_PAGE))
        p = min(max(p, 1), runs_pages)
        return {
            "name": name,
            "editable": catalog.editable(name) if catalog else False,
            "error": catalog.errors.get(name) if catalog else None,
            "info": conn.describe() if hasattr(conn, "describe") else {},
            "running": manager.is_running(name),
            "progress": manager.progress(name),
            "every": manager.schedule.get(name),
            "next": manager.next_run(name),
            "runs": store.list_runs(name, limit=RUNS_PAGE, offset=(p - 1) * RUNS_PAGE),
            "runs_total": runs_total, "runs_p": p, "runs_pages": runs_pages,
            "counts": store.stats().get(name, {}),
            "can_refresh_acls": hasattr(conn, "invalidate_acls"),
            "active": conn is not None,
            "analyse": (catalog.configs.get(name, {}).get("analyse_light", True) if catalog and name in catalog.configs
                        else getattr(conn, "analyse_light", True)),
            "ocr_ia": (catalog.configs.get(name, {}).get("ocr_ia", False) if catalog and name in catalog.configs
                       else getattr(conn, "ocr_ia", False)),
        }

    @router.get("/sources", response_class=HTMLResponse)
    def sources(request: Request):
        names = catalog.names() if catalog else sorted(connectors)
        return page(request, "sources.html", "sources", sources=[source_ctx(n) for n in names],
                    can_edit=catalog is not None, pending_yaml=catalog.pending_yaml() if catalog else [])

    # --- Ajout, modification, suppression de sources (console) ---------------------

    def _pairs(text: str) -> dict[str, list[str]]:
        """« clé = a, b » par ligne → {clé: [a, b]} (droits par dossier, groupes de site)."""
        out: dict[str, list[str]] = {}
        for line in (text or "").splitlines():
            if "=" in line:
                key, values = line.split("=", 1)
                out[key.strip()] = [v.strip() for v in values.split(",") if v.strip()]
        return out

    def source_from_form(form) -> dict:
        """Formulaire → définition de source (même forme que dans sources.yaml)."""
        kind = form.get("type", "filesystem")
        get = lambda k: (form.get(k) or "").strip()  # noqa: E731
        cfg: dict = {"name": get("name").lower(), "type": kind}
        if kind in ("filesystem", "ftp", "s3"):
            cfg["acl"] = {("" if k.strip("/") in ("", "(racine)") else k.strip("/")): v
                          for k, v in _pairs(form.get("acl", "")).items()}
        if kind == "filesystem":
            cfg["root"] = get("root")
        elif kind == "smb":
            for key in ("server", "share", "path", "username", "password_env", "port", "acl_refresh_hours"):
                cfg[key] = get(key)
            cfg["encrypt"] = form.get("encrypt") == "true"
        elif kind == "ftp":
            cfg.update(host=get("host"), port=get("ftp_port"), root=get("ftp_root"), username=get("ftp_username"),
                       password_env=get("ftp_password_env"), tls=form.get("tls") == "true")
        elif kind == "s3":
            for key in ("bucket", "prefix", "region", "endpoint_url", "access_key_id", "secret_access_key_env"):
                cfg[key] = get(key)
            cfg["path_style"] = form.get("path_style") == "true" or bool(get("endpoint_url"))
        elif kind in ("alfresco", "cmis"):
            cfg.update(url=get("url"), root=get("ged_root"), username=get("ged_username"),
                       password_env=get("ged_password_env"), acl_refresh_hours=get("acl_refresh_hours"))
            if kind == "cmis":
                cfg["repository"] = get("repository")
        elif kind == "sharepoint":
            for key in ("site_url", "library", "folder", "tenant_id", "client_id", "client_secret_env",
                        "acl_refresh_hours"):
                cfg[key] = get(key)
            cfg["site_groups"] = _pairs(form.get("site_groups", ""))
        cfg["synchro_minutes"] = get("synchro_minutes") or None
        if form.get("analyse_light_field"):  # case présente dans le formulaire
            cfg["analyse_light"] = form.get("analyse_light") == "true"
            cfg["ocr_ia"] = form.get("ocr_ia") == "true"
        return cfg

    SECRET_PREFIX = {"smb": "smb", "ftp": "ftp", "alfresco": "ged", "cmis": "ged", "s3": "s3", "sharepoint": "sp"}

    def secret_from_form(form):
        """Secret saisi (jamais renvoyé dans la page, jamais journalisé)."""
        from ged.sources import SecretInput

        prefix = SECRET_PREFIX.get(form.get("type", ""))
        if prefix is None:
            return None
        return SecretInput(form.get(f"{prefix}_secret") or "", form.get(f"{prefix}_secret_clear") == "true")

    def form_values(cfg: dict | None) -> dict:
        cfg = dict(cfg or {"type": "filesystem", "port": 445, "acl_refresh_hours": 24, "encrypt": True})
        cfg["acl_text"] = "\n".join(f"{k or '(racine)'} = {', '.join(v)}" for k, v in (cfg.get("acl") or {}).items())
        cfg["site_groups_text"] = "\n".join(f"{k} = {', '.join(v)}" for k, v in (cfg.get("site_groups") or {}).items())
        return cfg

    def require_catalog():
        if catalog is None:
            raise HTTPException(404, "Gestion des sources indisponible")

    def source_form(request: Request, values: dict, *, editing: str | None = None, error: str | None = None,
                    status: int = 200) -> HTMLResponse:
        return templates.TemplateResponse(request, "source_form.html", {
            "nav": "sources", "v": values, "editing": editing, "error": error,
            "in_docker": Path("/.dockerenv").exists(),
            "stored": bool(editing) and catalog.has_secret(editing), "can_store": catalog.box is not None,
        }, status_code=status)

    @router.get("/sources/nouvelle", response_class=HTMLResponse)
    def new_source(request: Request):
        require_catalog()
        return source_form(request, form_values(None))

    @router.get("/sources/{name}/modifier", response_class=HTMLResponse)
    def edit_source(request: Request, name: str):
        require_catalog()
        if name not in catalog.configs:
            raise HTTPException(404, "Source inconnue")
        return source_form(request, form_values(catalog.configs[name]), editing=name)

    @router.post("/sources/tester", response_class=HTMLResponse)
    async def test_source(request: Request):
        require_catalog()
        form = await request.form()
        cfg, secret = source_from_form(form), secret_from_form(form)
        from starlette.concurrency import run_in_threadpool

        result = await run_in_threadpool(catalog.test, cfg, secret)
        return templates.TemplateResponse(request, "_source_test.html", {"t": result})

    async def save(request: Request, editing: str | None):
        from fastapi.responses import RedirectResponse
        from starlette.concurrency import run_in_threadpool

        from ged.sources import SourceError

        form = await request.form()
        cfg, secret = source_from_form(form), secret_from_form(form)
        try:
            saved = await run_in_threadpool(lambda: catalog.save(cfg, editing=editing, secret=secret))
        except (SourceError, ValueError) as exc:
            values = form_values(cfg)
            values["acl_text"] = form.get("acl", "")
            values["site_groups_text"] = form.get("site_groups", "")
            error = str(exc) + (" (le mot de passe saisi n'est pas réaffiché : à ressaisir)" if secret and secret.value else "")
            return source_form(request, values, editing=editing, error=error, status=422)
        if form.get("sync_now") == "true":
            manager.start(saved["name"])
        return RedirectResponse(f"/admin/sources#src-{saved['name']}", status_code=303)

    @router.post("/sources/nouvelle", response_class=HTMLResponse)
    async def create_source(request: Request):
        require_catalog()
        return await save(request, None)

    @router.post("/sources/{name}/modifier", response_class=HTMLResponse)
    async def update_source(request: Request, name: str):
        require_catalog()
        if name not in catalog.configs:
            raise HTTPException(404, "Source inconnue")
        return await save(request, name)

    @router.post("/sources/importer")
    def import_yaml_sources(request: Request):
        """Sources ajoutées à sources.yaml après le premier démarrage : importées sur demande."""
        require_catalog()
        done, failed = catalog.import_pending_yaml()
        names = catalog.names()
        return page(request, "sources.html", "sources", sources=[source_ctx(n) for n in names], can_edit=True,
                    pending_yaml=catalog.pending_yaml(), imported=done, import_errors=failed)

    @router.get("/sources/export")
    def export_sources():
        from fastapi.responses import Response

        require_catalog()
        return Response(catalog.export_yaml(), media_type="application/yaml; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="sources-alambic-search.yaml"'})

    @router.get("/sources/{name}/supprimer", response_class=HTMLResponse)
    def confirm_delete(request: Request, name: str):
        require_catalog()
        if name not in catalog.configs:
            raise HTTPException(404, "Source inconnue")
        return page(request, "source_delete.html", "sources", src_name=name,
                    count=sum(store.stats().get(name, {}).values()), running=manager.is_running(name))

    @router.post("/sources/{name}/supprimer")
    def delete_source(request: Request, name: str, confirm: str = Form("")):
        from fastapi.responses import RedirectResponse

        from ged.sources import SourceError

        require_catalog()
        if confirm != name:
            return page(request, "source_delete.html", "sources", src_name=name,
                        count=sum(store.stats().get(name, {}).values()), running=manager.is_running(name),
                        error="Recopiez exactement le nom de la source pour confirmer.")
        try:
            catalog.delete(name)
        except SourceError as exc:
            return page(request, "source_delete.html", "sources", src_name=name, count=0, running=True, error=str(exc))
        return RedirectResponse("/admin/sources", status_code=303)

    @router.post("/sources/{name}/sync", response_class=HTMLResponse)
    def start_sync(request: Request, name: str, full: bool = False):
        if name not in connectors:  # inconnue, ou source de la console inaccessible
            raise HTTPException(404, f"Source inconnue : {name}")
        manager.start(name, full=full)
        return templates.TemplateResponse(request, "_source_runs.html", {"s": source_ctx(name)})

    @router.get("/sources/{name}/runs", response_class=HTMLResponse)
    def source_runs(request: Request, name: str, p: int = 1):
        if name not in connectors:
            raise HTTPException(404, f"Source inconnue : {name}")
        return templates.TemplateResponse(request, "_source_runs.html", {"s": source_ctx(name, p)})

    # --- Documents -----------------------------------------------------------------

    @router.get("/documents", response_class=HTMLResponse)
    def documents(request: Request, source: str = "", status: str = "", q: str = "", type: str = "", p: int = 1,
                  n: int = PAGE_SIZE):
        n = n if n in PAGE_SIZES else PAGE_SIZE
        p = max(p, 1)
        rows, total = store.list_documents(source or None, status or None, q.strip() or None,
                                           offset=(p - 1) * n, limit=n, doc_type=type or None)
        pages = max(1, math.ceil(total / n))
        if p > pages:  # page au-delà de la fin (filtre resserré) : dernière page
            p = pages
            rows, total = store.list_documents(source or None, status or None, q.strip() or None,
                                               offset=(p - 1) * n, limit=n, doc_type=type or None)
        ctx = {
            "rows": rows, "total": total, "p": p, "pages": pages, "n": n, "page_sizes": PAGE_SIZES,
            "f": {"source": source, "status": status, "q": q, "type": type, "n": n}, "sources": sorted(connectors),
            "types": store.type_counts(),
        }
        if request.headers.get("HX-Request") and request.headers.get("HX-Target") == "doc-results":
            return templates.TemplateResponse(request, "_doc_table.html", ctx)
        return page(request, "documents.html", "documents", **ctx)

    @router.get("/documents/{source}/{doc_id:path}", response_class=HTMLResponse)
    def document_detail(request: Request, source: str, doc_id: str):
        row = store.document_row(source, doc_id)
        if row is None:
            raise HTTPException(404, "Document introuvable dans l'index")
        return templates.TemplateResponse(
            request, "_doc_detail.html", {"d": row, "chunks": store.document_chunks(source, doc_id, limit=30)}
        )

    @router.get("/vignettes/{source}/{doc_id:path}")
    def admin_thumbnail(source: str, doc_id: str, page: int = 1, l: int = 160):  # noqa: E741
        if thumbnail is None:
            raise HTTPException(404, "Vignettes indisponibles")
        return thumbnail(source, doc_id, page, l, None)

    @router.post("/documents/relire", response_class=HTMLResponse)
    def document_refresh(request: Request, source: str = Form(...), doc_id: str = Form(...)):
        """Relit tout de suite le résultat dans Alambic Light (après une validation), sans attendre la synchro."""
        from ged.sync import refresh_review

        row = store.document_row(source, doc_id)
        if row is None:
            raise HTTPException(404, "Document introuvable dans l'index")
        notice = None
        if analyzer is None or row.meta is None or not row.meta.light_uuid:
            notice = ("warn", "Ce document n'a pas de résultat Alambic Light.")
        else:
            try:
                new = refresh_review(store, embedder, analyzer, source, doc_id, row.meta)
            except Exception as exc:
                notice = ("bad", f"Alambic Light ne répond pas : {exc}")
            else:
                if new is None:
                    notice = ("info", "Rien de nouveau : pas encore validé dans Alambic Light.")
                elif new.validated:
                    notice = ("ok", "Données validées reprises : la fiche et les réponses sont à jour.")
                elif new.light_status == "EXPIRE":
                    notice = ("warn", "Résultat expiré dans Alambic Light : la validation ne peut plus être reprise.")
                else:
                    notice = ("info", "Résultat mis à jour.")
                row = store.document_row(source, doc_id)
        return templates.TemplateResponse(request, "_doc_detail.html", {
            "d": row, "chunks": store.document_chunks(source, doc_id, limit=30), "notice": notice})

    # --- Bac à sable ---------------------------------------------------------------

    # --- Droits : comment les groupes se rejoignent ------------------------------------

    @router.get("/droits", response_class=HTMLResponse)
    def rights(request: Request, login: str = "", groups: str = ""):
        from ged.rights import classify, user_warnings

        display = identity.display_name if identity is not None else None
        stats = store.stats()
        by_source: dict[str, list[dict]] = {}
        index_kinds: set[str] = set()
        for src, principal, allow, deny in store.principal_counts():
            kind = classify(principal, display)
            index_kinds.add(kind.code)
            by_source.setdefault(src, []).append({
                "principal": principal, "name": pname(principal), "kind": kind, "allow": allow, "deny": deny})
        sources = []
        for src in sorted(set(by_source) | set(stats)):
            rows = by_source.get(src, [])
            conn = connectors.get(src)
            info = conn.describe() if hasattr(conn, "describe") else {}
            sources.append({"name": src, "rows": rows[:300], "more": max(0, len(rows) - 300),
                            "total": stats.get(src, {}).get("indexed", 0),
                            "orphans": [r for r in rows if r["kind"].orphan],
                            "unmapped": info.get("unmapped") or []})
        test = None
        if login.strip() or groups.strip():
            try:
                from ged.identity import AUTHENTICATED_USERS
                from ged.ntsecurity import EVERYONE

                # tout utilisateur connecté les reçoit
                principals = resolve_principals(identity, login, groups) | {EVERYONE, AUTHENTICATED_USERS}
            except Exception as exc:  # annuaire injoignable
                test = {"error": f"L'annuaire n'a pas répondu ({type(exc).__name__} : {exc})."}
            else:
                visible = store.visible_counts(principals)
                test = {
                    "login": login.strip(), "groups": groups.strip(),
                    "principals": sorted(({"principal": p, "name": pname(p), "kind": classify(p)} for p in principals),
                                         key=lambda r: (r["kind"].code, r["name"].lower())),
                    "per_source": [{"name": s["name"], "visible": visible.get(s["name"], 0), "total": s["total"]}
                                   for s in sources],
                    "warnings": user_warnings(principals, index_kinds),
                    "account": store.get_user(login.strip()) if login.strip() else None,
                    "unknown": bool(login.strip()) and store.get_user(login.strip()) is None
                    and (identity is None or not identity.principals(login.strip())),
                }
        return page(request, "droits.html", "droits", sources=sources, test=test, login=login, groups=groups)

    @router.get("/sandbox", response_class=HTMLResponse)
    def sandbox(request: Request):
        return page(request, "sandbox.html", "sandbox", types=store.type_counts(), fields=store.field_names())

    def sandbox_criteria(request: Request, valeur: str, champ: str, doc_type: str, login: str, groups: str):
        from ged.criteria import search_by_criteria

        def error(msg: str) -> HTMLResponse:
            return templates.TemplateResponse(request, "_criteria_result.html", {"error": msg})

        if len(valeur.strip()) < 2:
            return error("Saisissez la valeur cherchée (au moins 2 caractères), par exemple un nom.")
        try:
            grp = resolve_principals(identity, login, groups)
        except Exception as exc:  # annuaire injoignable
            return error(f"L'annuaire n'a pas répondu ({type(exc).__name__} : {exc}).")
        t0 = time.perf_counter()
        result = search_by_criteria(store, grp, valeur, doc_type=doc_type or None, field=champ.strip() or None)
        labels = {code: label for code, label, _ in store.type_counts()}
        return templates.TemplateResponse(request, "_criteria_result.html", {
            "r": result, "type_label": labels.get(doc_type, doc_type), "groups": grp,
            "ms": int((time.perf_counter() - t0) * 1000),
        })

    @router.post("/sandbox", response_class=HTMLResponse)
    def sandbox_ask(request: Request, question: str = Form(""), login: str = Form(""), groups: str = Form(""),
                    k: int = Form(8), doc_type: str = Form(""), formuler: bool = Form(False),
                    mode: str = Form("question"), valeur: str = Form(""), champ: str = Form(""),
                    par_document: bool = Form(False), reclasser: bool = Form(False)):
        if mode == "criteres":
            return sandbox_criteria(request, valeur, champ, doc_type, login, groups)
        try:
            grp = resolve_principals(identity, login, groups)
        except Exception as exc:  # annuaire injoignable
            return templates.TemplateResponse(
                request, "_sandbox_result.html",
                {"error": f"L'annuaire n'a pas répondu ({type(exc).__name__} : {exc})."},
            )
        if login.strip() and identity is not None and not grp:
            return templates.TemplateResponse(
                request, "_sandbox_result.html", {"error": f"Utilisateur « {login.strip()} » introuvable dans l'annuaire."}
            )
        question = question.strip()
        if len(question) < 3:
            return templates.TemplateResponse(request, "_sandbox_result.html", {"error": "Saisissez une question d'au moins 3 caractères."})
        t0 = time.perf_counter()
        try:
            answer = ask(question, grp, store, embedder, llm, k=min(max(k, 1), 30),
                         doc_types=frozenset({doc_type}) if doc_type else None, generate=formuler,
                         per_document=par_document, reranker=reranker if reclasser else None)
        except Exception as exc:  # LLM ou embeddings injoignables
            return templates.TemplateResponse(
                request, "_sandbox_result.html",
                {"error": f"Le moteur n'a pas répondu ({type(exc).__name__} : {exc}). Vérifiez que le serveur LLM et le serveur d'embeddings sont démarrés."},
            )
        ms = int((time.perf_counter() - t0) * 1000)
        store.log_question(grp, question, len(answer.hits), len(answer.citations), ms, sandbox=True)
        cited = {c.n for c in answer.citations}
        return templates.TemplateResponse(
            request, "_sandbox_result.html",
            {"answer": answer, "groups": sorted(grp, key=lambda p: templates.env.globals["pname"](p).lower()),
             "login": login.strip(), "ms": ms, "cited": cited},
        )

    # --- Utilisateurs (administrateurs seulement) ------------------------------------------

    def require_admin(request: Request):
        return auth.require(request, Role.ADMIN)

    def users_page(request: Request, **ctx) -> HTMLResponse:
        users = store.list_users()
        keys: dict[int, int] = {}
        now = time.time()
        for k in store.list_api_keys():
            if k.usable(now):
                keys[k.user_id] = keys.get(k.user_id, 0) + 1
        return page(request, "users.html", "users", users=users, key_counts=keys, roles=Role.ORDER,
                    role_labels=Role.LABELS, form=ctx.pop("form", {}), **ctx)

    def user_page(request: Request, user, **ctx) -> HTMLResponse:
        principals, ad_error = None, None
        try:
            principals = sorted(accounts.principals(user), key=lambda p: pname(p).lower())
        except Exception as exc:  # annuaire injoignable
            ad_error = f"{type(exc).__name__} : {exc}"
        return page(request, "user_edit.html", "users", u=user, keys=store.list_api_keys(user.id), now=time.time(),
                    roles=Role.ORDER, role_labels=Role.LABELS, principals=principals, ad_error=ad_error,
                    ad_unknown=bool(user.ad_login) and identity is not None and ad_error is None
                    and not identity.principals(user.ad_login), **ctx)

    def get_user_or_404(user_id: int):
        user = store.get_user_by_id(user_id)
        if user is None:
            raise HTTPException(404, "Compte introuvable")
        return user

    @router.get("/utilisateurs", response_class=HTMLResponse)
    def users_list(request: Request, _=Depends(require_admin)):
        return users_page(request)

    @router.post("/utilisateurs", response_class=HTMLResponse)
    def users_create(request: Request, login: str = Form(""), nom: str = Form(""), role: str = Form(Role.USER),
                     mot_de_passe: str = Form(""), ad_login: str = Form(""), groupes: str = Form(""),
                     _=Depends(require_admin)):
        from ged.accounts import AccountError, split_groups

        form = {"login": login, "nom": nom, "role": role, "ad_login": ad_login, "groupes": groupes}
        try:
            user = accounts.create(login, nom, role, mot_de_passe, ad_login, split_groups(groupes))
        except AccountError as exc:
            return users_page(request, error=str(exc), form=form)
        return RedirectResponse(f"/admin/utilisateurs/{user.id}?cree=1", status_code=303)

    @router.get("/utilisateurs/{user_id}", response_class=HTMLResponse)
    def users_edit(request: Request, user_id: int, cree: bool = False, _=Depends(require_admin)):
        user = get_user_or_404(user_id)
        return user_page(request, user, notice=("ok", "Compte créé.") if cree else None)

    @router.post("/utilisateurs/{user_id}", response_class=HTMLResponse)
    def users_update(request: Request, user_id: int, nom: str = Form(""), role: str = Form(Role.USER),
                     ad_login: str = Form(""), groupes: str = Form(""), actif: bool = Form(False),
                     me=Depends(require_admin)):
        from ged.accounts import AccountError, split_groups

        user = get_user_or_404(user_id)
        try:
            if role not in Role.ORDER:
                raise AccountError("Rôle inconnu.")
            if user.id == me.id and (role != Role.ADMIN or not actif):
                raise AccountError("Vous ne pouvez pas retirer votre propre rôle d'administrateur ni désactiver "
                                   "votre compte : demandez-le à un autre administrateur.")
            accounts.guard_last_admin(user, role, actif)
        except AccountError as exc:
            return user_page(request, user, notice=("bad", str(exc)))
        user.name, user.role, user.ad_login = nom.strip() or user.login, role, ad_login.strip()
        user.groups, user.active = split_groups(groupes), actif
        store.update_user(user)
        import logging

        logging.getLogger("alambic_search.comptes").info(
            "Compte modifié par %s : %s (%s, %s)", me.login, user.login, role, "actif" if actif else "désactivé")
        return user_page(request, user, notice=("ok", "Modifications enregistrées."))

    @router.post("/utilisateurs/{user_id}/mot-de-passe", response_class=HTMLResponse)
    def users_password(request: Request, user_id: int, mot_de_passe: str = Form(""), _=Depends(require_admin)):
        from ged.accounts import AccountError

        user = get_user_or_404(user_id)
        try:
            accounts.set_password(user, mot_de_passe)
        except AccountError as exc:
            return user_page(request, user, notice=("bad", str(exc)))
        return user_page(request, user, notice=("ok", "Mot de passe défini. Les sessions ouvertes de ce compte "
                                                      "sont fermées ; communiquez-le-lui par un canal sûr."))

    @router.post("/utilisateurs/{user_id}/cles", response_class=HTMLResponse)
    def users_new_key(request: Request, user_id: int, nom: str = Form(""), jours: int = Form(0),
                      _=Depends(require_admin)):
        user = get_user_or_404(user_id)
        _key, value = accounts.new_api_key(user, nom, jours if jours > 0 else None)
        return user_page(request, user, new_key=value)

    @router.post("/utilisateurs/{user_id}/cles/{key_id}/revoquer", response_class=HTMLResponse)
    def users_revoke_key(request: Request, user_id: int, key_id: int, _=Depends(require_admin)):
        user = get_user_or_404(user_id)
        if not any(k.id == key_id for k in store.list_api_keys(user.id)):
            raise HTTPException(404, "Clé introuvable")
        store.revoke_api_key(key_id)
        return user_page(request, user, notice=("ok", "Clé révoquée."))

    @router.post("/utilisateurs/{user_id}/supprimer", response_class=HTMLResponse)
    def users_delete(request: Request, user_id: int, me=Depends(require_admin)):
        from ged.accounts import AccountError

        user = get_user_or_404(user_id)
        try:
            if user.id == me.id:
                raise AccountError("Vous ne pouvez pas supprimer votre propre compte.")
            accounts.guard_last_admin(user, deleting=True)
        except AccountError as exc:
            return user_page(request, user, notice=("bad", str(exc)))
        store.delete_user(user.id)
        return users_page(request, notice=("ok", f"Compte « {user.login} » supprimé, ainsi que ses clés d'API."))

    return router


def mount_static(app) -> None:
    app.mount("/admin/static", StaticFiles(directory=str(HERE / "static")), name="admin-static")
