#!/bin/sh
# Prépare le cluster pour Alambic Search : réseau partagé et secrets. À lancer UNE fois, sur un manager.
# Les valeurs générées ne sont affichées qu'une fois : les noter dans le coffre de l'équipe.
set -eu

docker network inspect alambic >/dev/null 2>&1 || \
  docker network create --driver overlay --attachable --opt encrypted alambic

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
printf "Mot de passe de la console /admin (vide = généré) : "; read -r ADMIN || true
creer search_admin_password "${ADMIN:-$(gen)}"
printf "Mot de passe administrateur Keycloak (vide = généré) : "; read -r KCADMIN || true
creer keycloak_admin_password "${KCADMIN:-$(gen)}"
printf "Secret du client OIDC alambic-search (Keycloak → Clients → Credentials) : "; read -r OIDC || true
creer search_oidc_client_secret "${OIDC:-a-definir}"
printf "Clé d'API d'Alambic Light (vide si pas encore connue) : "; read -r LIGHT || true
creer search_light_api_key "${LIGHT:-a-definir}"
echo "Terminé. Valeurs lisibles plus tard : non (Docker ne les rend jamais) — d'où le coffre."
