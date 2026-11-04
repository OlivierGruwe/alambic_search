# Alambic Search — commandes courantes. `make` ou `make help` pour la liste.

.DEFAULT_GOAL := help

# Réglages locaux (.env) : modèles, mot de passe de la console…
-include .env

PORT ?= 8002
EMBED_MODEL ?= bge-m3
LLM_MODEL ?= qwen2.5:7b
SOURCE ?= partage-commun
# SOURCE donnée explicitement (make … SOURCE=x), pas la valeur par défaut ci-dessus réservée à make sync
SOURCE_OPT = $(if $(filter-out file,$(origin SOURCE)),--source $(SOURCE))
# Base de test PostgreSQL/pgvector (make test-pg) : surcharger si besoin
GED_TEST_DSN ?= postgresql://ged:ged@localhost:5433/ged

.PHONY: compte light-check help install lock db db-gpu up-gpu gpu-check run test test-pg test-smb samba lint format check sync ask up down logs pull-models clean eval eval-init eval-reference eval-generer secrets-key secrets-rotate rerank rerank-gpu rerank-modele revectoriser image push stack-deploy stack-ps stack-logs stack-rollback

help: ## Affiche cette aide
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

# --- Développement ----------------------------------------------------------------

install: ## Crée l'environnement et installe les dépendances (dev comprises)
	uv sync

lock: ## Met à jour uv.lock après un changement de dépendances
	uv lock

# Variables locales lues dans .env s'il existe (voir .env.example)
ENV_FILE := $(if $(wildcard .env),--env-file .env,)

db: ## Démarre la base (5433), Ollama (11434) et la conversion PDF (3000) pour le développement local
	docker compose up -d db ollama gotenberg

db-gpu: ## Comme db, avec Ollama sur la carte NVIDIA
	docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d db ollama gotenberg

gpu-check: ## Vérifie qu'Ollama utilise la carte graphique (colonne PROCESSOR)
	docker compose exec ollama nvidia-smi --query-gpu=name,memory.used,memory.total --format=csv
	docker compose exec ollama ollama ps

run: ## Lance l'API et la console en local, rechargement automatique (base : make db)
	uv run $(ENV_FILE) uvicorn ged.api:app_from_env --factory --reload --reload-dir ged --port $(PORT)

test: ## Lance les tests (sans PostgreSQL)
	uv run pytest -q

test-pg: ## Lance les tests, y compris l'intégration PostgreSQL/pgvector
	GED_TEST_DSN=$(GED_TEST_DSN) uv run pytest -q

samba: ## Monte un serveur Samba de test avec ACL Windows (Linux, root)
	sudo tests/samba/setup_samba.sh

test-smb: ## Tests d'intégration SMB/ACL NTFS (après make samba)
	GED_TEST_SMB=1 uv run pytest -q tests/test_smb.py

lint: ## Vérifie le style et les erreurs courantes
	uv run ruff check ged tests

format: ## Reformate le code
	uv run ruff check --fix ged tests
	uv run ruff format ged tests

check: lint test ## Lint + tests (avant un commit)

# --- Exploitation -----------------------------------------------------------------

sync: ## Synchronise une source : make sync SOURCE=partage-commun
	uv run $(ENV_FILE) alambic-search sync $(SOURCE)

# --- Production (Docker Swarm) ----------------------------------------------------------
STACK ?= search
STACK_ENV = deploy/swarm/stack.env
stack_vars = set -a && . ./$(STACK_ENV) && set +a

image: ## Construit l'image de production : make image (REGISTRY et TAG lus dans deploy/swarm/stack.env)
	@test -f $(STACK_ENV) || (echo "Créer $(STACK_ENV) depuis deploy/swarm/stack.env.example" && exit 1)
	$(stack_vars) && docker build -t $$REGISTRY/alambic-search:$$TAG .

push: image ## Construit et pousse l'image vers le registre
	$(stack_vars) && docker push $$REGISTRY/alambic-search:$$TAG

stack-deploy: ## Déploie ou met à jour la stack sur le Swarm (depuis un manager)
	@test -f deploy/swarm/sources.yaml || (echo "Créer deploy/swarm/sources.yaml depuis sources.example.yaml" && exit 1)
	$(stack_vars) && docker stack deploy -c deploy/swarm/stack.yml \
	  $$([ "$${STACK_GPU:-1}" = 0 ] || echo -c deploy/swarm/stack.gpu.yml) \
	  $$([ "$${STACK_CHIFFRE:-1}" = 0 ] || echo -c deploy/swarm/stack.chiffre.yml) --with-registry-auth --prune $(STACK)

stack-ps: ## État des services et des tâches de la stack
	docker stack services $(STACK)
	docker stack ps $(STACK) --no-trunc --filter desired-state=running

stack-logs: ## Journaux d'un service : make stack-logs S=api
	docker service logs --tail 200 -f $(STACK)_$(S)

