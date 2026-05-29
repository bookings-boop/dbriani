#!/bin/bash
# Weekly disk-hygiene cleanup. Runs from cron (Sundays). Belt-and-suspenders
# against the 2026-05-29 disk-full outage; the daily backup.sh handles dump
# retention (KEEP=3) and n8n auto-prunes executions at 7d
# (EXECUTIONS_DATA_PRUNE / MAX_AGE=168h), so this just clears the slower-moving
# cruft: stale bridge backups + docker build cache / dangling images.
set -uo pipefail
LOG=/home/ubuntu/backups/cleanup.log
log() { echo "[$(date '+%F %T')] $1" >> "$LOG"; }

log "===== WEEKLY CLEANUP START (disk $(df -h / | awk 'NR==2{print $5}')) ====="

# 1. Bridge .bak files: always keep the newest 3; of the older ones, delete
#    those more than 7 days old.
n=0
if cd /home/ubuntu/hermes-bridge 2>/dev/null; then
  for old in $(ls -1t -- *.bak.* 2>/dev/null | tail -n +4); do
    if [ -n "$(find "$old" -mtime +7 -print 2>/dev/null)" ]; then
      rm -f -- "$old" && n=$((n + 1))
    fi
  done
fi
log "[1/2] bridge .bak: removed $n (kept newest 3, older-than-7d)"

# 2. Docker build cache + dangling images. NEVER touches running containers,
#    their images, or named volumes (no -a-without-filter, no --volumes).
bc=$(docker builder prune -f  2>&1 | grep -i reclaimed || echo "build cache: 0B")
im=$(docker image   prune -f  2>&1 | grep -i reclaimed || echo "dangling images: 0B")
log "[2/2] docker: ${bc}; ${im}"

log "===== WEEKLY CLEANUP DONE  (disk $(df -h / | awk 'NR==2{print $5}')) ====="
