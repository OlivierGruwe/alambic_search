"""Pages des utilisateurs : connexion, portail, questions et listes.

Connexion :
- avec Keycloak (section `oidc`) : connexion unique, partagée avec Alambic Light et les autres
  applications du portail ; le mot de passe n'est jamais vu par Alambic Search ;
- sinon : identifiant et mot de passe Windows, vérifiés par l'AD (section `identity`).

L'utilisateur ne choisit jamais ses groupes : ils viennent de l'annuaire (ou du jeton Keycloak).
Il ne voit que les documents qu'il est autorisé à lire.
"""
from __future__ import annotations

import logging
import time
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ged.answer import ask
from ged.session import LoginThrottle

log = logging.getLogger("alambic_search.auth")

BAD_LOGIN = "Identifiant ou mot de passe incorrect."
THROTTLED = "Trop d'essais infructueux. Réessayez dans un quart d'heure."
NO_IDENTITY = ("La connexion des utilisateurs n'est pas configurée : ajoutez la section « oidc » (Keycloak) "
               "ou « identity » (annuaire) dans sources.yaml.")
OIDC_COOKIE = "alambic_oidc"  # connexion en cours : state, nonce, PKCE (10 min)
IDT_COOKIE = "alambic_idt"  # jeton d'identité : déconnexion unique sans confirmation
SEARCH_APP = {"nom": "Rechercher dans les documents", "url": "/rechercher", "interne": True,
              "description": "Questions en langage courant et listes de documents, dans la limite de vos droits."}


def _safe_next(target: str | None) -> str:
    """Retour après connexion : chemin local seulement (pas de redirection vers un autre site)."""
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return "/"
    return target


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def visible_apps(portal: list[dict], roles: list[str]) -> list[dict]:
    """Applications du portail que l'utilisateur peut ouvrir (rôle requis, s'il y en a un)."""
    owned = set(roles)
    return [app for app in [SEARCH_APP, *portal] if not app.get("role") or app["role"] in owned]


