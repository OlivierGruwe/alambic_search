# Alambic Search — POC

Couche d'interrogation par-dessus un partage réseau existant : indexation du contenu,
recherche hybride (plein texte + vecteurs), réponses avec citations au niveau de la page,
droits d'accès appliqués **avant** que le moindre extrait n'atteigne le LLM.

Les fichiers restent dans le partage. L'index stocke le texte découpé, les vecteurs,
les métadonnées, les droits et un pointeur vers l'original.

## Démarrage rapide

Prérequis : Docker, [uv](https://docs.astral.sh/uv/) et `make`. `make` seul liste toutes les commandes.

```bash
cp sources.example.yaml sources.yaml          # adapter les règles de droits
export ADMIN_PASSWORD='un-mot-de-passe-solide'
make up                                       # base, Ollama, API
make pull-models                              # bge-m3 et mistral-small

curl -X POST -u admin:$ADMIN_PASSWORD localhost:8002/sources/partage-commun/sync
curl -X POST localhost:8002/ask -H 'X-User-Groups: compta' \
     -H 'Content-Type: application/json' \
     -d '{"question": "Quelle est la pénalité de retard du contrat de maintenance ?"}'
```

`demo-data/` contient trois fichiers d'exemple ; pour un vrai test, monter le partage
SMB/NFS en lecture seule à la place (voir `docker-compose.yml`).

## Console d'administration

Ouvrir `http://localhost:8002/admin` (utilisateur `admin`, mot de passe `ADMIN_PASSWORD`).
Sans mot de passe défini, la console est désactivée.

- **Tableau de bord** : état de l'index (indexés, à passer en OCR, en erreur), questions des 14 derniers jours
  et part restée sans source citée, documents à traiter, dernière synchro par source.
- **Sources** : emplacement, formats, droits par dossier, synchronisation en un clic (en arrière-plan,
  suivi en direct) et historique des passages avec leurs erreurs.
- **Documents** : recherche par chemin, filtres par source et statut, détail d'un document (droits,
  extraits indexés, motif d'erreur).
- **Bac à sable** : poser une question à la place d'un utilisateur (ses groupes), voir la réponse,
  les extraits transmis au modèle et ceux qui ont été cités. Ces questions n'entrent pas dans les statistiques.

Aucune ressource externe : polices, htmx et logo sont servis par l'application (réseau fermé possible).
Le journal des questions contient leur texte : prévoir une purge (`purge_questions`) selon la politique RGPD.

## Droits Windows et annuaire

Avec une source `type: smb`, Alambic Search lit le partage directement en SMB (sans montage) :

- **Identité** : le FileId du serveur, stable malgré renommages et déplacements.
- **Droits** : la DACL NTFS de chaque fichier, traduite en SID autorisés et refusés en lecture.
  Un refus explicite l'emporte toujours (lecture plus stricte que Windows : au pire un document
  lisible reste caché, jamais l'inverse). Les ACE « héritage seul » sont ignorées.
- **Coût** : les droits sont gardés en cache, relus quand le fichier change et au moins toutes
  les `acl_refresh_hours` (24 h par défaut). Samba, par exemple, ne signale pas un changement
  de droits seul : le bouton **Relire tous les droits** de la console force une relecture.
- **Compte de service** : lecture seule, avec « Lire les autorisations ». Un fichier qu'il ne
  peut pas lire apparaît en erreur dans la console.

Avec une section `identity: type: ldap`, l'utilisateur (en-tête `X-User`, champ « Utilisateur »
du bac à sable) est résolu dans l'Active Directory : son SID, tous ses groupes via `tokenGroups`
(imbriqués et groupe principal compris), plus « Tout le monde » et « Utilisateurs authentifiés ».
Les noms de groupes sont aussi ajoutés, ce qui permet d'écrire les règles des sources
`filesystem` avec des noms AD. La console affiche les noms plutôt que les SID.

Tests contre un vrai serveur (Linux, root) : `make samba` puis `make test-smb`.

