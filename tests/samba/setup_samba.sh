#!/usr/bin/env bash
# Monte un serveur Samba de test avec des ACL Windows (acl_xattr) sur le port 445.
# Usage (root) : tests/samba/setup_samba.sh [dossier]
# Utilisé par tests/test_smb.py (GED_TEST_SMB=1). Idempotent.
set -euo pipefail

ROOT="${1:-/tmp/ged-samba}"
SHARE="$ROOT/share"
CONF="$ROOT/smb.conf"
PASS="Test-1234"

mkdir -p "$SHARE" "$ROOT/ncalrpc" "$ROOT/private" "$ROOT/lock" "$ROOT/state" "$ROOT/cache" "$ROOT/log" "$ROOT/run"

cat > "$CONF" <<EOF
[global]
    workgroup = TESTDOM
    netbios name = FICHIERS
    server role = standalone server
    smb ports = 445
    bind interfaces only = yes
    interfaces = lo
    private dir = $ROOT/private
    lock directory = $ROOT/lock
    state directory = $ROOT/state
    cache directory = $ROOT/cache
    pid directory = $ROOT/run
    ncalrpc dir = $ROOT/ncalrpc
    log file = $ROOT/log/%m.log
    passdb backend = tdbsam:$ROOT/private/passdb.tdb
    map to guest = never
    server min protocol = SMB2_10
    disable spoolss = yes
    load printers = no

[Commun]
    path = $SHARE
    read only = no
    vfs objects = acl_xattr
    map acl inherit = yes
    store dos attributes = yes
EOF

# Comptes et groupes Unix (les SID Windows sont attribués par Samba)
for g in compta rh stagiaires; do getent group "$g" >/dev/null || groupadd "$g"; done
add_user() { id "$1" >/dev/null 2>&1 || useradd -M -s /usr/sbin/nologin "$1"; usermod -G "$2" "$1"; }
add_user svc-alambic compta,rh   # compte de service : lecture seule (données + autorisations)
add_user admin-test compta       # administrateur : pose les ACL (le compte de service ne le peut pas)
add_user alice compta
add_user bob rh
add_user claire compta,stagiaires

for u in svc-alambic admin-test alice bob claire; do
  (echo "$PASS"; echo "$PASS") | smbpasswd -c "$CONF" -s -a "$u" >/dev/null
done
for g in compta rh stagiaires; do
  net -s "$CONF" groupmap add ntgroup="$g" unixgroup="$g" type=domain >/dev/null 2>&1 || true
done

# Arborescence de test
mkdir -p "$SHARE/Compta/Factures" "$SHARE/RH" "$SHARE/Public"
echo "Facture ACME : montant total 4 850 euros HT, paiement à 30 jours." > "$SHARE/Compta/Factures/facture-acme.txt"
echo "Contrat Durand : pénalité de retard de 2 % par semaine." > "$SHARE/Compta/contrat-durand.txt"
echo "Grille des salaires confidentielle 2026." > "$SHARE/RH/salaires.txt"
echo "L'accueil est ouvert du lundi au vendredi." > "$SHARE/Public/horaires.txt"
chmod -R a+rwX "$SHARE"   # les droits réels sont portés par les ACL Windows ci-dessous
# repartir d'ACL neutres à chaque exécution (xattr NT supprimé)
find "$SHARE" -type f -exec setfattr -x security.NTACL {} \; 2>/dev/null || true

pkill -f "smbd.*$CONF" 2>/dev/null || true
sleep 0.5
smbd -D -s "$CONF"
for _ in $(seq 1 30); do
  smbclient -s "$CONF" -U "svc-alambic%$PASS" //127.0.0.1/Commun -c "ls" >/dev/null 2>&1 && break
  sleep 0.3
done

ACL() { smbcacls -s "$CONF" -U "admin-test%$PASS" //127.0.0.1/Commun "$@"; }
SVC='ACL:FICHIERS\admin-test:ALLOWED/0/FULL,ACL:FICHIERS\svc-alambic:ALLOWED/0/READ'
# Compta : groupe compta, sauf les stagiaires (refus explicite sur le contrat)
ACL "Compta/Factures/facture-acme.txt" --set "$SVC,ACL:FICHIERS\\compta:ALLOWED/0/READ" >/dev/null
ACL "Compta/contrat-durand.txt" --set "$SVC,ACL:FICHIERS\\compta:ALLOWED/0/READ,ACL:FICHIERS\\stagiaires:DENIED/0/READ" >/dev/null
# RH : groupe rh
ACL "RH/salaires.txt" --set "$SVC,ACL:FICHIERS\\rh:ALLOWED/0/READ" >/dev/null
# Public : tout le monde
ACL "Public/horaires.txt" --set "$SVC,ACL:Everyone:ALLOWED/0/READ" >/dev/null

echo "Samba prêt sur 127.0.0.1:445, partage Commun ($SHARE)"
