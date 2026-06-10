#!/bin/bash
# offsite-backup.sh — ADDITIVE encrypted offsite backup (2026-06-10).
#
# Closes the single-disk gap: backup.sh writes everything to the same EC2
# disk that hit 100% on 2026-05-29; bridge .env / crontab / systemd units
# were backed up NOWHERE. This script bundles the full rebuild set,
# encrypts with the dubriani-offsite-backup PUBLIC key (asymmetric — the
# box can encrypt but NEVER decrypt its own backups; the private key
# lives on the operator's Mac + password manager), ships both bundles to
# Google Drive (rclone remote "dubriani-drive", scope=drive.file), and
# prunes old versions remotely. backup.sh is untouched; the data bundle
# REUSES its newest pg dump (no second dump load) unless it is stale.
#
# Cron: 30 3 * * * /home/ubuntu/offsite-backup.sh >> /home/ubuntu/backups/offsite.log 2>&1
set -euo pipefail

LOG=/home/ubuntu/backups/offsite.log
REMOTE="dubriani-drive:dubriani-offsite-backups"
GPG_KEY="dubriani-offsite-backup"
KEEP_DATA=7      # encrypted pg-dump bundles kept offsite
KEEP_CONFIG=30   # encrypted config bundles kept offsite
DUMP_MAX_AGE_H=26
TS=$(date +%Y%m%d_%H%M%S)
LOCKDIR=/tmp/offsite-backup.lock

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG"; }

if ! mkdir "$LOCKDIR" 2>/dev/null; then
  log "SKIP — another offsite run in progress"
  exit 0
fi
WORK=$(mktemp -d /home/ubuntu/backups/offsite-work.XXXXXX)
trap 'rm -rf "$WORK" "$LOCKDIR"' EXIT

log "====== OFFSITE BACKUP START ($TS) ======"

# ── 1. CONFIG BUNDLE — the full rebuild-critical set ────────────────────────
crontab -l > "$WORK/crontab.txt" 2>/dev/null || true
tar -czf "$WORK/config_${TS}.tar.gz" --ignore-failed-read \
  -C /home/ubuntu \
  --exclude='hermes-bridge/__pycache__' \
  --exclude='hermes-bridge/cron.log' \
  --exclude='hermes-bridge/*.bak-*' \
  --exclude='hermes-bridge/*.bak.*' \
  hermes-bridge \
  n8n/.env n8n/docker-compose.yml n8n/Caddyfile \
  .config/systemd/user/hermes-bridge.service \
  .config/systemd/user/hermes-gateway.service \
  .config/systemd/user/hermes-gateway-personal.service \
  -C "$WORK" crontab.txt
log "[1/4] config bundle: $(du -h "$WORK/config_${TS}.tar.gz" | cut -f1)"

# ── 2. DATA BUNDLE — newest pg dump (reuse backup.sh's; fresh if stale) ─────
DUMP=$(ls -1t /home/ubuntu/backups/postgres_*.sql 2>/dev/null | head -1 || true)
FRESH=""
if [ -n "$DUMP" ]; then
  AGE_H=$(( ( $(date +%s) - $(stat -c %Y "$DUMP") ) / 3600 ))
  [ "$AGE_H" -lt "$DUMP_MAX_AGE_H" ] && FRESH="$DUMP"
fi
if [ -n "$FRESH" ]; then
  log "[2/4] reusing dump $(basename "$FRESH") (age ${AGE_H}h)"
  gzip -c "$FRESH" > "$WORK/data_${TS}.sql.gz"
else
  log "[2/4] no fresh dump — running pg_dump"
  docker exec n8n-postgres-1 pg_dump -U n8n n8n | gzip > "$WORK/data_${TS}.sql.gz"
fi
log "      data bundle: $(du -h "$WORK/data_${TS}.sql.gz" | cut -f1)"

# ── 3. ENCRYPT (public key only — box cannot decrypt) + UPLOAD ──────────────
for f in "config_${TS}.tar.gz" "data_${TS}.sql.gz"; do
  gpg --batch --yes --trust-model always -e -r "$GPG_KEY" \
      -o "$WORK/$f.gpg" "$WORK/$f"
  rm -f "$WORK/$f"
done
rclone copyto "$WORK/config_${TS}.tar.gz.gpg" "$REMOTE/config/config_${TS}.tar.gz.gpg"
rclone copyto "$WORK/data_${TS}.sql.gz.gpg"   "$REMOTE/data/data_${TS}.sql.gz.gpg"
log "[3/4] uploaded: config/config_${TS}.tar.gz.gpg + data/data_${TS}.sql.gz.gpg"

# ── 4. REMOTE RETENTION — keep newest N of each, delete the rest ────────────
prune() {  # $1 = subdir, $2 = keep
  local old
  old=$(rclone lsf "$REMOTE/$1" 2>/dev/null | sort | head -n -"$2" || true)
  if [ -n "$old" ]; then
    while IFS= read -r f; do
      [ -n "$f" ] && rclone deletefile "$REMOTE/$1/$f" \
        && log "      pruned $1/$f"
    done <<< "$old"
  fi
}
prune config "$KEEP_CONFIG"
prune data   "$KEEP_DATA"
log "[4/4] retention enforced (config=$KEEP_CONFIG, data=$KEEP_DATA)"

log "====== OFFSITE BACKUP DONE — remote inventory: ======"
rclone lsf --format "stp" "$REMOTE/config" 2>/dev/null | tail -3 | tee -a "$LOG"
rclone lsf --format "stp" "$REMOTE/data"   2>/dev/null | tail -3 | tee -a "$LOG"