## Fichiers bureautiques : conversion en PDF

Avec `CONVERT_URL` (service Gotenberg/LibreOffice, lancé par `make db` et `make up`), les
fichiers Word, Excel, PowerPoint et OpenDocument (.docx .doc .odt .rtf .xlsx .xls .ods .pptx .ppt
.odp) sont convertis en PDF avant l'extraction :

- **vraies pages** : un .docx n'est plus vu comme une seule page, les citations sont exactes ;
- **Alambic Light** reçoit le PDF : les documents bureautiques sont typés eux aussi ;
- **lien de citation** : `/documents/…?pdf=true#page=N` ouvre le PDF converti à la volée,
  à la page citée (droits revérifiés) ; sans `pdf=true`, le fichier d'origine.

Le PDF n'est jamais conservé. Échec de conversion = document en erreur, retenté au passage
suivant. Sans `CONVERT_URL` : .docx lu sans pagination, autres formats bureautiques ignorés.

## Réponses : formulées par le LLM ou extraits seuls

La formulation par le LLM est optionnelle.

- **Sans LLM** (`LLM_MODEL=` vide dans `.env`) : aucune génération, pas de modèle à héberger.
  Une question renvoie les extraits les plus pertinents des documents accessibles, classés et
  cités (fichier, page, lien). `make pull-models` ne télécharge alors que le modèle d'embeddings.
- **Par question** : `"formuler": false` dans `/ask`, case « Formuler une réponse avec le LLM »
  du bac à sable, `make ask ... EXTRAITS=1` en ligne de commande.

Les droits s'appliquent de la même façon dans les deux cas. `/ask` renvoie `formulee`
(true/false) et, sans formulation, `extraits` (texte, fichier, page, type).

**Identifiant dans la question** (n° de permis, de facture, de contrat : au moins 5 caractères
avec un chiffre et une lettre ou un tiret, ex. `G6006-270173-05`) : seuls les documents qui le
contiennent tel quel, dans leur texte ou leurs données extraites, sont retenus ; la recherche par
ressemblance n'en ajoute pas d'autres. Si aucun document ne le contient, la réponse le dit et
revient à la ressemblance. `/ask` renvoie `identifiants` et `identifiants_introuvables`.

**Vignettes** : chaque résultat montre l'aperçu de la page trouvée (clic : ouvre le document à
cette page). Rendues à la volée depuis la source (PDF, images ; bureautique si la conversion PDF
est configurée), gardées en mémoire seulement, jamais écrites sur disque. Droits : ceux de l'index,
comme pour l'extrait affiché ; un document non autorisé répond 404. Route : `GET
/vignettes/{source}/{doc_id}?page=N&l=160|320|480` (WebP).

**Un résultat par document** : case « Un résultat par document » (recherche et bac à sable),
`"par_document": true` dans `/ask`, `par_document=true` sur `/search`. Chaque document n'apparaît
qu'une fois, même trouvé à plusieurs endroits, avec le lien vers sa meilleure page et la liste
des pages trouvées (chacune avec son lien). `k` compte alors des documents ; le LLM reçoit
jusqu'à 3 passages par document et ses citations [n] désignent des documents. `/ask` renvoie
`documents` : `n`, `path`, `type`, `url`, `extrait`, `pages` [{`page`, `url`}].

## Mesurer la qualité : jeu de questions de référence

Avant de régler quoi que ce soit (reclassement, découpage, modèle), on mesure. Le jeu de
questions dit, pour chaque question, quels documents doivent être retrouvés ; la mesure passe
par le même chemin que les utilisateurs, droits compris. Format : `eval/jeu.exemple.yaml`.

```bash
make eval-generer N=40          # questions candidates tirées de l'index → eval/candidates.yaml (à relire !)
cp eval/jeu.exemple.yaml eval/jeu.yaml   # puis y mettre les vraies questions
make eval                       # recherche seule (rapide)
make eval REPONSES=1            # + réponses rédigées : citations justes, mots attendus, « je ne sais pas »
make eval-reference             # la mesure devient la référence ; les suivantes affichent écarts et régressions
```

Indicateurs : document attendu retrouvé dans les k premiers, en premier, rang moyen (MRR),
bonne page ; avec `REPONSES=1`, citations justes, mots attendus, refus quand aucun document ne
répond. **Fuites** : un document déclaré interdit retrouvé ou cité, à corriger en priorité.
Code retour 1 en cas de fuite ou de régression (utilisable en intégration continue).

## RAG : reclassement, questions de suivi, contexte des passages

- **Reclassement** (facultatif) : la recherche hybride ramène 40 candidats, un modèle spécialisé
  (`BAAI/bge-reranker-v2-m3`) lit chaque couple question/passage et les réordonne. `make rerank`
  (CPU) ou `make rerank-gpu`, puis `RERANK_URL=http://127.0.0.1:8082` dans `.env`. Service
  injoignable : ordre de la recherche hybride, sans erreur. Si le conteneur n'arrive pas à télécharger le
  modèle (« relative URL without a base », proxy) : `make rerank-modele` le télécharge sur le
  poste dans `./models`, puis `RERANK_MODEL_ID=/models/bge-reranker-v2-m3` dans `.env`. Bac à sable : case « Reclasser » pour
  comparer. Droits appliqués avant : le modèle ne voit que des passages autorisés.
