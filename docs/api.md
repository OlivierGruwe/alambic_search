# Alambic Search — Documentation de l'API

Version 0.9.0 bêta 1.

API REST d'Alambic Search : questions, recherche, listes et accès aux documents, toujours dans la limite des droits du compte appelant.

- [Authentification](#authentification)
- [Vue d'ensemble](#vue-densemble)
- [POST /ask — poser une question](#post-ask--poser-une-question)
- [GET /search — extraits classés](#get-search--extraits-classés)
- [GET /recherche — liste exhaustive par critères](#get-recherche--liste-exhaustive-par-critères)
- [Documents et vignettes](#documents-et-vignettes)
- [Administration](#administration)
- [Exemples complets](#exemples-complets)

## Authentification

Chaque appel s'authentifie avec un compte Alambic Search ; la réponse ne contient que les documents que ce compte a le droit de lire. Les droits viennent du compte (compte Windows rattaché et groupes saisis par un administrateur), jamais de l'appelant.

| Méthode | En-tête | Usage |
| --- | --- | --- |
| Clé d'API (recommandé) | `Authorization: Bearer als_…` ou `X-API-Key: als_…` | Applications, scripts, Postman |
| Identifiant et mot de passe | `Authorization: Basic …` (HTTP Basic) | Essais ponctuels ; 5 échecs en 15 min bloquent 15 min |
| Session | cookie `alambic_session`, posé par `/connexion` | Navigateur (liens vers les documents cités) |
| En-têtes de confiance | `X-User` / `X-User-Groups` | Seulement avec `TRUST_IDENTITY_HEADERS=true`, derrière un proxy qui les pose lui-même ; désactivé par défaut |

Les identifiants explicites (clé, Basic) l'emportent sur le cookie. Une clé se crée dans **Mon compte → Clés d'API** (ou par un administrateur, page Utilisateurs) ; elle n'est affichée qu'une fois, a une échéance (30 j, 90 j, 1 an ou aucune) et se révoque à tout moment. Elle agit avec le rôle et les droits de son propriétaire ; un compte désactivé rend ses clés inopérantes aussitôt. Un compte sans mot de passe sert de compte d'application (clés seulement).

**Rôles.** Utilisateur : toutes les routes de recherche et de documents. Gestionnaire et administrateur : en plus, `POST /sources/{nom}/sync`.

**Erreurs communes.**

| Code | Cas | Corps |
| --- | --- | --- |
| 401 | Aucune authentification, clé invalide, expirée ou révoquée, mot de passe faux | `{"detail": "Clé d'API invalide, expirée ou révoquée"}` + en-tête `WWW-Authenticate` |
| 403 | Rôle insuffisant ; document que le compte ne peut pas lire (`/documents`) | `{"detail": "Rôle « Gestionnaire » requis"}` |
| 404 | Source ou document inconnu, ou non autorisé (vignettes) | `{"detail": "Document introuvable"}` |
| 422 | Paramètre manquant ou invalide | détail de validation FastAPI |
| 429 | Trop d'échecs d'identifiant (HTTP Basic) | `{"detail": "Trop d'essais infructueux…"}` |
| 503 | Annuaire injoignable : droits d'un compte rattaché à l'AD non vérifiables | `{"detail": "Annuaire injoignable : droits non vérifiables"}` |

## Vue d'ensemble

Adresse de base : `https://<hôte>` (Swarm, via Caddy) ou `http://localhost:8002` (poste de développement). Corps et réponses en JSON UTF-8, sauf les fichiers et les vignettes. Schéma OpenAPI complet : `/openapi.json` ; documentation interactive : `/docs`.

| Méthode | Route | Rôle requis | Objet |
| --- | --- | --- | --- |
| `POST` | `/ask` | utilisateur | Question en langage courant : réponse rédigée et citée, ou extraits, ou liste |
| `GET` | `/search` | utilisateur | Extraits classés (ou un résultat par document), sans LLM |
| `GET` | `/recherche` | utilisateur | Liste exhaustive par critères (données extraites par Alambic Light) |
| `GET` | `/documents/{source}/{doc_id}` | utilisateur | Le fichier d'origine, droits revérifiés en direct |
| `GET` | `/vignettes/{source}/{doc_id}` | utilisateur | Aperçu WebP d'une page |
| `POST` | `/sources/{nom}/sync` | gestionnaire | Synchronisation bloquante d'une source |
| `GET` | `/health` | aucun | État, version, sources chargées |

Les liens renvoyés (`url`) sont relatifs à l'adresse de base et pointent vers `/documents/…#page=N` : ils s'ouvrent dans un navigateur connecté, ou s'appellent avec la même clé d'API. Un `doc_id` peut contenir des `/` ou des `#` : l'utiliser tel qu'il est renvoyé dans `url`, déjà encodé.

## POST /ask — poser une question

Répond à une question en langage courant à partir des seuls documents que le compte peut lire, en citant ses sources à la page près. Les droits sont appliqués avant la recherche : aucun extrait interdit n'atteint le LLM.

**Corps de la requête**

| Champ | Type | Défaut | Rôle |
| --- | --- | --- | --- |
| `question` | texte, 3 à 2 000 caractères | — | La question |
| `k` | entier, 1 à 30 | 8 | Nombre d'extraits (ou de documents avec `par_document`) |
| `formuler` | booléen | `true` | `false` : extraits pertinents tels quels, sans rédaction (plus rapide) ; une question de liste reste traduite en critères |
| `par_document` | booléen | `false` | `true` : un résultat par document, pages trouvées regroupées ; les citations `[n]` désignent alors des documents |
| `types` | liste de codes | `[]` | Limite aux types de documents reconnus par Alambic Light (ex. `["FACTURE"]`) |
| `historique` | liste de `{question, reponse}`, 10 au plus | `[]` | Échanges précédents : une question de suivi (« et sa durée ? ») est d'abord rendue autonome par le LLM |

**Réponse**

| Champ | Contenu |
| --- | --- |
| `text` | Réponse rédigée avec ses renvois `[n]` ; sans LLM, une phrase d'introduction |
| `formulee` | `true` si rédigée par le LLM |
| `question_comprise` | Question de suivi rendue autonome, sinon `null` |
| `identifiants` | Identifiants détectés dans la question (n° de facture, de permis…) |
| `identifiants_introuvables` | Ceux qu'aucun document accessible ne contient |
| `citations` | `[{n, source, doc_id, path, page, url}]` : sources citées |
| `extraits` | Sans LLM et sans `par_document` : `[{n, path, page, type, text}]` |
| `documents` | Avec `par_document` : `[{n, path, type, url, extrait, score, pages:[{page, url}]}]`, sinon `null` |
| `liste` | Question de liste traduite en critères (« toutes les factures de GRUWE ») : même contenu que `/recherche`, sinon `null` |

**Comportements à connaître**

- **Identifiant dans la question** (au moins 5 caractères avec un chiffre et une lettre ou un tiret, ex. `F-2026-0042`) : seuls les documents qui le contiennent tel quel sont retenus ; s'il n'est nulle part, la réponse le dit et revient à la recherche par ressemblance.
- **Question de liste** (« toutes les … », avec ou sans `formuler`) : le LLM la traduit en critères contrôlés (type et champ existants, valeur présente dans la question), puis la liste vient d'une requête SQL exhaustive, pas du LLM.
- **Pas de réponse dans les documents** : le LLM le dit au lieu d'inventer ; aucune source n'est alors citée.
- **LLM en ligne** (Mistral AI, si configuré) : seuls la question et les extraits autorisés lui sont envoyés ; avec `formuler: false`, seule une question de liste (« toutes les … ») lui est envoyée, pour sa traduction en critères.

**Exemple**

```bash
curl -s https://search.client.fr/ask \
  -H "Authorization: Bearer $ALAMBIC_KEY" -H 'Content-Type: application/json' \
  -d '{"question": "Quelle est la pénalité de retard du contrat Durand ?"}'
```

```json
{
  "text": "La pénalité de retard est de 2 % par semaine [1].",
  "formulee": true,
  "duree_ms": 1840,
  "duree_llm_ms": 1620,
  "etapes_ms": {"vecteur": 45, "recherche": 30, "reclassement": 120},
  "question_comprise": null,
  "identifiants": [],
  "identifiants_introuvables": [],
  "citations": [
    {"n": 1, "source": "partage", "doc_id": "65024:1220631",
     "path": "Compta/contrat-fournisseur.docx", "page": 1,
     "url": "/documents/partage/65024:1220631#page=1"}
  ],
  "extraits": [],
  "documents": null,
  "liste": null
}
```

`duree_ms` : temps total de la réponse ; `duree_llm_ms` : dont attente du LLM (rédaction, traduction d'une question de liste, question de suivi), 0 sans LLM ; `etapes_ms` : durée de chaque étape de la recherche (vecteur de la question, recherche hybride, reclassement).

Question de suivi : renvoyer l'échange précédent.

```json
{"question": "et sa durée ?",
 "historique": [{"question": "Quelle est la pénalité de retard du contrat Durand ?",
                 "reponse": "La pénalité de retard est de 2 % par semaine [1]."}]}
```

## GET /search — extraits classés

Recherche hybride (plein texte français + vecteurs, reclassement si configuré), sans LLM : la route la plus rapide pour alimenter un moteur ou une liste de résultats.

| Paramètre | Type | Défaut | Rôle |
| --- | --- | --- | --- |
| `q` | texte | — | Les mots ou la question |
| `k` | entier, 1 à 30 | 8 | Nombre de résultats |
| `types` | codes séparés par des virgules | — | Ex. `FACTURE,CONTRAT` |
| `par_document` | booléen | `false` | Un résultat par document, avec ses pages trouvées |

**Réponse** (liste, du plus pertinent au moins pertinent) : `[{path, page, type, score, excerpt, url}]` ; `excerpt` est tronqué à 300 caractères, `type` est le libellé Alambic Light (ou `null`). Avec `par_document=true` : `[{n, path, type, url, extrait, score, pages:[{page, url}]}]`.

```bash
curl -s 'https://search.client.fr/search?q=p%C3%A9nalit%C3%A9+de+retard&k=2' -H "X-API-Key: $ALAMBIC_KEY"
```

```json
[
  {"path": "Compta/contrat-fournisseur.docx", "page": 1, "type": null, "score": 0.03279,
   "excerpt": "Contrat de maintenance Durand\n\nPénalité de retard : 2 % par semaine de retard.",
   "url": "/documents/partage/65024:1220631#page=1"},
  {"path": "Compta/Factures/facture-acme.pdf", "page": 2, "type": null, "score": 0.01613,
   "excerpt": "Conditions de paiement : 30 jours",
   "url": "/documents/partage/65024:1220629#page=2"}
]
```

Le `score` sert à ordonner, pas à comparer d'une requête à l'autre : son échelle dépend de la fusion (RRF) ou du reclassement.

## GET /recherche — liste exhaustive par critères

Donne **tous** les documents dont une donnée extraite par Alambic Light contient la valeur (mot entier, sans tenir compte des majuscules ni des accents), puis, à part, ceux qui ne la citent que dans leur texte. C'est la route des listes (« toutes les factures de M. GRUWE ») : la recherche par ressemblance n'en garde que quelques-uns.

| Paramètre | Type | Défaut | Rôle |
| --- | --- | --- | --- |
| `valeur` | texte, 2 caractères au moins | — | Nom, société, numéro… |
| `type` | code | — | Type de document (ex. `FACTURE`) |
| `champ` | nom de champ | — | Limite à un champ extrait (ex. `fournisseur`) |
| `mentions` | booléen | `true` | `false` : sans la liste des documents qui citent seulement la valeur |

**Réponse**

| Champ | Contenu |
| --- | --- |
| `filtre` | `{type, champ, valeur}` appliqués |
| `complet` | `false` si la liste dépasse 500 documents (tronquée) |
| `documents` | `[{path, type, libelle, url, champs:[{champ, valeur, aVerifier}]}]` : données qui correspondent ; `aVerifier` = donnée douteuse, à valider dans Alambic Light |
| `mentions` | `[{path, type, libelle, page, extrait, url}]` : documents dont seul le texte cite la valeur |

```bash
curl -s 'https://search.client.fr/recherche?type=FACTURE&valeur=GRUWE' -H "X-API-Key: $ALAMBIC_KEY"
```

```json
{
  "filtre": {"type": null, "champ": null, "valeur": "Durand"},
  "complet": true,
  "documents": [],
  "mentions": [
    {"path": "Compta/contrat-fournisseur.docx", "type": null, "libelle": null, "page": 1,
     "extrait": "Contrat de maintenance Durand  Pénalité de retard : 2 % par semaine de retard.",
     "url": "/documents/partage/65024:1220631#page=1"}
  ]
}
```

Sans Alambic Light (ou sur une source « texte seul »), `documents` reste vide : seules les mentions dans le texte sont trouvées. 422 si `valeur` fait moins de 2 caractères.

## Documents et vignettes

### GET /documents/{source}/{doc_id} — ouvrir le document cité

Renvoie le fichier d'origine, lu dans la source au moment de l'appel. Les droits sont **revérifiés en direct dans la source** (pas seulement dans l'index) : un droit retiré depuis la dernière synchronisation est respecté aussitôt.

| Paramètre | Rôle |
| --- | --- |
| `pdf=true` | Fichier bureautique (Word, Excel, PowerPoint…) converti à la volée en PDF, pour ouvrir la page citée ; les liens `url` de `/ask` et `/search` l'ajoutent déjà quand il le faut |
| `#page=N` | Fragment lu par le lecteur PDF du navigateur (non envoyé au serveur) |

| Code | Cas |
| --- | --- |
| 200 | Le fichier (type MIME d'origine, ou `application/pdf` avec `pdf=true`) |
| 303 | Navigateur sans session (`Accept: text/html`) : redirection vers `/connexion`, puis retour au document |
| 403 | Le compte n'a pas (ou plus) le droit de lire ce fichier |
| 404 | Source inconnue, document absent de l'index ou de la source |
| 502 | Conversion PDF en échec |

### GET /vignettes/{source}/{doc_id} — aperçu d'une page

Image WebP d'une page, rendue à la volée (PDF, images ; bureautique si la conversion PDF est configurée), gardée en mémoire, jamais écrite sur disque. Droits : ceux de l'index, comme pour l'extrait affiché.

| Paramètre | Défaut | Rôle |
| --- | --- | --- |
| `page` | 1 | Numéro de page |
| `l` | 160 | Largeur en pixels, ramenée à 160, 320 ou 480 |

Réponses : `image/webp` (cache privé 1 h, `ETag`) ; une image de remplacement `image/svg+xml` quand le format n'a pas d'aperçu ou que le rendu échoue ; 404 si le document est inconnu ou non autorisé (sans dire lequel des deux).

```bash
curl -s -o page1.webp 'https://search.client.fr/vignettes/partage/65024:1220629?page=1&l=320' -H "X-API-Key: $ALAMBIC_KEY"
```

## Administration

### POST /sources/{nom}/sync — synchroniser une source

Synchronisation **bloquante** (la réponse arrive quand elle est finie) : pour un script ou un ordonnanceur externe. La console et le service `worker` font la même chose en arrière-plan. Rôle gestionnaire ou administrateur.

```bash
curl -s -X POST https://search.client.fr/sources/partage/sync -H "X-API-Key: $CLE_GESTIONNAIRE"
```

```json
{"added": 0, "updated": 0, "moved": 0, "acl_changed": 0, "unchanged": 4, "deleted": 0,
 "ocr_required": 0, "converted": 0, "analysed": 0, "validated": 0, "errors": []}
```

| Champ | Sens |
| --- | --- |
| `added` / `updated` / `deleted` | Documents ajoutés, réindexés (contenu modifié), retirés de l'index |
| `moved` / `acl_changed` | Renommés ou déplacés / droits modifiés, sans réindexation |
| `unchanged` | Inchangés |
| `ocr_required` | Scans sans texte (Alambic Light non configuré) |
| `converted` / `analysed` / `validated` | Convertis en PDF / envoyés à Alambic Light / validations reprises |
| `errors` | `["chemin: motif"]` ; ces fichiers sont retentés au passage suivant |

| Code | Cas |
| --- | --- |
| 403 | Compte utilisateur : `{"detail": "Rôle « Gestionnaire » requis"}` |
| 404 | Source inconnue |
| 409 | Synchronisation déjà en cours (sur cette instance ou une autre), ou garde-fou : plus de 50 % des documents ont disparu d'un coup (partage démonté ?) ; rien n'est supprimé |

Une synchronisation longue peut dépasser le délai d'un proxy : prévoir un délai client généreux (`curl --max-time 3600`) ou passer par la planification de la console.

### GET /health — état du service

Sans authentification ; répond même quand l'instance est occupée. Utilisé par le contrôle de santé de Swarm.

```json
{"status": "ok", "version": "0.9.0b1", "sources": ["partage"], "llm": true}
```

### OpenAPI

`/openapi.json` décrit toutes les routes et leurs schémas (importable dans Postman ou un générateur de client) ; `/docs` permet de les essayer depuis le navigateur.

## Exemples complets

### Python

Une question, sa réponse et ses sources, puis le téléchargement du premier document cité.

```python
import os
import httpx

BASE = "https://search.client.fr"
api = httpx.Client(base_url=BASE, headers={"Authorization": f"Bearer {os.environ['ALAMBIC_KEY']}"}, timeout=120)

r = api.post("/ask", json={"question": "Quelle est la pénalité de retard du contrat Durand ?"})
r.raise_for_status()
answer = r.json()
print(answer["text"])
for c in answer["citations"]:
    print(f"[{c['n']}] {c['path']}, page {c['page']}")

if answer["citations"]:
    url = answer["citations"][0]["url"].split("#")[0]  # le fragment #page=N reste côté navigateur
    doc = api.get(url)
    doc.raise_for_status()
    open(answer["citations"][0]["path"].rsplit("/", 1)[-1], "wb").write(doc.content)
```

Question de suivi : garder les échanges et les renvoyer (3 derniers suffisent).

```python
historique = [{"question": "Quelle est la pénalité de retard du contrat Durand ?", "reponse": answer["text"]}]
suite = api.post("/ask", json={"question": "et sa durée ?", "historique": historique}).json()
print(suite["question_comprise"], "→", suite["text"])
```

### JavaScript (Node 18+)

```javascript
const r = await fetch("https://search.client.fr/search?q=" + encodeURIComponent("facture ACME") + "&par_document=true", {
  headers: { "X-API-Key": process.env.ALAMBIC_KEY },
});
if (!r.ok) throw new Error(`${r.status} ${(await r.json()).detail}`);
for (const d of await r.json()) console.log(d.n, d.path, d.pages.map(p => p.page));
```

Depuis une page web d'un autre domaine, l'appel direct est bloqué par le navigateur (CORS non ouvert) : passer par le serveur de l'application, qui garde la clé d'API secrète.

### Postman

1. Importer [`postman/alambic-search.postman_collection.json`](postman/alambic-search.postman_collection.json).
2. Dans les variables de la collection : `baseUrl` (ex. `https://search.client.fr`) et `apiKey` (clé créée dans **Mon compte → Clés d'API**).
3. La collection s'authentifie par la clé (`Authorization: Bearer {{apiKey}}`) ; pour l'essai par identifiant et mot de passe, choisir « Basic Auth » dans l'onglet Authorization et remplir `login` / `password`.

### Bonnes pratiques

- Une clé par application, nommée d'après elle, avec une échéance ; la révoquer dès qu'elle ne sert plus ou qu'elle a pu fuir.
- Pour une application qui interroge pour le compte de ses propres utilisateurs, un compte d'application dédié (sans mot de passe) dont les groupes reflètent ce que l'application a le droit de montrer.
- Ne jamais stocker la clé dans un code source ou une page web : variable d'environnement ou coffre de secrets.
- `formuler: false` ou `/search` quand une réponse rédigée n'est pas nécessaire : plus rapide, sans rédaction (seules les questions de liste passent par le LLM, pour leur traduction en critères).
