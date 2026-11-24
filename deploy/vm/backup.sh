#!/usr/bin/env bash
# Backs up the staging database and the uploaded files on the VM. Runs every night from the
# autoscope-backup systemd timer (installed by scripts/vm_deploy.sh); can also be run by hand:
#
#   bash /opt/autoscope/code/deploy/vm/backup.sh
#
# Each run writes /opt/autoscope/backups/<date>_<time>/ with:
#   db.dump          the whole database (pg_dump custom format; restore with pg_restore)
#   uploads.tar.gz   the uploaded files (photos, KYC documents)
# and keeps only the newest KEEP runs (7 by default). See docs/VM_STAGING.md, "Backups".
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/opt/autoscope/backups}"
KEEP="${KEEP:-7}"
COMPOSE_DIR="${COMPOSE_DIR:-/opt/autoscope/code/deploy/vm}"

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

cd "$COMPOSE_DIR"
umask 077                       # the dumps hold personal data: deploy user only
mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

stamp=$(date -u +%Y-%m-%d_%H%M)
work="$BACKUP_DIR/.$stamp.partial"
rm -rf "$work"
mkdir -p "$work"
trap 'rm -rf "$work"' EXIT

log "backup $stamp: database"
docker compose exec -T db pg_dump -U autoscope -d autoscope --format=custom --compress=6 >"$work/db.dump"
# A dump that pg_restore can't read is worse than none: check it before keeping it.
docker compose exec -T db pg_restore --list <"$work/db.dump" >/dev/null \
  || { log "backup $stamp: FAILED, the database dump is unreadable"; exit 1; }

log "backup $stamp: uploaded files"
docker compose exec -T api tar -czf - -C /data/uploads . >"$work/uploads.tar.gz"
tar -tzf "$work/uploads.tar.gz" >/dev/null \
  || { log "backup $stamp: FAILED, the uploads archive is unreadable"; exit 1; }

mv "$work" "$BACKUP_DIR/$stamp"
trap - EXIT
log "backup $stamp: done ($(du -sh "$BACKUP_DIR/$stamp" | cut -f1))"

# Keep the newest $KEEP runs. Names sort by date, so the oldest come first.
mapfile -t runs < <(find "$BACKUP_DIR" -mindepth 1 -maxdepth 1 -type d -name '20*' -printf '%f\n' | sort)
for ((i = 0; i < ${#runs[@]} - KEEP; i++)); do
  rm -rf "${BACKUP_DIR:?}/${runs[i]}"
  log "removed the old backup ${runs[i]}"
done