- **Questions de suivi** : « et pour Durand ? », « quel montant ? ». La page de recherche renvoie
  les 3 derniers échanges ; le LLM réécrit la question pour qu'elle se comprenne seule (affichée
  sous la réponse : « Question comprise… »), puis recherche et réponse se font sur cette question.
  « Nouvelle conversation » repart de zéro. API : `historique` dans `/ask`, `question_comprise`
  dans la réponse. Sans LLM : pas de reformulation.
- **Contexte des passages** : le texte vectorisé commence par le document, son type et la section
  en cours (« Document : Compta / contrat durand (Contrat) — ARTICLE 3 - PÉNALITÉS »). Le texte
  affiché et la recherche par mots ne changent pas. Index existant : `make revectoriser`
  (recalcule les vecteurs depuis la base, sans relire les fichiers ni rappeler Alambic Light).

Mesurer avant/après : `make eval-reference` (état actuel), changer un réglage, `make eval` ;
`alambic-search eval … --sans-reclassement` mesure sans reclassement.

## Alambic Light : OCR, type et données

Avec une section `alambic_light` dans `sources.yaml` (voir `sources.example.yaml`), chaque PDF et
chaque image (PNG, JPEG, TIFF, WEBP, alors indexés aussi) est envoyé à Alambic Light pendant la
synchronisation (`envoyer: tous`), ou seulement ceux sans texte (`envoyer: scans`).

- **Scans** : le texte OCR rendu par Alambic Light est découpé et indexé comme un autre texte.
- **Type et données** : stockés avec le document, filtrables (`types` dans `/search` et `/ask`,
  filtre « Type » de la console et du bac à sable) et résumés dans une **fiche** indexée,
  premier extrait du document : « Fiche Alambic : Facture / Fournisseur : … / Montant TTC : … ».
- **Données douteuses** : marquées « (à vérifier) » dans la fiche ; le LLM a pour consigne de le
  dire s'il s'en sert. Une fois validées par un gestionnaire dans Alambic Light, elles sont
  reprises à la synchronisation suivante (fiche « données validées »).
- **Fichier** : jamais conservé par Alambic Search. Il est envoyé avec un pointeur
  (`source` = `alambic-search:<source>`, `identifiant` = chemin).
- **Pannes** : Alambic Light injoignable → document en erreur, retenté au passage suivant ;
  fichier refusé (format, taille) → texte local seul ; fichier jugé illisible → pas de nouvel
  essai tant qu'il ne change pas.
