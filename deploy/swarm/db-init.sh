#!/bin/sh
# Premier démarrage de PostgreSQL sur un dossier de données VIDE (l'image ne lance ce script que
# dans ce cas) : si RESTORE_ON_EMPTY=true et qu'une sauvegarde existe dans /backups, rechargement
# automatique de la base ged (index, sources, comptes et clés d'API).
set -e

if [ "${RESTORE_ON_EMPTY:-true}" != "true" ]; then
  echo "Base vide : rechargement automatique désactivé (RESTORE_ON_EMPTY=${RESTORE_ON_EMPTY})"
  exit 0
fi
for base in ged; do
  dernier="$(ls -1 /backups/${base}-*.dump 2>/dev/null | sort | tail -1 || true)"
  if [ -z "$dernier" ]; then
    echo "Base vide : aucune sauvegarde de ${base} dans /backups — elle repart de zéro"
    [ "$base" = ged ] && echo "  (Alambic Search resynchronisera toutes ses sources au démarrage)"
    continue
  fi
  echo "Base vide : rechargement de ${base} depuis ${dernier}"
  pg_restore --username "$POSTGRES_USER" --dbname "$base" --no-owner --role "$POSTGRES_USER" \
    --exit-on-error "$dernier"
  echo "  ${base} rechargée"
done
