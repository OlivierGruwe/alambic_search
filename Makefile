# Alambic Search — commandes courantes. `make` ou `make help` pour la liste.

.DEFAULT_GOAL := help

# Réglages locaux (.env) : modèles, mot de passe de la console…
-include .env

PORT ?= 8002
EMBED_MODEL ?= bge-m3
LLM_MODEL ?= qwen2.5:7b
SOURCE ?= partage-commun
# Base de test PostgreSQL/pgvector (make test-pg) : surcharger si besoin
GED_TEST_DSN ?= postgresql://ged:ged@localhost:5433/ged

.PHONY: sso light-check help install lock db db-gpu up-gpu gpu-check run test test-pg test-smb samba lint format check sync ask up down logs pull-models clean eval eval-reference eval-generer secrets-key secrets-rotate

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

JEU ?= eval/jeu.yaml
N ?= 30

eval: ## Mesure la qualité sur le jeu de questions : make eval [JEU=eval/jeu.yaml] [REPONSES=1]
	uv run $(ENV_FILE) alambic-search eval $(JEU) $(if $(REPONSES),--reponses) --sortie eval/derniere.json \
		$(if $(wildcard eval/reference.json),--reference eval/reference.json)

eval-reference: ## Fait de la dernière mesure la référence (les suivantes s'y comparent)
	cp eval/derniere.json eval/reference.json

eval-generer: ## Questions candidates tirées de l'index, à relire : make eval-generer [N=30] [SOURCE=…]
	uv run $(ENV_FILE) alambic-search eval-generer --n $(N) $(if $(SOURCE),--source $(SOURCE)) --sortie eval/candidates.yaml

secrets-key: ## Génère une clé SECRETS_KEY (chiffrement des mots de passe saisis dans la console)
	@uv run python -m ged.secret_box cle

secrets-rotate: ## Rechiffre les secrets avec SECRETS_KEY (ancienne clé dans SECRETS_KEY_PREVIOUS)
	uv run $(ENV_FILE) python -m ged.secret_box rotation

light-check: ## Teste la connexion à Alambic Light (section alambic_light de sources.yaml)
	uv run $(ENV_FILE) alambic-search light-check

sso: ## Démarre Keycloak (connexion unique) sur http://localhost:8180 — comptes de démo : demo
	@docker network inspect alambic_sso >/dev/null 2>&1 || docker network create alambic_sso
	docker compose --profile sso up -d keycloak
	@echo "Console Keycloak : http://localhost:8180/admin (admin / KEYCLOAK_ADMIN_PASSWORD)"
	@echo "Activer dans sources.yaml : section oidc (voir sources.example.yaml)"

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