stack-rollback: ## Revient à la version précédente d'un service : make stack-rollback S=api
	docker service rollback $(STACK)_$(S)

rerank: ## Démarre le reclassement (bge-reranker-v2-m3, CPU) sur http://127.0.0.1:8082 — puis RERANK_URL dans .env
	docker compose --profile rerank up -d rerank
	@echo "Premier démarrage : téléchargement du modèle (~2 Go). Suivi : docker compose logs -f rerank"

rerank-modele: ## Télécharge le modèle de reclassement sur le poste (./models), si le conteneur n'y arrive pas
	uvx --from huggingface_hub hf download BAAI/bge-reranker-v2-m3 --local-dir models/bge-reranker-v2-m3
	@echo "Puis dans .env : RERANK_MODEL_ID=/models/bge-reranker-v2-m3 ; et make rerank"

rerank-gpu: ## Comme rerank, sur la carte NVIDIA (RERANK_IMAGE dans .env)
	docker compose -f docker-compose.yml -f docker-compose.gpu.yml --profile rerank up -d rerank

revectoriser: ## Recalcule les vecteurs (contexte document/section) sans relire les fichiers : make revectoriser [SOURCE=…]
	uv run $(ENV_FILE) alambic-search revectoriser $(SOURCE_OPT)

JEU ?= eval/jeu.yaml
N ?= 30

eval: ## Mesure la qualité sur le jeu de questions : make eval [JEU=eval/jeu.yaml] [REPONSES=1]
	uv run $(ENV_FILE) alambic-search eval $(JEU) $(if $(REPONSES),--reponses) --sortie eval/derniere.json \
		$(if $(wildcard eval/reference.json),--reference eval/reference.json)

eval-init: ## Crée eval/jeu.yaml directement depuis l'index (questions générées, non relues) : make eval-init [N=30]
	@test ! -f $(JEU) || (echo "$(JEU) existe deja : rien a faire (le supprimer pour le recreer)" && exit 1)
	uv run $(ENV_FILE) alambic-search eval-generer --n $(N) $(SOURCE_OPT) --sortie $(JEU)

eval-reference: ## Fait de la dernière mesure la référence (les suivantes s'y comparent)
	cp eval/derniere.json eval/reference.json

eval-generer: ## Questions candidates tirées de l'index, à relire : make eval-generer [N=30] [SOURCE=…]
	uv run $(ENV_FILE) alambic-search eval-generer --n $(N) $(SOURCE_OPT) --sortie eval/candidates.yaml

secrets-key: ## Génère une clé SECRETS_KEY (chiffrement des mots de passe saisis dans la console)
	@uv run python -m ged.secret_box cle

secrets-rotate: ## Rechiffre les secrets avec SECRETS_KEY (ancienne clé dans SECRETS_KEY_PREVIOUS)
	uv run $(ENV_FILE) python -m ged.secret_box rotation

light-check: ## Teste la connexion à Alambic Light (section alambic_light de sources.yaml)
	uv run $(ENV_FILE) alambic-search light-check

compte: ## Crée un compte ou redéfinit son mot de passe : make compte LOGIN=admin [ROLE=admin]
	uv run $(ENV_FILE) alambic-search compte $(LOGIN) $(if $(ROLE),--role $(ROLE))

ask: ## Pose une question : make ask Q="..." GROUPS=compta [EXTRAITS=1 : sans LLM]
	uv run $(ENV_FILE) alambic-search ask "$(Q)" --groups "$(GROUPS)" $(if $(EXTRAITS),--extraits)

# --- Docker -----------------------------------------------------------------------

up: ## Démarre la pile (base, Ollama, API) — ADMIN_PASSWORD requis
	@test -f sources.yaml || (echo "sources.yaml manquant : cp sources.example.yaml sources.yaml" && exit 1)
	@test -n "$(ADMIN_PASSWORD)" || (echo "Définir ADMIN_PASSWORD dans .env avant de lancer la pile" && exit 1)
	docker compose up -d --build

up-gpu: ## Comme up, avec Ollama sur la carte NVIDIA
	@test -f sources.yaml || (echo "sources.yaml manquant : cp sources.example.yaml sources.yaml" && exit 1)
	@test -n "$(ADMIN_PASSWORD)" || (echo "Définir ADMIN_PASSWORD dans .env avant de lancer la pile" && exit 1)
	docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build

down: ## Arrête la pile
	docker compose down

logs: ## Suit les journaux de l'API
	docker compose logs -f api

pull-models: ## Télécharge dans Ollama (Docker) les modèles EMBED_MODEL et LLM_MODEL de .env
	docker compose exec ollama ollama pull $(EMBED_MODEL)
	$(if $(strip $(LLM_MODEL)),docker compose exec ollama ollama pull $(LLM_MODEL),@echo "LLM_MODEL vide : pas de LLM")

clean: ## Supprime caches et environnement local
	rm -rf .venv .pytest_cache .ruff_cache
	find . -name __pycache__ -prune -exec rm -rf {} +