def build_user_router(store, embedder, llm, auth, throttle: LoginThrottle, document_url,
                      portal: list[dict]) -> APIRouter:
    from ged.ui import templates

    router = APIRouter(include_in_schema=False)
    sessions, identity, oidc = auth.sessions, auth.identity, auth.oidc

    def login_page(request: Request, *, error: str | None = None, login: str = "", suite: str = "/",
                   status: int = 200, info: str | None = None) -> HTMLResponse:
        return templates.TemplateResponse(
            request, "login.html",
            {"error": error, "info": info, "login": login, "suite": _safe_next(suite),
             "available": auth.can_login, "oidc": oidc is not None, "no_identity": NO_IDENTITY},
            status_code=status,
        )

    def callback_url(request: Request) -> str:
        base = oidc.public_url or str(request.base_url).rstrip("/")
        return f"{base}/auth/callback"

    # --- Connexion ---------------------------------------------------------------------

    @router.get("/connexion", response_class=HTMLResponse)
    def login_form(request: Request, suite: str = "/", deconnecte: bool = False, erreur: str = ""):
        if auth.session(request):
            return RedirectResponse(_safe_next(suite), status_code=303)
        if oidc is None or deconnecte or erreur:
            return login_page(request, suite=suite, error=erreur or None,
                              info="Vous êtes déconnecté." if deconnecte else None)
        return start_oidc(request, suite)

    @router.get("/connexion/keycloak")
    def start_oidc(request: Request, suite: str = "/"):
        from ged.oidc import OidcError, new_login_state

        if oidc is None:
            return RedirectResponse("/connexion", status_code=303)
        st = new_login_state()
        try:
            url = oidc.authorize_url(callback_url(request), st["state"], st["nonce"], st["challenge"])
        except OidcError as exc:
            log.error("Connexion Keycloak impossible : %s", exc)
            return login_page(request, error="Le service de connexion ne répond pas. Réessayez dans un instant.",
                              status=503)
        response = RedirectResponse(url, status_code=303)
        response.set_cookie(OIDC_COOKIE, sessions.pack({**st, "suite": _safe_next(suite)}, 600), max_age=600,
                            httponly=True, samesite="lax", secure=sessions.secure, path="/auth")
        return response

    @router.get("/auth/callback")
    def oidc_callback(request: Request, code: str = "", state: str = "", error: str = "",
                      error_description: str = ""):
        from ged.oidc import OidcError

        pending = sessions.unpack(request.cookies.get(OIDC_COOKIE))
        if oidc is None:
            return RedirectResponse("/connexion", status_code=303)
        if error:  # refusé chez Keycloak (accès non accordé, connexion annulée)
            log.warning("Keycloak a refusé la connexion : %s %s", error, error_description)
            return login_page(request, error="Connexion refusée par le service de connexion.", status=401)
        if pending is None or not state or state != pending.get("state") or not code:
            return login_page(request, error="Connexion expirée ou interrompue : recommencez.", status=400)
        try:
            tokens = oidc.exchange(code, callback_url(request), pending["verifier"])
            claims = oidc.validate_id_token(tokens["id_token"], pending["nonce"])
            user = oidc.user(claims)
            if tokens.get("access_token"):
                # Keycloak ne met les rôles des clients (alambic-light:validation…) que dans le jeton
                # d'accès : on les y lit aussi, après en avoir vérifié la signature et le destinataire
                access = oidc.user(oidc.validate_access_token(tokens["access_token"]))
                if access.login == user.login:
                    user = type(user)(user.login, user.name, user.groups or access.groups,
                                      sorted(set(user.roles) | set(access.roles)))
        except (OidcError, KeyError) as exc:
            log.warning("Connexion Keycloak refusée : %s", exc)
            return login_page(request, error=f"Connexion impossible : {exc}", status=401)
        extra = {"roles": user.roles}
        if oidc.groups_mode == "jeton":
            extra["groups"] = user.groups
        else:  # le compte doit exister dans l'annuaire : sinon aucun droit, inutile d'entrer
            try:
                principals = auth.principals({"u": user.login})
            except Exception as exc:
                log.error("Annuaire injoignable pendant une connexion : %s", exc)
                return login_page(request, error="L'annuaire ne répond pas. Réessayez dans un instant.", status=503)
            if not principals:
                log.warning("Connexion Keycloak de %s : compte absent de l'annuaire", user.login)
                return login_page(request, error=f"Le compte « {user.login} » n'existe pas dans l'annuaire de "
                                                 "l'entreprise.", status=403)
        log.info("Connexion (Keycloak) : %s depuis %s, rôles : %s, groupes : %s", user.login, _client_ip(request),
                 ", ".join(user.roles) or "aucun", ", ".join(user.groups) or "aucun")
        response = RedirectResponse(pending.get("suite") or "/", status_code=303)
        sessions.set_cookie(response, user.login, user.name, **extra)
        response.delete_cookie(OIDC_COOKIE, path="/auth")
        if len(tokens.get("id_token", "")) < 3500:
            response.set_cookie(IDT_COOKIE, tokens["id_token"], max_age=sessions.max_age, httponly=True,
                                samesite="lax", secure=sessions.secure, path="/")
        return response

    @router.post("/connexion", response_class=HTMLResponse)
    def login(request: Request, identifiant: str = Form(""), mot_de_passe: str = Form(""), suite: str = Form("/")):
        if oidc is not None:  # connexion unique : le mot de passe se saisit chez Keycloak seulement
            return login_page(request, error="La connexion se fait par le service de connexion de l'entreprise.",
                              status=405)
        if identity is None:
            return login_page(request, error=NO_IDENTITY, status=503)
        user_key, ip_key = f"u:{identifiant.strip().lower()}", f"ip:{_client_ip(request)}"
        if throttle.blocked(user_key, ip_key):
            log.warning("Connexion bloquée (trop d'essais) : %r depuis %s", identifiant[:60], _client_ip(request))
            return login_page(request, error=THROTTLED, login=identifiant, suite=suite, status=429)
        try:
            account = identity.authenticate(identifiant, mot_de_passe)
        except Exception as exc:  # annuaire injoignable : ce n'est pas un échec de l'utilisateur
            log.error("Annuaire injoignable pendant une connexion : %s", exc)
            return login_page(request, error="L'annuaire ne répond pas. Réessayez dans un instant.",
                              login=identifiant, suite=suite, status=503)
        if account is None:
            throttle.failed(user_key, ip_key)
            log.warning("Connexion refusée : %r depuis %s", identifiant[:60], _client_ip(request))
            return login_page(request, error=BAD_LOGIN, login=identifiant, suite=suite, status=401)
        throttle.succeeded(user_key)
        log.info("Connexion : %s depuis %s", account.login, _client_ip(request))
        response = RedirectResponse(_safe_next(suite), status_code=303)
        sessions.set_cookie(response, account.login, account.name)
        return response

    @router.post("/deconnexion")
    def logout(request: Request):
        target = "/connexion?deconnecte=1"
        if oidc is not None:  # déconnexion unique : ferme aussi la session Keycloak (et donc les autres applis)
            base = oidc.public_url or str(request.base_url).rstrip("/")
            try:
                target = oidc.logout_url(f"{base}/connexion?deconnecte=1", request.cookies.get(IDT_COOKIE)) or target
            except Exception as exc:
                log.warning("Déconnexion Keycloak impossible : %s", exc)
        response = RedirectResponse(target, status_code=303)
        sessions.clear_cookie(response)
        response.delete_cookie(IDT_COOKIE, path="/")
        return response

    # --- Portail et recherche -----------------------------------------------------------

    @router.get("/", response_class=HTMLResponse)
    def home(request: Request):
        user = auth.session(request)
        if user is None:
            return RedirectResponse("/connexion", status_code=303)
        apps = visible_apps(portal, user.get("r", []))
        if len(apps) <= 1:  # pas d'autre application : directement la recherche
            return search_page(request)
        return templates.TemplateResponse(request, "portal.html", {"user": user, "apps": apps})

    @router.get("/rechercher", response_class=HTMLResponse)
    def search_page(request: Request):
        user = auth.session(request)
        if user is None:
            return RedirectResponse("/connexion?suite=/rechercher", status_code=303)
        return templates.TemplateResponse(request, "search.html", {
            "user": user, "types": store.type_counts(), "has_llm": llm is not None,
            "has_portal": len(visible_apps(portal, user.get("r", []))) > 1,
        })

    @router.post("/rechercher", response_class=HTMLResponse)
    def search(request: Request, mode: str = Form("question"), question: str = Form(""), valeur: str = Form(""),
               doc_type: str = Form(""), formuler: bool = Form(False), par_document: bool = Form(False)):
        user = auth.session(request)

        def fragment(**ctx) -> HTMLResponse:
            return templates.TemplateResponse(request, "_user_result.html", {"url": document_url, **ctx})

        if user is None:  # session expirée pendant que la page était ouverte
            response = fragment(error="Votre session a expiré.")
            response.headers["HX-Redirect"] = "/connexion?suite=/rechercher"
            return response
        try:
            groups = auth.principals(user)
        except Exception as exc:
            log.error("Annuaire injoignable : %s", exc)
            return fragment(error="L'annuaire ne répond pas : vos droits ne peuvent pas être vérifiés. "
                                  "Réessayez dans un instant.")
        if not groups:
            return fragment(error="Votre compte n'a plus accès à l'annuaire (désactivé ou supprimé).")
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
            answer = ask(question, groups, store, embedder, llm, doc_types=frozenset({doc_type}) if doc_type else None,
                         generate=formuler, url_for=document_url, per_document=par_document)
        except Exception as exc:
            log.error("Question en échec : %s", exc)
            return fragment(error="Le moteur de recherche ne répond pas. Réessayez dans un instant.")
        ms = int((time.perf_counter() - t0) * 1000)
        store.log_question(groups, question, len(answer.hits), len(answer.citations), ms)
        return fragment(answer=answer, r=answer.criteria, ms=ms)

    return router


def login_redirect(path: str) -> RedirectResponse:
    return RedirectResponse(f"/connexion?suite={quote(path)}", status_code=303)
