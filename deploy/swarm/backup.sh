#!/bin/sh
# Sauvegarde quotidienne des bases ged et keycloak (pg_dump, format custom), rétention en jours.
# Restauration : voir l'onglet Exploitation du dossier (pg_restore).
set -eu
export PGPASSWORD="$(cat "$PGPASSFILE_SECRET")"
HOUR="${BACKUP_HOUR:-02}"
KEEP="${BACKUP_KEEP_DAYS:-14}"
echo "Sauvegarde quotidienne à ${HOUR}h, conservation ${KEEP} jours, dans /backups"
until pg_isready -q; do sleep 5; done
while true; do
  if [ "$(date +%H)" = "$HOUR" ] && [ ! -f "/backups/.fait-$(date +%F)" ]; then
    for base in ged keycloak; do
      fichier="/backups/${base}-$(date +%F-%H%M).dump"
      if pg_dump -Fc -d "$base" -f "${fichier}.tmp"; then
        mv "${fichier}.tmp" "$fichier"
        echo "$(date -Is) OK ${fichier} ($(du -h "$fichier" | cut -f1))"
      else
        rm -f "${fichier}.tmp"
        echo "$(date -Is) ÉCHEC sauvegarde ${base}" >&2
      fi
    done
    touch "/backups/.fait-$(date +%F)"
    find /backups -name '*.dump' -mtime +"$KEEP" -delete
    find /backups -name '.fait-*' -mtime +"$KEEP" -delete
  fi
  sleep 300
done
