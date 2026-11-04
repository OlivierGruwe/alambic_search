"""Pages des utilisateurs : connexion, recherche, et « Mon compte » (mot de passe, clés d'API).

Connexion avec un compte d'Alambic Search (créé dans la console, page Utilisateurs). Les droits
sur les documents viennent du compte : identifiant AD rattaché (groupes lus dans l'annuaire) et/ou
groupes saisis par l'administrateur. L'utilisateur ne les choisit jamais.
"""
from __future__ import annotations

import json
import logging
import time
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ged.accounts import AccountError, password_tag, verify_password
from ged.answer import ask
from ged.auth import client_ip

log = logging.getLogger("alambic_search.auth")

BAD_LOGIN = "Identifiant ou mot de passe incorrect."
THROTTLED = "Trop d'essais infructueux. Réessayez dans un quart d'heure."


def _safe_next(target: str | None) -> str:
    """Retour après connexion : chemin local seulement (pas de redirection vers un autre site)."""
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return "/"
    return target


def same_origin(request: Request) -> None:
    """Anti-CSRF des formulaires : refuse un POST venu d'un autre site."""
    origin = request.headers.get("origin") or request.headers.get("referer")
    if origin:
        from urllib.parse import urlsplit

        if urlsplit(origin).netloc != request.headers.get("host", ""):
            raise HTTPException(403, "Requête refusée : elle ne vient pas d'Alambic Search.")


