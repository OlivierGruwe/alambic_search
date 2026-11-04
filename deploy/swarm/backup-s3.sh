#!/bin/sh
# Copie les sauvegardes de /backups (pg_dump quotidien du service backup) vers un stockage S3 :
# AWS, OVHcloud, Scaleway, Outscale, MinIO… Activé par BACKUP_S3=1 dans stack.env (stack.s3.yml).
# Copie seulement (jamais de suppression côté S3) : la durée de conservation se règle par une règle
# de cycle de vie du bucket (ex. 90 jours). Identifiants : secrets search_s3_access_key / _secret_key,
# compte limité à l'écriture dans ce bucket (pas besoin de créer ni de lister les buckets).
set -eu
export RCLONE_CONFIG_S3_TYPE=s3
# Sans adresse (S3_ENDPOINT vide) : AWS. Une région vide fausse la signature (AuthorizationHeaderMalformed) :
# défaut us-east-1, à remplacer par la région du bucket (eu-west-3 = Paris).
if [ -z "${S3_ENDPOINT:-}" ]; then PROVIDER_DEFAULT=AWS; else PROVIDER_DEFAULT=Other; fi
export RCLONE_CONFIG_S3_PROVIDER="${S3_PROVIDER:-$PROVIDER_DEFAULT}"
export RCLONE_CONFIG_S3_ENDPOINT="${S3_ENDPOINT:-}"
export RCLONE_CONFIG_S3_REGION="${S3_REGION:-us-east-1}"
# clés saisies sous Windows : retours chariot et espaces retirés
export RCLONE_CONFIG_S3_ACCESS_KEY_ID="$(tr -d ' \r\n' < /run/secrets/search_s3_access_key)"
export RCLONE_CONFIG_S3_SECRET_ACCESS_KEY="$(tr -d ' \r\n' < /run/secrets/search_s3_secret_key)"
export RCLONE_S3_NO_CHECK_BUCKET=true          # le bucket existe déjà : pas de droit de création requis
if [ -n "${S3_SSE:-}" ]; then                    # chiffrement côté serveur (ex. AES256)
  export RCLONE_CONFIG_S3_SERVER_SIDE_ENCRYPTION="$S3_SSE"
fi
DEST="s3:${S3_BUCKET:?S3_BUCKET manquant dans stack.env}/${S3_PREFIX:-alambic-search}"
INTERVAL="${S3_INTERVAL:-3600}"
echo "Copie des sauvegardes vers ${DEST} (${RCLONE_CONFIG_S3_PROVIDER}, région ${RCLONE_CONFIG_S3_REGION}) toutes les ${INTERVAL} s"
while true; do
  # --immutable : un fichier déjà envoyé n'est jamais réécrit (une sauvegarde ne change pas)
  if rclone copy /backups "$DEST" --include '*.dump' --immutable --stats 0 -q; then
    echo "$(date -Iseconds) OK sauvegardes copiées vers ${DEST}"
  else
    echo "$(date -Iseconds) ÉCHEC copie vers ${DEST} (identifiants, réseau ou bucket) : nouvel essai au prochain passage" >&2
  fi
  sleep "$INTERVAL"
done
