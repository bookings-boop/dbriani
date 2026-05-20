# Dubriani AI Agent — Handoff to Claude Code

**Last updated:** 2026-05-20 — Phase 1B (Telegram approval bot) ready to deploy.

> **Secrets redacted 2026-05-20** for Git. Real values live in `.env` (gitignored) and N8N credentials — see `.env.example` and `docs/credentials-map.md`. Repo layout: see `README.md`.

**Audience:** Claude Code running locally on Zayn's MacBook Pro, picking this project up to deliver steps 0–4 of the execution order.

**Operating mode:** Build via terminal. Edit/generate exportable N8N workflow JSON. Commit to Git. Do NOT click through the N8N GUI. Where you cannot complete an action without the user (OAuth, QR scans, button clicks, webhook activations), prepare the artifact and instruct Zayn precisely.

**User preferences:** Be a ruthless mentor. Stress-test ideas. Don't ship trash. Call out risks loudly.

---

## 0. tl;dr

**Goal:** WhatsApp + Gmail AI agent for Dubriani Yachts (Dubai luxury yacht charters). Customer message → Claude drafts grounded reply → Zayn approves via **Telegram bot with inline buttons** → reply sent → logged in HubSpot.

**Current state:**
- WAHA paired to Dubriani number `971589502303@c.us`
- 14-node Phase 1A workflow live (WhatsApp admin approval)
- Anthropic key rotated 2026-05-20
- Supermemory connected to Zayn's Google Drive knowledge folder, auto-syncing
- Telegram bot created and ready to wire in

**Immediate scope — steps 0–4 only. Do not exceed.**

| # | Item | Done? |
|---|------|-------|
| 0 | Rotate Anthropic API key | ✅ |
| 1 | Set up Git repo with `.env` externalization | ⏳ |
| 2 | Verify Supermemory contents + tag schema | ⏳ |
| 3 | Tighten Supermemory search with `containerTags` | ⏳ |
| 4 | **Migrate approval to Telegram bot (MVP: Send / Skip / Edit / Regen)** | ⏳ |

Everything beyond step 4 (HubSpot, Postgres, Gmail, weekly distillation, Redis migration, full 7-button UI) is deferred until 0–4 are validated on real customer messages for ~1 week.

---

## 1. Project context

Dubriani Charters Leisure Yachts and Boats Rental L.L.C., Dubai. Luxury yacht charters + Doha ops + chauffeur + multi-day itineraries + premium catering + special occasions.

**Conversion data (drives prompt + flow design):**
- 2.88% overall chat-to-paid (426/14,804)
- VIP chats convert 22.4% (15× more valuable)
- Top loss reason: ghost after price (14.1% of all chats)
- Reply <15min → 1.13%, 1–6hr → 0.55%, 24hr+ → 0%
- 88% paid via non-card (USDT, cash, bank, 50/50)

**Build philosophy:**
- Approval-first. Zero autonomous sends ever.
- Never invent prices, availability, specs, policies.
- Multi-message bursts > single paragraph.
- Mirror cultural micro-signals.

---

## 2. Infrastructure (running on EC2)

| Component | Detail | Status |
|---|---|---|
| EC2 Ubuntu | IP 13.63.82.112 | Running |
| Docker | Hosts all services | Running |
| N8N | `https://n8n.13-63-82-112.sslip.io` | Running |
| WAHA | `https://waha.13-63-82-112.sslip.io` | Running, paired correctly |
| PostgreSQL | inside docker | Running, unused yet |
| Redis | inside docker | Running, unused yet |
| Caddy | terminates SSL | Running |
| Google Drive (knowledge) | folder `1Z5_we30q5QzB_hfN9YugX55ZXp4cNKIX` | Connected to Supermemory |
| Telegram bot | created via @BotFather | Webhook not yet set |

---

## 3. Credentials, URLs, IDs

⚠️ **Secrets below. Move to `.env` before any `git commit`.** Replace inline values in workflow JSON with `={{ $env.VAR_NAME }}` before commit.

### 3.1 WAHA

| Field | Value |
|---|---|
| Base URL | `https://waha.13-63-82-112.sslip.io` |
| API key (`X-Api-Key` header) | `<see .env: WAHA_API_KEY>` |
| Session | `default` |
| Paired number | `971589502303@c.us` |
| Send-text | `POST /api/sendText` body `{session, chatId, text}` |
| Session status | `GET /api/sessions/default` |
| Webhook receiver (N8N) | `https://n8n.13-63-82-112.sslip.io/webhook/whatsapp` |

