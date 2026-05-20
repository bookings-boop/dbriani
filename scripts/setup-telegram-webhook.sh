#!/usr/bin/env bash
# setup-telegram-webhook.sh
#
# Registers the Dubriani Telegram bot's webhook URL with Telegram.
# Run ONCE, after importing the Phase 1B workflow into N8N.
#
# Re-run is safe (idempotent).

set -euo pipefail

# Load .env if present
if [ -f "$(dirname "$0")/../.env" ]; then
    set -a
    # shellcheck disable=SC1091
    source "$(dirname "$0")/../.env"
    set +a
fi

if [ -z "${TELEGRAM_BOT_TOKEN:-}" ]; then
    echo "✗ TELEGRAM_BOT_TOKEN is not set. Add it to .env (see .env.example)." >&2
    exit 1
fi
: "${N8N_BASE_URL:=https://n8n.13-63-82-112.sslip.io}"

WEBHOOK_URL="${N8N_BASE_URL}/webhook/telegram"

echo "Registering Telegram webhook → $WEBHOOK_URL"
echo

response=$(curl -sS "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook?url=${WEBHOOK_URL}")
echo "$response"
echo

ok=$(echo "$response" | python3 -c "import sys, json; print(json.load(sys.stdin).get('ok'))" 2>/dev/null || echo "unknown")
if [ "$ok" = "True" ]; then
    echo "✓ Webhook set."
else
    echo "✗ Failed. See response above."
    exit 1
fi

echo
echo "Verifying with getWebhookInfo..."
echo
curl -sS "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getWebhookInfo" | python3 -m json.tool