def build_user_router(store, embedder, llm, auth, document_url, reranker=None) -> APIRouter:
    from ged.ui import templates

    router = APIRouter(include_in_schema=False)
    sessions, accounts, throttle = auth.sessions, auth.accounts, auth.throttle

    def login_page(request: Request, *, error: str | None = None, login: str = "", suite: str = "/",
                   status: int = 200, info: str | None = None) -> HTMLResponse:
        return templates.TemplateResponse(
            request, "login.html",
            {"error": error, "info": info, "login": login, "suite": _safe_next(suite),
             "no_account": not store.list_users()},
            status_code=status,
        )

    def open_session(response, user) -> None:
        sessions.set_cookie(response, user.login, user.name, pw=password_tag(user))

    # --- Connexion ---------------------------------------------------------------------

    @router.get("/connexion", response_class=HTMLResponse)
    def login_form(request: Request, suite: str = "/", deconnecte: bool = False):
        session = auth.session(request)
        if session and accounts.from_session(session):
            return RedirectResponse(_safe_next(suite), status_code=303)
        return login_page(request, suite=suite, info="Vous êtes déconnecté." if deconnecte else None)

    @router.post("/connexion", response_class=HTMLResponse)
    def login(request: Request, identifiant: str = Form(""), mot_de_passe: str = Form(""), suite: str = Form("/")):
        same_origin(request)
        user_key, ip_key = f"u:{identifiant.strip().lower()}", f"ip:{client_ip(request)}"
        if throttle.blocked(user_key, ip_key):
            log.warning("Connexion bloquée (trop d'essais) : %r depuis %s", identifiant[:60], client_ip(request))
            return login_page(request, error=THROTTLED, login=identifiant, suite=suite, status=429)
        user = accounts.authenticate(identifiant, mot_de_passe)
        if user is None:
            throttle.failed(user_key, ip_key)
            log.warning("Connexion refusée : %r depuis %s", identifiant[:60], client_ip(request))
            return login_page(request, error=BAD_LOGIN, login=identifiant, suite=suite, status=401)
        throttle.succeeded(user_key)
        store.touch_login(user.id)
        log.info("Connexion : %s (%s) depuis %s", user.login, user.role, client_ip(request))
        response = RedirectResponse(_safe_next(suite), status_code=303)
        open_session(response, user)
        return response

    @router.post("/deconnexion")
    def logout(request: Request):
        response = RedirectResponse("/connexion?deconnecte=1", status_code=303)
        sessions.clear_cookie(response)
        return response

    def session_user(request: Request):
        session = auth.session(request)
        return accounts.from_session(session) if session else None

    # --- Mon compte ------------------------------------------------------------------------

    def account_page(request: Request, user, **ctx) -> HTMLResponse:
        return templates.TemplateResponse(request, "account.html", {
            "nav": "account", "user": user, "keys": store.list_api_keys(user.id), "now": time.time(), **ctx})

    @router.get("/compte", response_class=HTMLResponse)
    def my_account(request: Request):
        user = session_user(request)
        if user is None:
            return login_redirect("/compte")
        return account_page(request, user)

    @router.post("/compte/mot-de-passe", response_class=HTMLResponse)
    def change_password(request: Request, actuel: str = Form(""), nouveau: str = Form(""), confirmation: str = Form("")):
        same_origin(request)
        user = session_user(request)
        if user is None:
            return login_redirect("/compte")
        if user.password_hash and not verify_password(actuel, user.password_hash):
            return account_page(request, user, pw_error="Mot de passe actuel incorrect.")
        if nouveau != confirmation:
            return account_page(request, user, pw_error="Les deux saisies du nouveau mot de passe diffèrent.")
        try:
            accounts.set_password(user, nouveau)
        except AccountError as exc:
            return account_page(request, user, pw_error=str(exc))
        response = account_page(request, user, pw_ok="Mot de passe changé. Vos autres sessions sont fermées.")
        open_session(response, user)  # la session courante continue avec le nouveau mot de passe
        return response

    @router.post("/compte/cles", response_class=HTMLResponse)
    def create_key(request: Request, nom: str = Form(""), jours: int = Form(0)):
        same_origin(request)
        user = session_user(request)
        if user is None:
            return login_redirect("/compte")
        _key, value = accounts.new_api_key(user, nom, jours if jours > 0 else None)
        return account_page(request, user, new_key=value)

    @router.post("/compte/cles/{key_id}/revoquer", response_class=HTMLResponse)
    def revoke_key(request: Request, key_id: int):
        same_origin(request)
        user = session_user(request)
        if user is None:
            return login_redirect("/compte")
        if not any(k.id == key_id for k in store.list_api_keys(user.id)):
            raise HTTPException(404, "Clé introuvable")
        store.revoke_api_key(key_id)
        log.info("Clé d'API révoquée par %s : %s", user.login, key_id)
        return account_page(request, user, key_ok="Clé révoquée : elle ne fonctionne plus.")

    # --- Recherche ---------------------------------------------------------------------

    @router.get("/", response_class=HTMLResponse)
    def home(request: Request):
        if session_user(request) is None:
            return RedirectResponse("/connexion", status_code=303)
        return RedirectResponse("/rechercher", status_code=303)

    @router.get("/rechercher", response_class=HTMLResponse)
    def search_page(request: Request):
        user = session_user(request)
        if user is None:
            return RedirectResponse("/connexion?suite=/rechercher", status_code=303)
        return templates.TemplateResponse(request, "search.html", {
            "nav": "search", "user": user, "types": store.type_counts(), "has_llm": llm is not None})

    @router.post("/rechercher", response_class=HTMLResponse)
    def search(request: Request, mode: str = Form("question"), question: str = Form(""), valeur: str = Form(""),
               doc_type: str = Form(""), formuler: bool = Form(False), par_document: bool = Form(False),
               historique: str = Form("")):
        user = session_user(request)

        def fragment(**ctx) -> HTMLResponse:
            return templates.TemplateResponse(request, "_user_result.html", {"url": document_url, **ctx})

        if user is None:  # session expirée pendant que la page était ouverte
            response = fragment(error="Votre session a expiré.")
            response.headers["HX-Redirect"] = "/connexion?suite=/rechercher"
            return response
        try:
            groups = accounts.principals(user)
        except Exception as exc:
            log.error("Annuaire injoignable : %s", exc)
            return fragment(error="L'annuaire ne répond pas : vos droits ne peuvent pas être vérifiés. "
                                  "Réessayez dans un instant.")
        t0 = time.perf_counter()
        if mode == "criteres":
            from ged.criteria import search_by_criteria

            if len(valeur.strip()) < 2:
                return fragment(error="Saisissez ce que vous cherchez (au moins 2 caractères), par exemple un nom.")
            result = search_by_criteria(store, groups, valeur, doc_type=doc_type or None)
            return fragment(r=result, ms=int((time.perf_counter() - t0) * 1000))
        question = question.strip()
        if len(question) < 3:
            return fragment(error="Saisissez une question d'au moins 3 caractères.")
        try:
            history = _history(historique)
            answer = ask(question, groups, store, embedder, llm, doc_types=frozenset({doc_type}) if doc_type else None,
                         generate=formuler, url_for=document_url, per_document=par_document, reranker=reranker,
                         history=history)
        except Exception as exc:
            log.error("Question en échec : %s", exc)
            return fragment(error="Le moteur de recherche ne répond pas. Réessayez dans un instant.")
        ms = int((time.perf_counter() - t0) * 1000)
        store.log_question(groups, question, len(answer.hits), len(answer.citations), ms)
        # échanges à renvoyer avec la question suivante (questions de suivi)
        turn = {"question": answer.reformulated or question, "reponse": answer.text[:1500]}
        return fragment(answer=answer, r=answer.criteria, ms=ms, asked=question,
                        historique=json.dumps((history + [turn])[-3:], ensure_ascii=False))

    return router


def _history(raw: str) -> list[dict]:
    """Historique renvoyé par la page (champ caché) : validé, borné, jamais fiable au-delà du texte."""
    try:
        data = json.loads(raw) if raw else []
    except ValueError:
        return []
    if not isinstance(data, list):
        return []
    return [{"question": str(t.get("question") or "")[:2000], "reponse": str(t.get("reponse") or "")[:2000]}
            for t in data[-3:] if isinstance(t, dict)]


def login_redirect(path: str) -> RedirectResponse:
    return RedirectResponse(f"/connexion?suite={quote(path)}", status_code=303)
