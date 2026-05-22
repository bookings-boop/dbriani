# Secret Rotation Runbook

Two secrets were exposed during the build and should be rotated: the **n8n
API key** and the **Telegram bot token**. Rotation is operator-driven — only
the operator can mint / revoke these. Secret **values** go straight into the
`.env` files — never into chat or git. All `.env` files are gitignored.

---

## 1. n8n API key — `N8N_API_KEY`

**Used by:** the local deploy / inspection scripts (`scripts/n8n_deploy.py`
and the `build_*.py` / `*_check.py` scripts) — they read `N8N_API_KEY` from
the repo's local `.env`.

**Rotate:**
1. n8n UI → **Settings → n8n API** → **Create an API key** → copy it.
2. **Delete the old key** in the same screen — this invalidates it.
3. Edit the local repo `.env`
   (`/Users/macbook/projects/dubriani-ai-agent/.env`):
   `N8N_API_KEY=<new key>`
4. Verify: `python3 scripts/n8n_deploy.py` — the read-only self-test should
   resolve the API and read the live workflow.

No service restart needed — this key is only used by local tooling.

---

## 2. Telegram bot token — `dubriani_hermes_bot`

**Used in two places on the EC2 box:**
- `~/n8n/.env` → `TELEGRAM_BOT_TOKEN` — the n8n workflow's
  `$env.TELEGRAM_BOT_TOKEN` (every Telegram send/edit in the workflow).
- `~/hermes-bridge/.env` → `ADMIN_TG_TOKEN` — the bridge cron scripts
  (daily summary, reminders).

If both reference the **same bot**, it is one token in two files — update
both.

**Rotate:**
1. Telegram → **@BotFather** → `/revoke` → select `dubriani_hermes_bot` → it
   issues a **new token** (the old one dies immediately).
2. On the box, update both env files:
   - `~/n8n/.env` — `TELEGRAM_BOT_TOKEN=<new>`
   - `~/hermes-bridge/.env` — `ADMIN_TG_TOKEN=<new>` (if the same bot)
3. Recreate the n8n container so it picks up the new env:
   `cd ~/n8n && docker compose up -d`
4. Restart the bridge: `export XDG_RUNTIME_DIR=/run/user/$(id -u);
   systemctl --user restart hermes-bridge`
5. Verify: send a command from the operator Telegram (e.g. `/caps`) — a reply
   should come back, and the next draft card should still post.

⚠️ The old token dies the instant you `/revoke` — the bot is down for the
~1–2 minutes until steps 2–4 finish. Rotate during a quiet window.

---

## Also worth rotating (if ever exposed)

`BRIDGE_TOKEN` (`~/hermes-bridge/.env`) · the WAHA API key · the Anthropic API
key. Out of scope for this rotation, but the same principle applies: new value
into the `.env`, restart the consumer, verify.
