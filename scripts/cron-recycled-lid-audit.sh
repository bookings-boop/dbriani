#!/bin/bash
# Recycled-LID monitor (Stage 0 of the phone-keyed identity cure) — 2026-06-02.
# Read-only audit; alerts the operator via Telegram ONLY if a genuine
# recycled-LID suspect appears while we hold the full phone-keyed migration
# (other session owns canonicalize_cid/reconcile). Logs to cron.log.
cd /home/ubuntu/hermes-bridge || exit 0
set -a; . ./.env; set +a
OUT=$(AUDIT_CAP=60 /usr/bin/python3 /home/ubuntu/hermes-bridge/recycled_lid_audit.py 2>&1)
N=$(printf '%s\n' "$OUT" | grep -oE 'SUSPECTS: [0-9]+' | grep -oE '[0-9]+$')
echo "$(date -u +%FT%TZ) recycled-lid-audit suspects=${N:-ERR}" >> /home/ubuntu/hermes-bridge/cron.log
if [ "${N:-0}" -gt 0 ] 2>/dev/null; then
  printf '%s\n' "$OUT" >> /home/ubuntu/hermes-bridge/cron.log
  curl -s -m 10 "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${ADMIN_CHAT_ID}" \
    --data-urlencode "text=⚠️ Recycled-LID monitor: ${N} suspect(s) found — a customer may be showing under the wrong name (Qurbani-class). Ping Claude to fix, or run scripts/recycled_lid_audit.py for details." \
    >> /home/ubuntu/hermes-bridge/cron.log 2>&1
fi
