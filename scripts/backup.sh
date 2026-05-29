#!/bin/bash
set -euo pipefail

# ─── Config ───────────────────────────────────────────────────────────────────
BACKUP_DIR="/home/ubuntu/backups"
LOG_FILE="/home/ubuntu/backups/backup.log"
# Keep only the most-recent N of EACH artifact type. Dumps are large
# (~3GB each and growing), so 3 ≈ 9–10GB — fits the 28GB disk with room.
# (Was RETENTION_DAYS=7 with a broken find -o cleanup that never pruned the
#  postgres dumps → disk filled to 100% on 2026-05-29 and took the bot down.)
KEEP=3
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
COMPOSE_DIR="/home/ubuntu/n8n"

# ─── Setup ────────────────────────────────────────────────────────────────────
mkdir -p "$BACKUP_DIR"

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

log "====== BACKUP START ============================="

# ─── 0. Disk guard — never let a dump fill the disk ────────────────────────────
USE=$(df --output=pcent / | tail -1 | tr -dc 0-9)
if [ "${USE:-0}" -ge 92 ]; then
  log "      ⚠️  disk at ${USE}% — pruning BEFORE dump to avoid filling it"
  for pat in 'postgres_*.sql' 'config_*.tar.gz'; do
    ls -1t "$BACKUP_DIR"/$pat 2>/dev/null | tail -n +$((KEEP)) | xargs -r rm -f
  done
fi

# ─── 1. PostgreSQL dump ────────────────────────────────────────────────────────
log "[1/3] Dumping PostgreSQL..."
if docker exec n8n-postgres-1 pg_dump -U n8n n8n > "$BACKUP_DIR/postgres_${TIMESTAMP}.sql"; then
  SIZE=$(du -sh "$BACKUP_DIR/postgres_${TIMESTAMP}.sql" | cut -f1)
  log "      ✓ postgres_${TIMESTAMP}.sql ($SIZE)"
else
  log "      ✗ PostgreSQL dump FAILED"
  rm -f "$BACKUP_DIR/postgres_${TIMESTAMP}.sql"   # don't keep a partial dump
  exit 1
fi

# ─── 2. Config files backup ────────────────────────────────────────────────────
log "[2/3] Backing up config files..."
tar -czf "$BACKUP_DIR/config_${TIMESTAMP}.tar.gz"   -C "$COMPOSE_DIR"   .env docker-compose.yml Caddyfile 2>/dev/null || true
SIZE=$(du -sh "$BACKUP_DIR/config_${TIMESTAMP}.tar.gz" | cut -f1)
log "      ✓ config_${TIMESTAMP}.tar.gz ($SIZE)"

# ─── 3. Cleanup — keep only the newest $KEEP of each type ──────────────────────
# Correct precedence: handle each glob separately. ls -1t = newest first;
# tail -n +$((KEEP+1)) = everything AFTER the newest KEEP → delete those.
log "[3/3] Keeping newest ${KEEP} of each type; removing older..."
DELETED=0
for pat in 'postgres_*.sql' 'config_*.tar.gz'; do
  for f in $(ls -1t "$BACKUP_DIR"/$pat 2>/dev/null | tail -n +$((KEEP+1))); do
    rm -f "$f" && DELETED=$((DELETED+1))
  done
done
log "      ✓ Removed $DELETED old file(s)"

# ─── Summary ──────────────────────────────────────────────────────────────────
TOTAL=$(du -sh "$BACKUP_DIR" --exclude=backup.log | cut -f1)
log "====== BACKUP DONE — total size: $TOTAL ========"