**WAHA inbound payload shape** (verified):
```json
{
  "body": {
    "event": "message",
    "session": "default",
    "me": { "id": "971589502303@c.us" },
    "payload": {
      "from": "<sender>@c.us",
      "fromMe": false,
      "body": "<message text>",
      "hasMedia": false
    }
  }
}
```

Key paths in N8N expressions:
- Text: `$('Webhook').item.json.body.payload.body`
- Phone: `$('Webhook').item.json.body.payload.from`
- Name: `$('Webhook').item.json.body.payload.notifyName || $('Webhook').item.json.body.payload.pushName || ''`

### 3.2 Anthropic API ✅ ROTATED 2026-05-20

| Field | Value |
|---|---|
| Endpoint | `https://api.anthropic.com/v1/messages` |
| API key | `<see .env: ANTHROPIC_API_KEY>` |
| Version | `anthropic-version: 2023-06-01` |
| Model | `claude-sonnet-4-6` |
| Prompt caching | `system: [{type, text, cache_control: {type: "ephemeral"}}]` |

⚠️ **Key has been leaked twice in chat.** Going forward, Zayn pastes directly into local `.env`, never into chat. When regenerating workflow JSON, use `={{ $env.ANTHROPIC_API_KEY }}` and let Zayn populate `.env`.

### 3.3 Supermemory

| Field | Value |
|---|---|
| Search | `POST https://api.supermemory.ai/v3/search` |
| Add | `POST https://api.supermemory.ai/v3/add` |
| Auth | `Authorization: Bearer <token>` |
| N8N credential ID (in workflow) | `aKreN1AB4QYfQKlz` |
| N8N credential name | `"Bearer Auth account"` (httpBearerAuth) |
| API token | **Zayn to retrieve from N8N → Credentials → Bearer Auth account → reveal, then paste into `.env` as `SUPERMEMORY_API_KEY`.** |

**Google Drive sync:** folder `https://drive.google.com/drive/folders/1Z5_we30q5QzB_hfN9YugX55ZXp4cNKIX` connected via Supermemory's Google Drive connector. Auto-syncing.

**Verification (step 2):** Run `scripts/verify-supermemory.py` (provided). It:
1. Hits `/v3/search` with `q="dubriani yacht"` no tags → prints results
2. Hits again with `containerTags: ["dubriani"]` → prints results
3. Compares — tells you whether ingestion is using your expected tags

**Target tag schema:**

| Content | Tags |
|---|---|
| Examples | `["dubriani", "examples"]` |
| Rules | `["dubriani", "rules"]` |
| Yachts | `["dubriani", "yachts"]` |
| Catering | `["dubriani", "catering"]` |
| Per-customer | `["dubriani", "customer:971XXX"]` |
| Corrections | `["dubriani", "corrections"]` |

### 3.4 Telegram Bot ✅ CREATED 2026-05-20

| Field | Value |
|---|---|
| Bot token | `<see .env: TELEGRAM_BOT_TOKEN>` |
| Admin user ID | `5532831477` |
| API base | `https://api.telegram.org/bot<TOKEN>/` |
| N8N webhook path | `/webhook/telegram` |

**One-time webhook setup (run once after workflow imported):**

```bash
# TELEGRAM_BOT_TOKEN is sourced from .env — see scripts/setup-telegram-webhook.sh
curl "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook?url=https://n8n.13-63-82-112.sslip.io/webhook/telegram"
```

Verify:
```bash
curl "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getWebhookInfo"
```

**Security:** Every Telegram update MUST verify `from.id == 5532831477`. Already enforced in Verify Admin node. Do not weaken.

### 3.5 HubSpot

Not yet connected. Deferred to step 5 (post-MVP).

### 3.6 Gmail

Not yet connected. Deferred to step 10.

### 3.7 Constants

- N8N base URL: `https://n8n.13-63-82-112.sslip.io`
- WAHA webhook path: `/webhook/whatsapp`
- Telegram webhook path: `/webhook/telegram`
- Zayn (admin name): "Zayn"
- Agent persona: "Maria"

---

## 4. The system prompt

**File:** `dubriani-system-prompt-v2.md` (~27 KB). Embedded as `systemPrompt` Set node field in workflow.

**Design:**
1. Draft-approval mode (top of prompt).
2. Strict JSON output: `{ "messages": [...], "notes_for_zayn": "..." }`.
3. `messages` array supports multi-message; currently only `[0]` is sent.
4. Hard pricing floors (Satoshi B2C morning AED 1,500/hr).
5. Hard stops flagged in notes, not auto-handled (Doha/chauffeur pricing, retired yachts, refunds, multi-day quotes, B2B, angry customers).