- Les droits ne changent pas : ceux de la source s'appliquent à la fiche comme au texte.
- **Reprise des validations** : à chaque synchro, les documents qui avaient des données « à
  vérifier » sont relus dans Alambic Light (`GET /v1/documents/{uuid}`). Validés, leurs valeurs
  validées remplacent la fiche. Résultat expiré, ou validé mais données effacées (export
  acquitté) : les valeurs connues restent, marquées « à vérifier », et ne sont plus relues.
  Avec `synchro_minutes` sur la source, cette reprise se fait toute seule.

Le texte OCR est lu dans la réponse du typage ou, à défaut, sur `GET /v1/documents/{uuid}/texte`.
Si Alambic Light n'expose aucun des deux, un scan n'est interrogeable que par sa fiche (signalé
dans la console).

## Comptes, rôles et connexion

Alambic Search gère ses propres comptes (console → **Utilisateurs**, administrateurs seulement).
Premier démarrage, base sans compte : le compte `admin` est créé avec `ADMIN_PASSWORD`. Accès
perdu : `make compte LOGIN=admin ROLE=admin` (ou `alambic-search compte admin --role admin`)
redéfinit le mot de passe et réactive le compte.

| Rôle | Accès |
| --- | --- |
| Utilisateur | Recherche (`/rechercher`), API, « Mon compte » (mot de passe, clés d'API) |
| Gestionnaire | + console : sources, documents, synchros, droits, bac à sable ; `POST /sources/…/sync` |
| Administrateur | + comptes et clés d'API de tous |

Droits sur les documents d'un compte (cumulés) :

- **Compte Windows rattaché** : SID et groupes lus dans l'annuaire (section `identity`), comme
  sous Windows — indispensable pour les partages SMB et SharePoint (droits en SID) ;
- **Groupes saisis** par l'administrateur : noms des règles par dossier (dossier, FTP, S3) et des GED ;
- toujours : Tout le monde, Utilisateurs authentifiés, `u:<identifiant>`.

Sécurité :

- mots de passe : empreinte scrypt, 10 caractères au moins ; changer un mot de passe ferme les
  autres sessions du compte ;
- session : cookie signé (`SESSION_SECRET`), HttpOnly, SameSite=Lax, 8 h ; `COOKIE_SECURE=true`
  derrière HTTPS ; compte, rôle et groupes relus à chaque requête (désactiver un compte coupe
  aussitôt sessions et clés) ;
- 5 échecs en 15 min pour un identifiant ou une adresse : connexion bloquée 15 min (page et HTTP Basic) ;
- clés d'API : `als_<préfixe>_<secret>`, montrées une seule fois, seule l'empreinte SHA-256 est
  gardée ; échéance (30 j, 90 j, 1 an ou aucune), révocables ; un compte sans mot de passe
  n'utilise que des clés (compte d'application) ;
- il reste toujours un administrateur actif ; on ne peut ni se retirer le rôle ni supprimer son propre compte.

## Recherche par critères : « toutes les factures de M. GRUWE »

La recherche par ressemblance classe des extraits et en garde quelques-uns : elle ne convient pas
pour lister. La recherche par critères filtre en SQL sur les données extraites par Alambic Light :

- **documents** : type demandé ET un champ contient la valeur (mot entier, sans casse ni accents :
  « GRUWE » trouve « M. Olivier Gruwé », pas « GRUWEL »). Liste complète (500 max, signalé si
  tronquée) ; une valeur « à vérifier » est signalée, pas écartée ;
- **mentions** (à part) : documents du type, ou non typés, dont seul le texte contient la valeur.

Droits appliqués comme pour les questions. Accès :

- API : `GET /recherche?type=FACTURE&valeur=GRUWE[&champ=client]`
- Console : bac à sable, onglet « Liste par critères »
- Question en langage courant (avec un LLM) : « toutes les factures de M. GRUWE », « combien de
  factures pour Gruwe ? » sont traduites en critères. Le LLM ne choisit pas les documents : type et
  champ doivent exister, la valeur doit figurer dans la question, sinon recherche ordinaire.
  `/ask` renvoie alors `liste` (même format que `/recherche`) et le filtre utilisé.

