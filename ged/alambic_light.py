"""Client d'Alambic Light : OCR, type du document et données extraites.

Alambic Search reste maître des sources, des droits et de l'index ; Alambic Light lit le
document (OCR si besoin), le type dans son référentiel et en extrait les données, avec ses
propres contrôles et sa validation par un gestionnaire.

Contrat utilisé (alambic_light.api) :
  POST /v1/token                    jeton OAuth2 client_credentials (sinon en-tête X-Api-Key)
  POST /v1/documents/typage?ia=     multipart « fichier » + « source », « identifiant »
       → 200 résultat, ou 202 + Location tant que le traitement n'est pas fini
  GET  /v1/documents/{uuid}         résultat (statut EN_COURS, RECONNU, NON_RECONNU, ERREUR, VALIDE)
  GET  /v1/documents/{uuid}/texte   texte OCR page par page (s'il est exposé)
  GET  /v1/referentiel              libellés des types (droit « lecture », facultatif)
Formats acceptés : PDF, PNG, JPEG, TIFF, WEBP.

Le fichier n'est pas conservé par Alambic Search : il est lu dans la source, envoyé, oublié.
Le pointeur (source + identifiant) permet à Alambic Light de le désigner sans le garder.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from pathlib import PurePosixPath

import httpx

from ged.models import DocMeta, Page

log = logging.getLogger(__name__)

EXTENSIONS = frozenset({".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp"})
IMAGE_EXTENSIONS = EXTENSIONS - {".pdf"}
EN_COURS = "EN_COURS"
ERREUR = "ERREUR"


class LightUnavailable(Exception):
    """Alambic Light injoignable ou en erreur : le document sera retenté à la prochaine synchro."""


class LightRejected(Exception):
    """Alambic Light refuse le fichier (format, taille) : inutile de le renvoyer tel quel."""


@dataclass(frozen=True)
class Analysis:
    meta: DocMeta
    pages: list[Page] | None  # texte OCR par page, s'il a été transmis
    error: str | None = None  # statut ERREUR : raison donnée par Alambic Light

    @property
    def failed(self) -> bool:
        return self.meta.light_status == ERREUR


class AlambicLight:
    def __init__(
        self,
        url: str,
        *,
        api_key: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        ia: bool = True,
        send_all: bool = True,
        concurrency: int = 2,
        max_wait: float = 300.0,
        timeout: float = 120.0,
        transport: httpx.BaseTransport | None = None,
        validation_link: str | None = None,
        validation_roles: tuple[str, ...] = ("alambic-light:validation", "alambic-light:admin"),
    ):
        if not url:
            raise ValueError("alambic_light : url vide")
        if not api_key and not (client_id and client_secret):
            raise ValueError("alambic_light : api_key_env, ou client_id + client_secret_env, est requis")
        self.url = url.rstrip("/")
        # Écran de validation d'un document, tel que l'ouvre le NAVIGATEUR (l'url ci-dessus peut être
        # une adresse interne, ex. http://alambic-light:8000 dans Docker). {uuid} : résultat.
        self.validation_link = validation_link if validation_link is not None else f"{self.url}/validation?document={{uuid}}"
        self.validation_roles = tuple(validation_roles)
        self.ia = ia
        self.send_all = send_all  # False : seulement les documents sans texte (scans)
        self.concurrency = max(1, int(concurrency))
        self.max_wait = max_wait
        self._api_key = api_key
        self._client_id = client_id
        self._client_secret = client_secret
        self._token: tuple[str, float] | None = None
        self._lock = threading.Lock()
        self._labels: dict[str, str] | None = None
        self._text_endpoint = True  # passe à False si l'instance n'expose pas /texte
        self._http = httpx.Client(
            base_url=self.url, timeout=httpx.Timeout(timeout, connect=10.0), transport=transport,
            headers={"User-Agent": "alambic-search"},
        )

    def validation_url(self, uuid: str) -> str | None:
        if not self.validation_link or not uuid:
            return None
        from urllib.parse import quote

        return self.validation_link.replace("{uuid}", quote(uuid, safe=""))

    def describe(self) -> dict:
        return {"url": self.url, "ia": self.ia, "envoi": "tous" if self.send_all else "scans",
                "concurrence": self.concurrency}

    # --- Vérification de la connexion ---------------------------------------------------

    def check(self) -> dict:
        """Teste la connexion sans rien envoyer : service joignable, identifiants, droits, texte OCR.

        Renvoie {"ok": bool, "etapes": [(libellé, ok|None, détail)]} ; None = avertissement.
        """
        steps: list[tuple[str, bool | None, str]] = []

        def done() -> dict:
            return {"ok": all(s[1] is not False for s in steps), "etapes": steps}

        try:
            r = self._http.get("/health")
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            steps.append(("Service joignable", r.status_code == 200,
                          f"version {body.get('version', '?')}, état {body.get('status', r.status_code)}"))
        except httpx.HTTPError as exc:
            steps.append(("Service joignable", False, f"{self.url} : {exc}"))
            return done()

        try:
            r = self._request("GET", "/v1/moi")
        except LightUnavailable as exc:
            steps.append(("Identifiants", False, str(exc)))
            return done()
        if r.status_code == 200:
            me = r.json()
            scopes = set(me.get("scopes") or [])
            steps.append(("Identifiants", True, f"compte « {me.get('client')} »"))
            steps.append(("Droit « typage »", "typage" in scopes,
                          "accordé" if "typage" in scopes else "absent : les documents seraient refusés"))
            steps.append(("Droit « lecture »", True if "lecture" in scopes else None,
                          "accordé (libellés des types)" if "lecture" in scopes
                          else "absent : les codes (FACTURE…) tiendront lieu de libellés"))
        else:
            steps.append(("Identifiants", None, f"/v1/moi indisponible ({r.status_code}) : droits non vérifiés"))

        r = self._request("GET", "/v1/documents/00000000-0000-0000-0000-000000000000/texte")
        exposed = not (r.status_code == 404 and _detail(r) == "Not Found")
        steps.append(("Texte OCR", True if exposed else None,
                      "GET /v1/documents/{uuid}/texte disponible" if exposed
                      else "non exposé : les scans ne seront interrogeables que par leur fiche"))
        return done()

    # --- Ce qui est envoyé -------------------------------------------------------------

    def accepts(self, filename: str) -> bool:
        return PurePosixPath(filename).suffix.lower() in EXTENSIONS

    def wanted(self, filename: str, has_text: bool) -> bool:
        return self.accepts(filename) and (self.send_all or not has_text)

    # --- Appels ------------------------------------------------------------------------

    def analyze(self, data: bytes, filename: str, *, source: str, identifiant: str) -> Analysis:
        """Envoie le fichier, attend le résultat (202 → interrogations espacées), récupère le texte."""
        files = {"fichier": (PurePosixPath(filename).name, data)}
        form = {"source": source[:200], "identifiant": identifiant[:500]}
        r = self._request("POST", "/v1/documents/typage", params={"ia": str(self.ia).lower()},
                          files=files, data=form)
        if r.status_code in (413, 415):
            raise LightRejected(_detail(r))
        rec = self._json(r)
        if r.status_code == 202 or rec.get("statut") == EN_COURS:
            rec = self._wait(rec["uuid"], r.headers.get("Location"))
        return self._analysis(rec, with_text=True)

    def ocr(self, data: bytes, filename: str, *, source: str, identifiant: str,
            ia_ocr: bool = False) -> tuple[list[Page] | None, str | None]:
        """OCR seul (sources « texte seul ») : même appel que le typage, avec ocr_only=true.
        `ia_ocr` : lecture par l'IA (ocr=ia), sinon le moteur par défaut d'Alambic Light.
        Seul le texte est gardé : un type ou des données éventuellement renvoyés sont ignorés.
        Renvoie (pages, erreur)."""
        params = {"ia": str(self.ia).lower(), "ocr_only": "true"}
        if ia_ocr:
            params["ocr"] = "ia"
        files = {"fichier": (PurePosixPath(filename).name, data)}
        form = {"source": source[:200], "identifiant": identifiant[:500]}
        r = self._request("POST", "/v1/documents/typage", params=params, files=files, data=form)
        if r.status_code in (413, 415):
            raise LightRejected(_detail(r))
        rec = self._json(r)
        if r.status_code == 202 or rec.get("statut") == EN_COURS:
            rec = self._wait(rec["uuid"], r.headers.get("Location"))
        if rec.get("statut") == ERREUR:
            return None, rec.get("erreur") or "lecture impossible"
        return _pages_from(rec) or self._text(rec.get("uuid")), None

    def result(self, uuid: str) -> Analysis | None:
        """Résultat actuel (ex. après validation par un gestionnaire). None : expiré côté Alambic Light."""
        r = self._request("GET", f"/v1/documents/{uuid}")
        if r.status_code == 404:
            return None
        return self._analysis(self._json(r), with_text=False)

    def _wait(self, uuid: str, location: str | None) -> dict:
        path = location or f"/v1/documents/{uuid}"
        deadline = time.monotonic() + self.max_wait
        pause = 1.0
        while True:
            time.sleep(pause)
            rec = self._json(self._request("GET", path))
            if rec.get("statut") != EN_COURS:
                return rec
            if time.monotonic() > deadline:
                raise LightUnavailable(f"Alambic Light : document {uuid} toujours en cours après {self.max_wait:.0f} s")
            pause = min(pause * 1.5, 5.0)

    def _analysis(self, rec: dict, *, with_text: bool) -> Analysis:
        code = rec.get("typeDocument")
        statut = rec.get("statut")
        # donnees : {champ: ChampExtrait {valeur, confiance, source, aVerifier, brut}} ;
        # après validation (statut VALIDE), ce sont les valeurs validées.
        fields = rec.get("donnees") or {}
        fields = fields if isinstance(fields, dict) else {}
        to_check = () if statut == "VALIDE" else _to_check(rec, fields)
        meta = DocMeta(
            doc_type=code if code and code != "INCONNU" else None,
            label=self.label(code) if code and code != "INCONNU" else None,
            fields=fields if isinstance(fields, dict) else {},
            to_check=to_check,
            light_uuid=rec.get("uuid"),
            light_status=statut,
            confidence=rec.get("confiance"),
        )
        pages = None
        if with_text and statut != ERREUR:
            pages = _pages_from(rec) or self._text(rec.get("uuid"))
        return Analysis(meta, pages, rec.get("erreur") if statut == ERREUR else None)

    def _text(self, uuid: str | None) -> list[Page] | None:
        if not uuid or not self._text_endpoint:
            return None
        r = self._request("GET", f"/v1/documents/{uuid}/texte")
        if r.status_code == 404:
            if _detail(r) == "Not Found":  # route absente (≠ « uuid inconnu ou expiré »)
                log.warning("Alambic Light n'expose pas /v1/documents/{uuid}/texte : "
                            "les scans ne seront interrogeables que par leur fiche (type et données)")
                self._text_endpoint = False
            return None
        return _pages_from(self._json(r))

    def label(self, code: str | None) -> str | None:
        if not code:
            return None
        if self._labels is None:
            try:
                r = self._request("GET", "/v1/referentiel")
                self._labels = {t["code"]: t["libelle"] for t in self._json(r)} if r.status_code == 200 else {}
            except (LightUnavailable, KeyError, TypeError):
                self._labels = {}  # compte sans le droit « lecture » : le code tient lieu de libellé
        return self._labels.get(code) or code.replace("_", " ").capitalize()

    # --- Transport ---------------------------------------------------------------------

    def _auth(self, renew: bool = False) -> dict[str, str]:
        if self._api_key:
            return {"X-Api-Key": self._api_key}
        with self._lock:
            if renew or self._token is None or self._token[1] < time.monotonic():
                try:
                    r = self._http.post("/v1/token", data={
                        "grant_type": "client_credentials",
                        "client_id": self._client_id, "client_secret": self._client_secret,
                    })
                except httpx.HTTPError as exc:
                    raise LightUnavailable(f"Alambic Light injoignable ({self.url}) : {exc}") from exc
                if r.status_code != 200:
                    raise LightUnavailable(f"Alambic Light refuse les identifiants ({r.status_code} : {_detail(r)})")
                body = r.json()
                self._token = (body["access_token"], time.monotonic() + max(30, int(body.get("expires_in", 300)) - 30))
            return {"Authorization": f"Bearer {self._token[0]}"}

    def _request(self, method: str, path: str, **kw) -> httpx.Response:
        renewed = False
        for attempt in range(6):
            try:
                r = self._http.request(method, path, headers=self._auth(), **kw)
            except httpx.HTTPError as exc:
                raise LightUnavailable(f"Alambic Light injoignable ({self.url}) : {exc}") from exc
            if r.status_code == 401 and not self._api_key and not renewed:
                self._auth(renew=True)  # jeton expiré entre-temps
                renewed = True
                continue
            if r.status_code == 429 or r.status_code == 503:
                wait = min(float(r.headers.get("Retry-After") or 2 ** attempt), 30.0)
                log.info("Alambic Light demande d'attendre %.0f s (%s)", wait, r.status_code)
                time.sleep(wait)
                continue
            if r.status_code in (401, 403):
                raise LightUnavailable(f"Alambic Light refuse l'accès ({r.status_code} : {_detail(r)}) : "
                                       "vérifier la clé et le droit « typage »")
            if r.status_code >= 500:
                raise LightUnavailable(f"Alambic Light en erreur ({r.status_code})")
            return r
        raise LightUnavailable("Alambic Light surchargé : nouvel essai à la prochaine synchronisation")

    @staticmethod
    def _json(r: httpx.Response) -> dict:
        if r.status_code >= 400:
            raise LightUnavailable(f"Alambic Light : {r.status_code} {_detail(r)}")
        try:
            return r.json()
        except ValueError as exc:
            raise LightUnavailable(f"Alambic Light : réponse illisible ({r.status_code})") from exc

    def close(self) -> None:
        self._http.close()


def _detail(r: httpx.Response) -> str:
    try:
        body = r.json()
    except ValueError:
        return r.text[:200]
    if isinstance(body, dict):
        return str(body.get("detail") or body.get("error_description") or body)[:300]
    return str(body)[:300]


def _to_check(rec: dict, fields: dict) -> tuple[str, ...]:
    """Champs douteux : donneesAVerifier, champs marqués aVerifier, et verification.aVerifier
    (une cellule de tableau « lignes.2.montant » signale le champ « lignes »)."""
    names = list(_names(rec.get("donneesAVerifier")))
    names += [k for k, v in fields.items() if isinstance(v, dict) and v.get("aVerifier")]
    names += [n.split(".", 1)[0] for n in _names((rec.get("verification") or {}).get("aVerifier"))]
    return tuple(dict.fromkeys(names))  # ordre conservé, sans doublon


def _names(value) -> tuple[str, ...]:
    """donneesAVerifier : liste de noms de champs, ou objets {champ: …}, ou dictionnaire."""
    if not value:
        return ()
    if isinstance(value, dict):
        return tuple(str(k) for k in value)
    out = []
    for v in value:
        if isinstance(v, str):
            out.append(v)
        elif isinstance(v, dict) and (v.get("champ") or v.get("nom")):
            out.append(str(v.get("champ") or v.get("nom")))
    return tuple(out)


def _pages_from(rec: dict) -> list[Page] | None:
    """Texte OCR s'il est dans la réponse (`pages` ou `texte`). Le typage n'en renvoie pas
    (`ocr` y vaut natif / tesseract / mistral) : il vient de GET /v1/documents/{uuid}/texte."""
    ocr = rec.get("ocr") if isinstance(rec.get("ocr"), dict) else {}
    for pages in (rec.get("pages"), ocr.get("pages")):
        if isinstance(pages, list) and pages and isinstance(pages[0], dict):
            out = [
                Page(int(p.get("numero") or p.get("page") or i), str(p.get("texte") or p.get("text") or ""))
                for i, p in enumerate(pages, 1)
            ]
            if any(p.text.strip() for p in out):
                return out
    for texte in (rec.get("texte"), ocr.get("texte")):
        if isinstance(texte, str) and texte.strip():
            # pages séparées par un saut de page (\f), comme Tesseract
            return [Page(i, t) for i, t in enumerate(texte.split("\f"), 1) if t.strip()] or None
    return None


# --- Fiche : type et données, sous forme d'extrait interrogeable -----------------------------

FICHE_CHUNK_NO = -1  # avant le premier extrait du texte


def fiche_text(meta: DocMeta, max_chars: int = 2000) -> str | None:
    """« Fiche » du document : rend le type et les données trouvables par la recherche et
    utilisables par le LLM. Les valeurs douteuses sont marquées « à vérifier »."""
    if not meta.doc_type and not meta.fields:
        return None
    lines = [f"Fiche Alambic : {meta.label or meta.doc_type or 'type non reconnu'}"
             + (" (données validées)" if meta.validated else "")]
    for name, value in meta.fields.items():
        rendered = _render(value)
        if rendered is None:
            continue
        flag = " (à vérifier)" if name in meta.to_check else ""
        lines.append(f"{_human(name)} : {rendered}{flag}")
    text = "\n".join(lines)
    return text if len(text) <= max_chars else text[: max_chars - 1] + "…"


def _human(name: str) -> str:
    """montantTTC → Montant TTC, date_echeance → Date echeance."""
    words = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name.replace("_", " ")).split()
    return " ".join([words[0].capitalize(), *words[1:]]) if words else name


def _render(value) -> str | None:
    if value is None or value == "" or value == [] or value == {}:
        return None
    if isinstance(value, dict):
        if "valeur" in value:  # ChampExtrait {valeur, confiance, source, aVerifier, brut}
            rendered = _render(value["valeur"])
            if rendered is None and value.get("brut"):
                return f"{value['brut']} (format invalide)"  # lu mais rejeté (clé d'IBAN fausse…)
            return rendered
        parts = [f"{_human(k)} {r}" for k, v in value.items() if (r := _render(v)) is not None]
        return ", ".join(parts) or None
    if isinstance(value, list):
        items = [r for v in value if (r := _render(v)) is not None]
        if not items:
            return None
        return f"{len(items)} élément{'s' if len(items) > 1 else ''} : " + " ; ".join(items)
    if isinstance(value, bool):
        return "oui" if value else "non"
    return str(value)
