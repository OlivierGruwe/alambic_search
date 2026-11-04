#!/bin/sh
# Prépare le cluster pour Alambic Search : réseau partagé et secrets. À lancer UNE fois, sur un manager.
# Les valeurs générées ne sont affichées qu'une fois : les noter dans le coffre de l'équipe.
set -eu

# Réseau partagé avec Light, chiffré sauf STACK_CHIFFRE=0 (Docker Desktop : pas de module IPsec)
if ! docker network inspect alambic >/dev/null 2>&1; then
  if [ "${STACK_CHIFFRE:-1}" = 0 ]; then
    docker network create --driver overlay --attachable alambic
  else
    docker network create --driver overlay --attachable --opt encrypted alambic
  fi
fi

gen() { head -c 32 /dev/urandom | base64 | tr -d '\n=+/' | cut -c1-40; }
key() { head -c 32 /dev/urandom | base64 | tr '+/' '-_' | tr -d '\n='; }

creer() {  # creer <nom> <valeur>
  if docker secret inspect "$1" >/dev/null 2>&1; then
    echo "  $1 : existe déjà (inchangé)"
  else
    printf '%s' "$2" | docker secret create "$1" - >/dev/null
    echo "  $1 : créé"
  fi
}

echo "Secrets d'Alambic Search :"
creer search_db_password "$(gen)"
creer search_session_secret "$(gen)"
SK="$(key)"
if ! docker secret inspect search_secrets_key >/dev/null 2>&1; then
  echo "  SECRETS_KEY (à conserver hors du cluster, sans elle les mots de passe des sources sont perdus) :"
  echo "    $SK"
fi
creer search_secrets_key "$SK"
printf "Mot de passe du compte « admin », créé au premier démarrage (vide = généré, affiché) : "; read -r ADMIN || true
if [ -z "${ADMIN:-}" ] && ! docker secret inspect search_admin_password >/dev/null 2>&1; then
  ADMIN="$(gen)"; echo "  mot de passe de « admin » : $ADMIN"
fi
creer search_admin_password "${ADMIN:-inchange}"
printf "Clé d'API du LLM en ligne, ex. Mistral AI (vide : LLM local, Ollama) : "; read -r LLMKEY || true
creer search_llm_api_key "${LLMKEY:-aucune}"
printf "Clé d'API d'Alambic Light (vide si pas encore connue) : "; read -r LIGHT || true
creer search_light_api_key "${LIGHT:-a-definir}"
if [ "${BACKUP_S3:-0}" = 1 ]; then
  echo "Copie des sauvegardes vers S3 (BACKUP_S3=1) :"
  # Saisie visible : la saisie masquée (stty) duplique les collages sous Git Bash
  printf "  ID de clé d'accès (AWS : 20 caractères, AKIA…) : "; read -r S3AK || true
  printf "  Clé d'accès secrète (AWS : 40 caractères) : "; read -r S3SK || true
  S3AK=$(printf '%s' "${S3AK:-}" | tr -d ' \r\n'); S3SK=$(printf '%s' "${S3SK:-}" | tr -d ' \r\n')
  case "$S3AK" in */*) echo "  L'ID de clé contient un / : c'est sans doute la clé secrète. Rien n'est créé." >&2; exit 1;; esac
  [ -n "$S3AK" ] && creer search_s3_access_key "$S3AK"
  [ -n "$S3SK" ] && creer search_s3_secret_key "$S3SK"
  clear 2>/dev/null || true   # efface la clé secrète de l'écran
fi
echo "Terminé. Valeurs lisibles plus tard : non (Docker ne les rend jamais) — d'où le coffre."