## Sources : gérées dans la console, enregistrées en base

La base (table `source_configs`) est la seule référence pour la liste des sources.

- **Premier démarrage** : les sources de `sources.yaml` sont importées en base. Ensuite le fichier
  n'est plus lu pour les sources (il garde l'annuaire et Alambic Light). Une source ajoutée plus
  tard au fichier est signalée dans la console, avec un bouton « Importer ».
- **Types** : dossier (droits par dossier), partage Windows SMB (ACL NTFS), FTP/FTPS (MLSD,
  droits par dossier), S3 Amazon ou compatible MinIO/Garage/OVH/Scaleway (droits par préfixe), Alfresco (API
  REST, droits Alfresco refus compris), GED CMIS 1.1 browser binding (Nuxeo, OpenText…, ACL CMIS),
  SharePoint / OneDrive (Graph : utilisateurs et groupes AD synchronisés reconnus par leur SID,
  groupes de site à associer à des groupes AD, liens de partage ignorés). Les trois derniers sont
  testés contre des serveurs simulés : à valider sur une vraie GED / un vrai tenant.
- **Correspondance des droits des GED** : `GROUP_Compta` → groupe AD `Compta` ; utilisateur
  `alice` → `u:alice` (ajouté aux droits de l'utilisateur connecté) ; `GROUP_EVERYONE` → tout le
  monde ; groupes de site Alfresco et rôles internes : ignorés.
- **Console → Sources** : ajouter (dossier avec droits par dossier, ou partage Windows SMB),
  « Tester l'accès » (parcours sans rien enregistrer), modifier, supprimer. Effet immédiat,
  sans redémarrage.
- **Exporter (YAML)** : la liste au format de `sources.yaml`, pour sauvegarder, versionner ou
  préparer une autre installation.
- **Mots de passe et secrets** (SMB, FTP, S3, Alfresco, CMIS, SharePoint) : saisis dans la console,
  ils sont chiffrés (AES-256-GCM) dans la table `source_secrets`, avec la clé `SECRETS_KEY` qui
  reste hors de la base (`.env` ou secret Docker via `SECRETS_KEY_FILE`). Jamais réaffichés,
  journalisés ni exportés ; liés à leur source (recopiés ailleurs, ils ne se déchiffrent pas).
  - Clé : `make secrets-key`, à sauvegarder **à part** de la base. Perdue : secrets à ressaisir
    (index et documents intacts).
  - Rotation : nouvelle clé dans `SECRETS_KEY`, l'ancienne dans `SECRETS_KEY_PREVIOUS`, puis
    `make secrets-rotate`.
  - Toujours possible : la variable d'environnement (`password_env`…), prioritaire si les deux
    sont renseignées. Sans `SECRETS_KEY`, seule cette voie est proposée.
- Le nom d'une source ne se modifie pas (il identifie ses documents dans l'index).
- **Suppression** : confirmée en recopiant le nom ; retire les documents et l'historique de
  l'index, sans toucher aux fichiers.
- `make sync SOURCE=…` lit aussi les sources en base.
- Dans Docker, un dossier doit être monté dans le conteneur (DOCUMENTS_DIR → `/data/partage`).
- Les formulaires de la console refusent les requêtes venues d'un autre site (anti-CSRF).

### Source « texte seul »

Case « Type et données (Alambic Light) » de chaque source (`analyse_light: false` dans le YAML) :

- **Cochée** (défaut) : type, données extraites, fiche interrogeable, validations.
- **Décochée** : texte intégral seulement. Les documents avec du texte ne partent pas vers
  Alambic Light ; les scans y sont envoyés pour leur seul texte, avec `ocr_only=true` sur
  `POST /v1/documents/typage` (un type ou des données éventuellement renvoyés sont ignorés).
- **Lecture des scans par l'IA** (case visible en texte seul, `ocr_ia: true`) : ajoute `ocr=ia`
  (`ocr_only=true&ocr=ia`), plus fiable sur les pièces d'identité, documents abîmés ou manuscrits,
  mais payant. Ne s'applique qu'aux scans nouveaux ou modifiés.
