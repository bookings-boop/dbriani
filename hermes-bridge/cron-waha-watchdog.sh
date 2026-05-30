#!/usr/bin/env bash
# cron-waha-watchdog.sh — guarantee the WhatsApp 'default' session stays up.
#
# WHATSAPP_RESTART_ALL_SESSIONS=true is set on the waha container, but it
# proved UNRELIABLE on recreate (2026-05-30: the session came back STOPPED
# and WAHA logged "Session status is not as expected" — its auto-resume
# raced engine init and gave up). After an EC2 reboot the same can happen,
# leaving WhatsApp silently offline until someone notices.
#
# This belt-and-suspenders cron (every few minutes) starts the session
# whenever it is STOPPED. It deliberately acts ONLY on STOPPED — it leaves
# WORKING/STARTING/SCAN_QR_CODE/FAILED untouched so it never fights a state
# that needs a human (e.g. a QR re-pair). Resolves the WAHA container IP
# dynamically (docker reassigns it on recreate), mirroring waha.py's
# self-heal. Logs to waha-watchdog.log and pings the operator on Telegram
# (1h cooldown) when it has to act.
ENVF=/home/ubuntu/hermes-bridge/.env
LOG=/home/ubuntu/hermes-bridge/waha-watchdog.log
COOLDOWN=/home/ubuntu/hermes-bridge/.waha-alert-cooldown
TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# Telegram alert with a 1h cooldown so a flapping session can't spam the operator.
alert() {
  local now last tok chat
  now=$(date +%s)
  last=$(stat -c %Y "$COOLDOWN" 2>/dev/null || echo 0)
  [ $((now - last)) -lt 3600 ] && return 0
  tok=$(grep '^ADMIN_TG_TOKEN' "$ENVF" 2>/dev/null | cut -d= -f2-)
  chat=$(grep '^ADMIN_CHAT_ID' "$ENVF" 2>/dev/null | cut -d= -f2-)
  [ -z "$tok" ] && return 0
  curl -s --max-time 10 "https://api.telegram.org/bot${tok}/sendMessage" \
    --data-urlencode "chat_id=${chat}" --data-urlencode "text=$1" >/dev/null 2>&1
  touch "$COOLDOWN"
}

K=$(grep '^WAHA_API_KEY' "$ENVF" 2>/dev/null | cut -d= -f2-)
IP=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' n8n-waha-1 2>/dev/null)
if [ -z "$IP" ]; then
  echo "$TS waha container not found — skipping" >> "$LOG"
  exit 0
fi

STAT=$(curl -s --max-time 8 "http://$IP:3000/api/sessions/default" \
        -H "X-Api-Key: $K" 2>/dev/null \
        | grep -oE '"status":"[A-Z_]+"' | grep -oE '[A-Z_]+' | head -1)

if [ "$STAT" = "STOPPED" ]; then
  curl -s --max-time 12 -X POST "http://$IP:3000/api/sessions/default/start" \
        -H "X-Api-Key: $K" -H "Content-Type: application/json" -d '{}' \
        >/dev/null 2>&1
  echo "$TS session was STOPPED -> issued /start" >> "$LOG"
  alert "⚠️ Hermes WAHA watchdog: the WhatsApp session was STOPPED — issued /start to bring it back online. Please verify it shows WORKING."
fi
