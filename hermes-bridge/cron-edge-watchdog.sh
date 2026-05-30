#!/usr/bin/env bash
# cron-edge-watchdog.sh — keep the public edge (caddy -> n8n) serving.
#
# Caddy is the ONLY path for inbound WhatsApp webhooks
# (WAHA -> https://<n8n-domain>/webhook/whatsapp). If caddy is down, or lands
# WITHOUT its docker network after a reboot (a known failure: a bare
# `docker start` attaches no network), or n8n itself is down, inbound customer
# messages are SILENTLY LOST (seen 2026-05-30: ECONNREFUSED to :443 during a
# load spike). restart:unless-stopped only covers a crash — not an
# up-but-unhealthy or network-detached caddy.
#
# This checks the public edge END-TO-END (DNS + caddy + TLS + routing + n8n)
# via n8n's /healthz and, on SUSTAINED failure (3 misses ~8s apart, to ignore
# transient blips), runs `docker compose up -d` — the documented recovery that
# re-attaches networks and starts anything down (a no-op for a healthy stack).
# Logs to edge-watchdog.log and pings the operator on Telegram (1h cooldown).
URL="https://n8n.13-63-82-112.sslip.io/healthz"
COMPOSE_DIR=/home/ubuntu/n8n
ENVF=/home/ubuntu/hermes-bridge/.env
LOG=/home/ubuntu/hermes-bridge/edge-watchdog.log
COOLDOWN=/home/ubuntu/hermes-bridge/.edge-alert-cooldown
TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# Telegram alert with a 1h cooldown so a flapping edge can't spam the operator.
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

check() { curl -sS -o /dev/null -w "%{http_code}" --max-time 12 "$URL" 2>/dev/null; }

CODE=$(check)
[ "$CODE" = "200" ] && exit 0

# Retry to avoid acting on a transient blip (slow request, brief reload).
for i in 1 2; do
  sleep 8
  C=$(check)
  [ "$C" = "200" ] && exit 0
done

echo "$TS edge DOWN (last http=$CODE) -> docker compose up -d" >> "$LOG"
cd "$COMPOSE_DIR" && docker compose up -d >> "$LOG" 2>&1
RC=$?
echo "$TS recovery attempted (exit $RC)" >> "$LOG"
alert "⚠️ Hermes edge watchdog: the public edge was DOWN (http=${CODE}). Ran 'docker compose up -d' to recover (exit ${RC}). Inbound WhatsApp may have briefly dropped — please check the server."