- Décocher retire, à la synchro suivante, les types, données et fiches déjà extraits. Recocher
  fait analyser, à la synchro suivante, les documents qui ne l'ont pas été.

## Synchronisations planifiées

« Synchronisation automatique » (en minutes) sur une source, dans la console : resynchronisation automatique (nouveaux
fichiers, droits, validations Alambic Light). Minimum 5 minutes ; sans ce réglage, synchro
manuelle (console, `make sync`). Le planificateur tourne dans l'API : une seule instance d'API
par base pour l'instant.

## Production : Docker Swarm

`deploy/swarm/` contient la stack de production (`stack.yml`) : `caddy` (HTTPS), `api` (2 instances
et plus, sans état), `worker` (synchronisations planifiées), `db` + `backup`, `ollama`, `rerank`,
`gotenberg`. Secrets Docker (`secrets.sh`, variables `*_FILE` lues sous `/run/secrets`),
placement par étiquettes de nœuds (`alambic.db`, `alambic.gpu`, `alambic.edge`).

```bash
sh deploy/swarm/secrets.sh                 # une fois, sur un manager
cp deploy/swarm/stack.env.example deploy/swarm/stack.env      # puis adapter
cp deploy/swarm/sources.example.yaml deploy/swarm/sources.yaml
make push && make stack-deploy             # construire, publier, déployer
make stack-ps                              # état
```

Plusieurs instances partagent un verrou PostgreSQL par source : une source n'est jamais
synchronisée deux fois en même temps (api, worker, ligne de commande). `SCHEDULER_ENABLED=false`
désactive la planification sur une instance. Base perdue : PostgreSQL recharge seul la dernière
sauvegarde au démarrage sur un dossier vide (`RESTORE_ON_EMPTY`), et sans sauvegarde l'instance qui
planifie resynchronise toutes les sources (`AUTO_SYNC_EMPTY`). Installation, mises à jour, ajout et retrait de
nœuds, sauvegardes, rotations, incidents : dossier « Alambic Search — Dossier d'architecture et
d'exploitation ».

## API

Collection Postman prête à importer : `docs/postman/alambic-search.postman_collection.json`
(questions, recherche, listes par critères, ouverture du document cité, vignette, synchro).
Schéma complet : `/openapi.json` (importable aussi dans Postman), documentation interactive : `/docs`.

| Méthode | Route | Rôle |
| --- | --- | --- |
| `POST` | `/ask` | `{"question", "k", "types", "formuler", "par_document"}` → réponse, `citations` (fichier, page, lien), `extraits` ou `documents`, `identifiants`, `liste` |
| `GET` | `/search?q=…&k=8&types=FACTURE&par_document=true` | Extraits classés (ou un élément par document), sans LLM |
| `GET` | `/recherche?valeur=GRUWE&type=FACTURE&champ=…&mentions=true` | Liste exhaustive par critères (données extraites) |
| `GET` | `/documents/{source}/{doc_id}` | Ouvre le fichier après **revérification en direct** des droits (`?pdf=true` : bureautique en PDF) |
| `GET` | `/vignettes/{source}/{doc_id}?page=N&l=320` | Aperçu WebP de la page |
| `POST` | `/sources/{nom}/sync` | Synchro bloquante (scripts). Rôle **gestionnaire** ou **administrateur**. 409 si une synchro tourne ou si le garde-fou de suppression se déclenche |
| `GET` | `/health` | État et sources chargées (sans authentification) |

Authentification (toutes les routes sauf `/health`), avec les droits du compte :

- **Clé d'API** (recommandé pour une application) : `Authorization: Bearer als_…` ou
  `X-API-Key: als_…`, créée dans « Mon compte » ou par un administrateur ;