**Editing:** modify the `.md` file, regenerate workflow via `scripts/build_workflow.py`. Never hand-edit the inline `systemPrompt` value inside JSON.

---

## 5. Phase 1B workflow (this delivery)

**File:** `dubriani-workflow-PHASE-1B.json`.
**Replaces:** Phase 1A (WhatsApp admin approval).
**Two webhook triggers, one workflow** (shared static data).

### Customer branch (WAHA → Telegram)
```
WAHA Webhook → Filter Inbound → Supermemory → Build Prompt → Claude AI →
Parse Response → Queue Draft → Send Draft to Telegram (4 inline buttons)
```

### Telegram branch (button presses + edit replies)
```
Telegram Webhook → Verify Admin → Switch Update Type:
  ├─ callback_query → Parse Callback → Answer Callback → Switch on Action:
  │     ├─ send  → Lookup Draft → Send to Customer (WAHA) → Edit Telegram Msg
  │     ├─ skip  → Mark Skipped → Edit Telegram Msg
  │     ├─ edit  → Set Awaiting → Edit Telegram Msg → Send "type your edit"
  │     └─ regen → Lookup Draft → Re-call Claude → Parse → Edit Telegram Msg
  └─ message → Find Awaiting Draft → Send Admin's Text to Customer → Confirm
```

### Pending queue entry shape

```json
{
  "id": "<ts>_<random>",
  "customer_phone": "971XXX@c.us",
  "customer_name": "...",
  "customer_message": "...",
  "memory_context": "<json string>",
  "draft_text": "...",
  "notes": "...",
  "telegram_message_id": 123,
  "telegram_chat_id": 5532831477,
  "status": "pending | awaiting_edit | sent | skipped"
}
```

### The 4 MVP buttons

| Button | callback_data | Action |
|---|---|---|
| ✅ Send | `send:<draft_id>` | Forward draft to customer; update Telegram |
| ✏️ Edit | `edit:<draft_id>` | Set status=awaiting_edit; ask for text |
| 🔁 Regen | `regen:<draft_id>` | Re-call Claude with hint; replace Telegram msg |
| ❌ Skip | `skip:<draft_id>` | Mark skipped; update Telegram |

Deferred to step 9: Payment link, Call instead, Snooze, A/B/C tone variants.

---

## 6. Execution detail — steps 0–4

### Step 0: Anthropic key rotation ✅ DONE
New key in §3.2. Move to `.env`.

### Step 1: Git repo

```bash
mkdir -p ~/projects/dubriani-ai-agent && cd ~/projects/dubriani-ai-agent
git init
```

Structure:
```
dubriani-ai-agent/
├── .env                              # secrets, gitignored
├── .env.example                      # template, committed
├── .gitignore
├── README.md
├── HANDOFF.md
├── system-prompt.md
├── workflows/
│   ├── phase-1a-archived.json
│   └── phase-1b-telegram.json
├── scripts/
│   ├── build_workflow.py
│   ├── verify-supermemory.py
│   └── setup-telegram-webhook.sh
└── docs/
    ├── credentials-map.md
    └── test-cases.md
```

`.gitignore`:
```
.env
.DS_Store
*.log
node_modules/
__pycache__/
*.pyc
.venv/
```

### Step 2: Verify Supermemory

Run `python scripts/verify-supermemory.py` after setting `SUPERMEMORY_API_KEY` in `.env`. Interpret output:
- Both searches empty → ingestion not complete or auth wrong. Check Supermemory dashboard.
- Untagged returns Dubriani content, tagged doesn't → tag mismatch. Find what tag was used. Adjust workflow.
- Both return Dubriani content → done. Use `["dubriani"]` in workflow.

### Step 3: Tighten search

Edit Supermemory node in workflow body:
```
={
  "q": {{ JSON.stringify($json.body.payload.body) }},
  "containerTags": ["dubriani"],
  "limit": 5
}
```

(Adjust tag if step 2 revealed a different one.)

### Step 4: Deploy Phase 1B ⭐ MAIN BUILD

Sequence:

