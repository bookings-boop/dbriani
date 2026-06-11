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

# secret_token (2026-06-11): Telegram echoes this as the
# X-Telegram-Bot-Api-Secret-Token header on every delivery; the n8n Verify
# Admin node rejects any update whose header doesn't match. MUST be included
# on every setWebhook or it is reset to empty (stripping auth). Read from
# TELEGRAM_WEBHOOK_SECRET; POST it so the secret never lands in the URL /
# shell history / logs. Warn (don't fail) if unset so a first bring-up
# before the secret exists still works (from.id gates meanwhile).
if [ -n "${TELEGRAM_WEBHOOK_SECRET:-}" ]; then
    response=$(curl -sS -X POST \
        --data-urlencode "url=${WEBHOOK_URL}" \
        --data-urlencode "secret_token=${TELEGRAM_WEBHOOK_SECRET}" \
        "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook")
else
    echo "⚠ TELEGRAM_WEBHOOK_SECRET not set — registering WITHOUT a secret (forgeable; set it and re-run)." >&2
    response=$(curl -sS "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook?url=${WEBHOOK_URL}")
fi
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