- **Identifiant et mot de passe** : HTTP Basic (freiné après 5 échecs) ;
- **Session** du navigateur (après `/connexion`) ;
- **En-têtes** `X-User` (résolu dans l'annuaire) / `X-User-Groups` : seulement avec
  `TRUST_IDENTITY_HEADERS=true` (développement, ou derrière un proxy qui fixe ces en-têtes).

Identifiants explicites (clé, Basic) l'emportent sur le cookie de session. Clé invalide, expirée
ou révoquée : 401 ; rôle insuffisant : 403.

En ligne de commande (pour une synchro planifiée) :

```bash
make sync SOURCE=partage-commun
make ask Q="Quelle pénalité de retard ?" GROUPS=compta
```

## Fonctionnement

```
ged/
  connectors/base.py        interface commune : lister, lire, droits, localiser
  connectors/filesystem.py  partage réseau : identité par inode, droits par dossier, refus par défaut
  extract.py                texte page par page (PDF, DOCX, TXT/MD/CSV) ; scans et images → Alambic Light
  alambic_light.py          client Alambic Light (typage, attente 202, texte OCR, fiche)
  chunking.py               extraits ~1200 caractères, chevauchement 200, jamais à cheval sur 2 pages
  sync.py                   delta : ajout, modification (hash), déplacement, droits, suppression
  store.py                  PostgreSQL + pgvector, recherche hybride RRF filtrée par droits
  answer.py                 contexte numéroté → LLM → citations [n] vérifiées
  api.py / cli.py           points d'entrée
```

Comportements à connaître :

- **Identité stable** : un fichier renommé ou déplacé n'est pas réindexé, seuls son chemin
  et ses droits sont mis à jour.
- **Contenu inchangé** (date modifiée, hash identique) : pas de réindexation.
- **Garde-fou** : si plus de 50 % des documents disparaissent d'un coup (partage démonté),
  la synchro s'arrête sans rien supprimer. Idem si le parcours échoue en cours de route.
- **Erreurs** : un fichier illisible est journalisé et retenté au passage suivant.
- **Doublons** : une même copie présente à plusieurs endroits n'apparaît qu'une fois dans le contexte.
- **Recherche plein texte** : configuration `french` (racinisation), termes combinés en OU.
- `EMBED_DIM` fixe la dimension de la colonne vecteur à la création : changer de modèle
  d'embeddings impose de recréer l'index.

## Développement

Dépendances gérées par uv (`pyproject.toml` + `uv.lock`, versionné).

```bash
cp .env.example .env               # variables locales (base, Ollama, mot de passe console)
cp sources.example.yaml sources.yaml   # chemins adaptés à ton poste
make install        # crée .venv et installe tout (dev compris)
make db             # base PostgreSQL/pgvector (port 5433) + Ollama (11434) en Docker
make pull-models    # une seule fois
make run            # API + console en local, rechargement automatique
make check          # lint (ruff) + tests
make format         # reformate le code
make test-pg        # tests + intégration PostgreSQL/pgvector (GED_TEST_DSN=...)
make lock           # après un ajout ou une mise à jour de dépendance
```

`make run`, `make sync` et `make ask` lisent `.env` s'il existe. La base du projet écoute sur le
port **5433** pour ne pas entrer en conflit avec un PostgreSQL déjà installé sur le poste (5432).
Une base existante convient aussi, à condition d'y avoir l'extension pgvector : il suffit
d'adapter `DATABASE_URL`.

## Tests

Les tests couvrent l'extraction, le découpage, le connecteur, la synchro (ajout, modification,
déplacement, suppression, garde-fous, OCR requis, erreurs), le filtrage des droits avant le LLM,
les citations et l'API de bout en bout.

## Hors POC / étapes suivantes

- Indexation dans un worker séparé (la console ne partage plus le processus des synchros)
- ACL NTFS réelles (SID résolus via l'AD) et agent local avec journal USN
- Routeur : questions agrégées (montants, dates) en SQL sur les champs extraits
- Connecteurs SharePoint (Graph, delta token) et CMIS
- Traitement asynchrone (Celery) pour le scan initial des gros volumes
- Journal d'audit des questions et des accès
