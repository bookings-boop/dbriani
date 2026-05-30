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
# Logs to edge-watchdog.log.
URL="https://n8n.13-63-82-112.sslip.io/healthz"
COMPOSE_DIR=/home/ubuntu/n8n
LOG=/home/ubuntu/hermes-bridge/edge-watchdog.log
TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)

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
echo "$TS recovery attempted (exit $?)" >> "$LOG"