1. **Set Telegram webhook** (§3.4 curl).
2. **Disable Phase 1A workflow** in N8N (keep, don't delete — fallback).
3. **Import `dubriani-workflow-PHASE-1B.json`**.
4. **Verify Supermemory credential** auto-linked. If not, re-pick.
5. **Activate** new workflow.
6. **Run test sequence:**

| Test | Action | Expected |
|---|---|---|
| T1: Send | Send "test send" from personal WA to Dubriani. Tap ✅ Send in Telegram. | Customer gets draft. Telegram msg updates to "✅ Sent at HH:MM". |
| T2: Skip | New customer msg. Tap ❌ Skip. | Customer gets nothing. Telegram updates to "❌ Skipped". |
| T3: Edit | New customer msg. Tap ✏️ Edit. Type your version. | Customer gets your version. Telegram confirms "✏️ Sent your version". |
| T4: Regen | New customer msg. Tap 🔁 Regen. | Telegram msg replaced with new draft + buttons. Test Send/Skip on regenerated. |
| T5: Multi-pending | Send 2 customer msgs fast. | 2 separate Telegram draft msgs, each independently actionable. |
| T6: Unauth | From a different Telegram user, message the bot. | Bot ignores. N8N execution shows Verify Admin dropped it. |

---

## 7. Pitfalls (DO NOT REPEAT)

1. **N8N expression engine has no `?.` (optional chaining).** Use `||` chains.
2. **`$json` = preceding node output, NOT webhook.** Use `$('Webhook').item.json...` explicitly.
3. **HTTP `jsonBody` `=` prefix marks whole field as expression.** Don't add another `=` inside string values.
4. **Anthropic `system` must be array of `{type, text, cache_control}` objects** for caching to work.
5. **Model: `claude-sonnet-4-6` (dateless format).**
6. **WAHA `payload.from` can be `@lid`** (linked-id business), not always `@c.us`. Don't assume.
7. **`notification_template` events have empty body.** Filter out.
8. **Claude wraps JSON in ` ```json ``` ` fences sometimes.** Strip defensively.
9. **WAHA chatId suffixes:** `@c.us` (personal/biz), `@lid` (linked-id biz), `@g.us` (groups). Phase 1 handles `@c.us` only.
10. **N8N IF v2:** boolean ops `{"type": "boolean", "operation": "false"}` test falsy.
11. **Verify WAHA pairing.** `curl $WAHA/api/sessions/default` — `me.id` should match Dubriani. Was wrong previously (paired to freelancer's phone).
12. **Sending to a chatId not on WhatsApp errors.** Confirm before depending on a number.
13. **N8N static workflow data only persists on successful execution.** Don't rely on for critical state at scale.
14. **Telegram `callback_data` ≤ 64 bytes.** Format `<action>:<draft_id>` fits.
15. **Always call `answerCallbackQuery` after button press**, else spinner persists on user's button.
16. **Bot is publicly addressable. ALWAYS verify `from.id == TELEGRAM_ADMIN_USER_ID`.**
17. **`editMessageText` requires `chat_id` AND `message_id`.** Store both in pending queue.

---

## 8. Files in this delivery

| File | Purpose |
|---|---|
| `HANDOFF.md` | This file. Read first. |
| `dubriani-system-prompt-v2.md` | System prompt source |
| `dubriani-workflow-PHASE-1A.json` | Archived (previous) |
| `dubriani-workflow-PHASE-1B.json` | **Main deliverable** — Telegram approval |
| `.env.example` | Env var template |
| `verify-supermemory.py` | Script to verify SM state |
| `setup-telegram-webhook.sh` | One-shot curl for Telegram webhook |
| `Dubriani-Scope-of-Work-v2.docx` | Original v2 scope |

---

## 9. Recommended first prompt to Claude Code

```
Read HANDOFF.md, dubriani-system-prompt-v2.md, and dubriani-workflow-PHASE-1B.json in this folder. Confirm you've understood:
1. Current state (Phase 1A live, Phase 1B ready to deploy)
2. Execution order — steps 0-4 ONLY
3. Pitfalls in §7

Then propose:
a) Risks you see beyond what's flagged
b) Exact action sequence for steps 1-4, separating "you do" vs "Zayn does"
c) Clarifying questions

Do not start building until I approve.
```

---

## 10. Stress-test of v2 scope (carry forward)

Concerns with v2 itself:

1. **7-button + 4-edit-sub-option UI is over-scoped for v1.** Phase 1B ships 4 buttons deliberately. Add the rest after a week of validation.
2. **"Confidence: 92%" in draft preview is fake** unless built from a real signal. Decision deferred.
3. **A/B/C tone-variant buttons = same prompt + suffix.** Cheap to add but each = another Claude call. Track usage.
4. **"Reply within 5–10 min" is a HUMAN constraint.** Bot drafts instantly but doesn't send until Zayn approves. Backup approver is Phase 2.
5. **Pending queue in static data is concurrency-fragile.** Redis migration = step 12.
6. **WAHA banning risk.** Approval-first is the protection.
7. **Telegram bot security:** Verify Admin node is critical. Do not remove.

---

— Handoff updated 2026-05-20 by Claude (web). Steps 0–4 ready to deploy.
