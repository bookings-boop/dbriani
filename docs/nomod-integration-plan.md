# Nomod Payment-Link Integration — Implementation Plan (Phase 1)

> **STATUS: PLAN ONLY — NOT APPROVED, NOT BUILT.** Research + design only.
> No code written, no API calls made to Nomod, nothing deployed.
> Awaiting operator review of the open decisions in §15.

> **For agentic workers:** once approved, implement task-by-task with
> `superpowers:executing-plans`. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Let Maria send Nomod payment links over WhatsApp once a booking is confirmed — every link drafted, reviewed, and approved by the operator (Zayn) before it reaches the customer. No autopilot in Phase 1.

**Architecture:** The `Claude AI` drafting node decides *when* a booking is ready to pay and emits structured payment fields. The n8n workflow branches to a bridge endpoint that creates the Nomod link (the bridge owns the API key and all money-safety guards). The payment message routes through the **existing** operator-approval card. A scheduled poll detects paid/expired links and notifies the operator — Nomod has no webhooks.

**Tech stack:** n8n (workflow `azPIy9OcDwiPV5uY`, "Dubriani Phase 1B", 98 nodes) · Hermes Bridge (`hermes-bridge/server.py`, stdlib Python, systemd `--user`, port 8788) · Postgres (`n8n` DB, role `hermes_rw`) · Nomod API (`https://api.nomod.com/v1`) · WAHA (WhatsApp) · Telegram bot (operator).

**Companion doc:** `docs/nomod-api-capabilities.md` — what Nomod's API actually supports.

---

## §0. Premise corrections — read before anything else

The task spec contains three assumptions that the codebase and the Nomod docs contradict. The plan below is built on the **corrected** premises. Each needs operator sign-off (§15, decisions #8–#10).

### C1 — The workflow does NOT use the bridge `/draft` endpoint
The spec says *"Bridge /draft response gains: should_send_payment, payment_amount…"*. It doesn't. The live workflow drafts replies with the **`Claude AI`** httpRequest node calling `api.anthropic.com` **directly** (model `claude-sonnet-4-6`); the bridge `/draft` endpoint is unused. This is the same premise error found and corrected in the customer-header feature (`docs/feature-header-plan.md`).

**Correction:** the payment-trigger fields are emitted in the **`Claude AI` node's JSON output** and read by the **`Parse Response`** node. The detection instructions go into the **embedded `systemPrompt` inside the `Build Prompt` Set node** (the live drafting prompt) — not the bridge's `system-prompt.md`. We mirror the change into `system-prompt.md` too, for consistency, but the live behaviour comes from `Build Prompt`.

### C2 — Nomod has NO webhooks
The spec's §5 asks for a `/nomod-webhook` bridge endpoint and signature verification. Nomod publishes no webhooks (`docs/nomod-api-capabilities.md` §9). Payment status must be **polled**.

**Correction:** drop `/nomod-webhook` and signature verification entirely. Add a **scheduled poll** — an n8n Schedule-Trigger workflow that calls a bridge `POST /payment-poll` endpoint every few minutes; the bridge queries `GET /v1/charges?link_id=…`, updates the `payments` table, and returns events for the operator to be notified about. **No new public/inbound endpoint is created** — a security improvement over the webhook design.

### C3 — A Nomod link's amount is immutable after creation
`PATCH /v1/links/{id}/edit` accepts only `status` and `title`. The spec's "Edit amount → re-generate link (old link auto-invalidated if Nomod supports it)" — Nomod **does** support invalidation (`PATCH status=disabled` or `DELETE …/delete`), but **not** amount editing.

**Correction:** "Edit amount" = disable the old link **and create a new one** (two API calls). Because Phase 1 is approval-gated, the operator always edits *before* the link is sent to the customer, so there is no risk of a customer holding a stale link. Documented as designed behaviour, not a residual.

---

## §1. Prerequisites

Verify all of these **before** Task 1:

1. **`NOMOD_API_KEY`** present in `~/hermes-bridge/.env` on EC2 (`13.63.82.112`). Confirm the bridge process can read it (`os.environ`) — the systemd unit loads `EnvironmentFile=%h/hermes-bridge/.env`. Confirm with: a one-off `GET /v1/currencies` call returns `200` and lists `AED`. *(This is the only Nomod call made before build — a read-only check, no money, no link created.)*
2. **Nomod account** is the intended production account; the business is `enabled` (a disabled business returns `business_not_enabled`).
3. **Postgres** reachable from the bridge via `docker exec n8n-postgres-1`; superuser `n8n` available for the `payments` table DDL (role `hermes_rw` has no DDL grant — same as `customer_facts`).
4. **`scripts/n8n_deploy.py`** safe-deploy path works (re-fetches `staticData` before PUT). All workflow edits go through it.
5. **Backups:** current workflow JSON exported (`workflows/phase-1b-telegram.json`), and `scripts/rollback_workflow.py` confirmed functional.
6. **Telegram admin chat id** `5532831477` confirmed for paid/expired notifications.
7. Confirm the **Nomod processing-fee rate** for the account and whether the customer or Dubriani absorbs it (`allow_service_fee`) — Q-E.
8. Decide the open questions in §15 — several gate the build.

---

## §2. Architecture (corrected)

### 2.1 Outbound — drafting a payment link

```
WAHA Webhook (customer WhatsApp msg)
   → … → Build Prompt (embedded systemPrompt + payment-detection rules)
   → Claude AI  ── emits JSON: messages[], …, should_send_payment, payment_* fields
   → Parse Response  ── extracts payment_* with safe defaults
   → Customer Facts (existing header feature)
   → ┌─ Check Payment Trigger (NEW — IF node) ─┐
      │ should_send_payment == true            │ false
      ▼                                        ▼
   Generate Payment Link (NEW httpRequest)   (normal draft flow)
      → POST bridge /payment-link               │
      → bridge: guards → Nomod POST /v1/links    │
                → write payments row             │
      → Build Payment Message (NEW code node)    │
      └──────────────┬───────────────────────────┘
                     ▼
            Queue & Format  ── prepends the 💳 payment header to the card
                     ▼
            Send Draft to Telegram  ── EXISTING operator-approval card
                     ▼
            operator approves → Send One to Customer (EXISTING)
```

The bridge owns the Nomod API key and every money-safety guard. n8n only ever talks to the bridge (via the existing `Hermes Bridge` credential) — it never holds the Nomod key. If `/payment-link` refuses (guard tripped, feature flag off, Nomod error), it returns `ok:false` + a reason; the workflow then **posts the normal text draft** with an operator note — the payment path never blocks a reply (same fail-safe principle as the customer-header feature).

### 2.2 Inbound — detecting that a link was paid (polling, no webhook)

```
Schedule Trigger (every N min)  ──  separate workflow "Nomod Payment Poll"
   → POST bridge /payment-poll
   → bridge: for each payments row status='created'
        GET /v1/charges?link_id=<id>  → classify paid | failed | expired
        update payments row
        return events[]
   → Loop events → Telegram (admin chat 5532831477)
        paid    → "✅ payment received" card
        expired → "⏳ link expired" card + retry suggestion
        failed  → "⚠️ payment failed" card + retry suggestion
```

### 2.3 Why bridge-side Nomod calls (deviation from spec §2)

The spec implies n8n calls Nomod directly. We recommend the **bridge** makes all Nomod calls because:
- The API key is **already** in `~/hermes-bridge/.env` — keep it there, off n8n.
- Every money guard (amount range, AED hard-code, dedup, per-day cap, deposit math) lives in **one** testable Python place, not split across n8n JS nodes.
- Reuses the bridge's existing fail-safe + `_psql` + unit-test patterns.
- No new public endpoint (polling is outbound from n8n).

Trade-off: more bridge code. For production money, centralised and unit-tested wins. **Needs sign-off (decision #9).**

---

## §3. Data model — `payments` table

DDL (run as superuser `n8n`, like `customer_facts`):

```sql
CREATE TABLE IF NOT EXISTS payments (
  id              serial PRIMARY KEY,
  customer_id     text        NOT NULL,        -- WAHA chat id (e.g. '...@lid')
  customer_name   text,
  nomod_link_id   text UNIQUE,                 -- Nomod link uuid
  nomod_charge_id text,                        -- set when paid (for refunds)
  link_url        text,
  amount_aed      numeric(12,2) NOT NULL,       -- the CHARGED amount (after deposit math)
  total_aed       numeric(12,2) NOT NULL,       -- full booking total
  payment_type    text NOT NULL,                -- 'full' | 'deposit_50'
  status          text NOT NULL DEFAULT 'created',
                  -- created | paid | failed | expired | disabled
  booking_summary text,
  created_at      timestamptz NOT NULL DEFAULT now(),
  approved_at     timestamptz,                  -- when operator hit Send
  paid_at         timestamptz,
  updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS payments_customer_idx ON payments (customer_id, status);
CREATE INDEX IF NOT EXISTS payments_open_idx     ON payments (status) WHERE status = 'created';
```

Notes:
- `amount_aed` vs `total_aed`: a 50% deposit charges `amount_aed` (= `total_aed`/2). The message template shows both.
- `status='disabled'` — set when the operator edits the amount (old link disabled, new row created).
- `nomod_link_id UNIQUE` — guards against a double-insert of the same link.
- Contains customer PII → table is **never** committed (consistent with the `staticData` rule).

---

## §4. Bridge changes (`hermes-bridge/server.py`)

All additive. New env vars (in `~/hermes-bridge/.env`, gitignored):

| Var | Default | Purpose |
|---|---|---|
| `NOMOD_API_KEY` | — | Nomod key (already set). |
| `NOMOD_API_BASE` | `https://api.nomod.com/v1` | Base URL. |
| `PAYMENTS_ENABLED` | `false` | **Master kill-switch.** Build/deploy "dark", flip on after review. |
| `NOMOD_MIN_AED` | `100` | Min charge amount (decision #6). |
| `NOMOD_MAX_AED` | `100000` | Max charge amount (decision #6). |
| `NOMOD_MAX_LINKS_PER_DAY` | `5` | Per-customer/day link cap (decision #5). |
| `NOMOD_LINK_EXPIRY_DAYS` | `7` | Nomod `expiry_date` horizon (decision #1). |
| `NOMOD_DEDUP_WINDOW_MIN` | `30` | Re-use an existing unpaid link created within N min. |

### 4.1 New helper functions (pure where possible — unit-tested)

- `nomod_request(method, path, body=None)` — wraps `urllib` calls to Nomod with `X-API-KEY`, 10s timeout, returns `(status, json, error)`. Exponential-backoff retry on `429`/`5xx` (3 tries). **No retry** on `4xx` other than `429`.
- `compute_charge_amount(total, payment_type)` — pure: `full` → `total`; `deposit_50` → `round(total/2, 2)`. **All money arithmetic server-side — never the LLM.**
- `validate_payment_amount(amount)` — pure: returns `(ok, reason)` against `NOMOD_MIN_AED`/`NOMOD_MAX_AED`.
- `payments_open_for(customer_id)` — SELECT rows `status='created'` for dedup + cap checks.
- `count_links_today(customer_id)` — COUNT rows created in last 24h.
- `insert_payment(...)`, `update_payment_status(link_id, ...)` — `payments` CRUD via `_psql`.
- `classify_charge(charge_json, link_json)` — pure: maps a Nomod charge/link state → `paid` | `failed` | `expired` | `created`. **Until the charge `status` enum is empirically confirmed (Q-C), any ambiguous "money present" state maps to a `needs_review` flag → operator, never auto-`paid`.**
- `build_payment_card_header(payment)` — pure: the `💳 PAYMENT LINK READY FOR REVIEW` header block (§8).

### 4.2 New endpoint — `POST /payment-link`

Request (from the `Generate Payment Link` node): `{customer_id, customer_name, total_aed, payment_type, booking_summary, yacht, date}`.

Logic (fail-safe — never raises to the caller):
1. If `PAYMENTS_ENABLED` is false → `{ok:false, reason:"payments disabled"}`.
2. `compute_charge_amount` → `validate_payment_amount`. Out of range → `{ok:false, reason:"amount AED <x> outside 100–100000 — manual review"}`.
3. **Dedup:** if `payments_open_for(customer_id)` has a row created within `NOMOD_DEDUP_WINDOW_MIN` → return that existing link, `{ok:true, reused:true, …}`.
4. **Per-day cap:** `count_links_today >= NOMOD_MAX_LINKS_PER_DAY` → `{ok:false, reason:"daily link cap reached"}`.
5. Build the Nomod body — `currency:"AED"` (hard-coded, never from input), one `items` entry (`name` = yacht + date, `amount` = charge amount string), `title` = `"Dubriani Yachts — <name>"`, `note` = `booking_summary`, `expiry_date` = `min(today + NOMOD_LINK_EXPIRY_DAYS, booking_date − 1d)`.
6. `nomod_request("POST", "/links", body)`. On non-201 → `{ok:false, reason:<mapped error>}`.
7. `insert_payment(...)` with `status='created'`.
8. Return `{ok:true, link_url, link_id, charge_amount, total_aed, payment_type, expires_at}`.
9. **Audit log:** every call (success or refusal) logged with timestamp, customer_id, amount, outcome.

### 4.3 New endpoint — `POST /payment-poll`

Called by the scheduled workflow. Logic:
1. SELECT all `payments` rows `status='created'`.
2. For each: `GET /v1/charges?link_id=<id>` (+ `GET /v1/links/<id>` for expiry/disable state).
3. `classify_charge` → if `paid`: set `status='paid'`, `paid_at`, `nomod_charge_id`. If `expired`: `status='expired'`. If `failed`: `status='failed'`. `needs_review` → leave `created`, flag for operator.
4. Return `{ok:true, events:[{customer_id, customer_name, status, amount_aed, link_url, needs_review}]}`.
5. On Nomod error for a row → skip it, leave `created`, retry next poll. Never crash the batch.

### 4.4 New endpoint — `POST /payment-disable` (supports "Edit amount" + "Skip")

`{link_id}` → `PATCH /v1/links/{id}/edit` `status=disabled`, set `payments.status='disabled'`. Used when the operator edits the amount (then `/payment-link` is called again) or cancels.

### 4.5 Register paths
Add `/payment-link`, `/payment-poll`, `/payment-disable` to the `do_POST` allowed-paths tuple and dispatch — all token-gated by `X-Bridge-Token` exactly like existing endpoints.

---

## §5. Hermes detection logic — when to trigger a payment

### 5.1 JSON output schema (emitted by the `Claude AI` node)
The drafting node already returns structured JSON. Add **optional** fields (all default to "no payment" if absent):

| Field | Type | Meaning |
|---|---|---|
| `should_send_payment` | bool | `true` only when **all** conditions in §5.2 hold. |
| `payment_total` | number | The **full** confirmed booking total in AED. (Bridge derives the charged amount.) |
| `payment_type` | string | `"full"` (default) or `"deposit_50"`. |
| `payment_description` | string | Booking summary for the Nomod `note` + the card. |
| `payment_intro` | string | Warm, lowercase opener for the WhatsApp message (Maria's voice). |
| `payment_urgency_note` | string | **Optional** urgency line — empty by default (see §7). |

`payment_currency` is **not** an LLM field — AED is hard-coded bridge-side.

### 5.2 Trigger conditions (all required)
`should_send_payment` is `true` **only when every one** of these is true:
- Yacht confirmed.
- Date **and** duration confirmed.
- Price confirmed **and the customer has acknowledged it**.
- Customer shows explicit booking intent ("book it", "let's do it", "I'm in", or clear equivalent).

When **any** is uncertain, `should_send_payment` stays `false` — a false positive wastes customer trust. The prompt instructs Maria to **prefer asking** *"would you like me to send the payment link?"* over silently triggering.

### 5.3 Where the instructions live
- **Primary:** the embedded `systemPrompt` string inside the **`Build Prompt`** Set node — appended after an existing stable anchor (same technique as `build_ask_name_prompt.py`). Deployed via a `scripts/build_*.py` script + `n8n_deploy.py`.
- **Mirror:** the same block added to `system-prompt.md` so the bridge's copy stays consistent (the bridge's `/improve`/`/learn` read it).

### 5.4 `Parse Response` change
Extend `Parse Response` to read the six fields with safe defaults (`should_send_payment` → `false`, numbers → `0`, strings → `""`). Defensive: a malformed/absent block must never break parsing of the normal `messages[]`.

---

## §6. Workflow changes

Built with `scripts/build_nomod_payment.py` (new), deployed via `n8n_deploy.py` safe-deploy. Two pieces:

### 6.1 Main workflow (`azPIy9OcDwiPV5uY`) — new nodes
1. **`Check Payment Trigger`** (IF) — after `Customer Facts`. Condition: `$json.should_send_payment === true`. True → payment branch; false → existing `Queue & Format` path unchanged.
2. **`Generate Payment Link`** (httpRequest) — `POST {bridge}/payment-link`, `Hermes Bridge` credential, body from the parsed payment fields + customer facts. `onError: continueRegularOutput` — a bridge failure must not kill the run.
3. **`Build Payment Message`** (code) — assembles the customer-facing WhatsApp text (§7) and the `💳` card header (§8). The **booking-summary block and amount are built deterministically here** from the bridge's returned `charge_amount`/`link_url` — so the amount shown always equals the link's amount. Only `payment_intro` and `payment_urgency_note` are LLM text.
4. Rewire into **`Queue & Format`**, which prepends the `💳` header to the operator card. As in the header feature, `Queue & Format` must resolve its draft data via explicit node references (`$('Parse Response')`), not `$input`, because the HTTP node sits in the chain.

### 6.2 New workflow — "Nomod Payment Poll"
A small separate workflow: **Schedule Trigger** (interval = decision #8) → `POST {bridge}/payment-poll` → **Split** events → per event, **Telegram sendMessage** to chat `5532831477` with a paid / expired / failed card. Separate workflow = isolated, easy to pause, no risk to the main draft path.

---

## §7. Payment message template

> **Per the operator's adjustment:** there is **no fixed "link valid for 24h" line.**
> Urgency/expiry framing is added by Hermes **only when context justifies it**;
> default = no urgency line. The Nomod link's real expiry (set by the bridge,
> `NOMOD_LINK_EXPIRY_DAYS`, e.g. 7 days) is **decoupled** from what Maria says —
> a genuine slow payer keeps a working link; urgency is a messaging choice, not
> a technical countdown.

### 7.1 Structure
`Build Payment Message` assembles, in order:
1. **`payment_intro`** — Hermes-authored, warm, lowercase (Maria's voice).
2. Blank line.
3. **Deterministic booking-summary block** — built by the node, not the LLM:

   *Full payment:*
   ```
   hi mark, here's everything for your charter 🛥️

   🛥️  Sunseeker Satoshi 70
   📅  Sat 14 Dec · 4 hours
   👥  6 guests
   💳  total: AED 12,000

   tap here to confirm your booking: <link>
   ```
   *50% deposit:*
   ```
   💳  deposit to secure: AED 6,000 (50% of AED 12,000)
   ```
4. **`payment_urgency_note`** — appended **only if non-empty**. Hermes decides per-message.

### 7.2 When Hermes ADDS an urgency line
Pattern-recognition — add only when the conversation shows:
- Customer hesitating across **>3 turns** without committing.
- **Same-day or next-day** booking (genuine time pressure).
- Customer mentions **comparing other options**.
- Booking date is **within 7 days**.
- A **price-haggler showing partial commitment**.

### 7.3 When Hermes does NOT add an urgency line (default)
- Customer is **enthusiastic and ready** — fake pressure cheapens trust.
- **VIP / returning** customer — push tactics insult them.
- **First-message booking** — clearly a hot lead, no push needed.
- Booking is **>14 days out** — no genuine urgency.

Example urgency lines (Hermes-voiced, not templated): *"this weekend's slots are going fast, so worth locking in soon"* · *"since it's for tomorrow i'd grab it now — this holds your slot."*

These ADD/DON'T-ADD rules go into the §5.3 system-prompt block.

---

## §8. Operator approval card

The payment draft routes through the **existing** approval card (`Send Draft to Telegram`), with a payment-specific header prepended:

```
💳 PAYMENT LINK READY FOR REVIEW
Customer: Mark
Amount: AED 6,000  (50% deposit of AED 12,000)
Booking: Sunseeker Satoshi 70 · Sat 14 Dec
Link: created · expires 29 May
──────────────
<WhatsApp message draft below>
```

Operator actions:
| Button | Behaviour |
|---|---|
| **Send** | Existing send path → `Send One to Customer`; set `payments.approved_at`. |
| **Edit message** | Existing refine/edit-prompt flow — re-draft the text, **link unchanged**. |
| **Edit amount** | Prompt the operator for a new amount → `POST /payment-disable` (old link) → `POST /payment-link` (new amount) → re-render the card. New `payments` row; old row → `disabled`. |
| **Skip** | `POST /payment-disable` (archive the unused link) → conversation continues **text-only**; no link sent. |

"Edit amount" is the most complex addition — a new callback action in `Parse Callback` / `Route Action` plus an operator-input capture (reuse the existing "Edit Prompt" text-capture pattern). The operator card showing the amount in plain sight is the **primary safety net** against an LLM-mis-stated amount.

---

## §9. Safety guards (production money)

| Guard | Where | Behaviour |
|---|---|---|
| **Master kill-switch** | `PAYMENTS_ENABLED` env | Off → `/payment-link` refuses; deploy dark, flip on after review. |
| **Amount range** | `validate_payment_amount` | Charge < `NOMOD_MIN_AED` (100) or > `NOMOD_MAX_AED` (100000) → refuse, route to manual review. |
| **Currency hard-coded** | `/payment-link` | `"AED"` set server-side; the LLM **cannot** influence currency. |
| **No LLM arithmetic** | `compute_charge_amount` | LLM emits the full total; the bridge computes deposit/charge amounts. |
| **Duplicate prevention** | `payments_open_for` + dedup window | An unpaid `created` link for the customer within `NOMOD_DEDUP_WINDOW_MIN` → reuse it, don't create a second. Substitutes for Nomod's missing idempotency key. |
| **Per-day cap** | `count_links_today` | > `NOMOD_MAX_LINKS_PER_DAY` (5) links/customer/day → refuse. |
| **Operator approval** | existing card | **Every** link reviewed before send — Phase 1 has no autopilot. The card surfaces the amount as the human check. |
| **Audit log** | bridge log | Every create / approve / disable / poll-result logged with timestamp + amount + customer_id. |
| **Fail-to-human** | `classify_charge`, all endpoints | Any ambiguity (unknown charge status, Nomod error) → route to the operator; never auto-confirm a payment or auto-block a draft. |
| **Key isolation** | bridge-only | `NOMOD_API_KEY` only in `~/hermes-bridge/.env`; never in n8n, never committed. |

---

## §10. Phase 2 — autopilot (specified, NOT built)

Phase 2 would let Maria send a payment link **without** per-link operator approval. Gate it behind **all** of:
- **≥ 50 approval-mode links sent** with **0 wrong-amount incidents** and a healthy paid-conversion rate.
- A dedicated **payment cap** (separate from the message caps): max N auto-sent links/day, max M AED auto-authorised per link without review (anything above → forced approval).
- **Break conditions** specific to payments: any refund, any customer dispute/"this is wrong" message, any `invalid_amount`/Nomod error, or amount > the auto threshold → instantly drop that conversation to approval mode.
- Operator can flip payments autopilot on/off independently of message autopilot (`/caps`-style command).
- The amount must still be a **conversation-confirmed** figure; autopilot removes the *approval click*, never the *amount verification* — so an amount-confidence check stays mandatory.

Phase 2 is out of scope here and must be its own brainstorm → spec → plan cycle.

---

## §11. Risks specific to production money

| # | Risk | Likelihood | Mitigation |
|---|---|---|---|
| R1 | LLM states the **wrong amount** (mishears/miscalculates the quote) | Medium | No LLM arithmetic (§9); amount range guard; **operator card shows the amount** — the human verifies vs the actual quote; Phase 1 is 100% approval-gated. |
| R2 | **No sandbox** → first real test moves real money | High (certain) | §13 testing plan: link *creation* is free; one paid test uses a small amount + operator's own card + dashboard refund. |
| R3 | **Polling lag** — a paying customer waits up to one poll interval for the operator's "paid" confirmation | Medium | Short interval (decision #8, recommend 3 min); the customer still gets Nomod's own on-page success screen instantly. |
| R4 | **Duplicate links** — two drafts in flight create two links (no Nomod idempotency key) | Low–Medium | Dedup window in `/payment-link`; `nomod_link_id UNIQUE`; n8n node retry disabled. |
| R5 | **Stale link after "Edit amount"** | Low | Old link `PATCH disabled` before the new one is created; Phase 1 always edits *before* send, so the customer never sees the old link. |
| R6 | **API key compromise** → attacker creates links on the account | Low | Key only in bridge `.env`, gitignored, bridge not publicly reachable; rotate via Nomod app if suspected; `401` handling halts the feature. |
| R7 | **Unknown rate limits** → `429` under load | Low | Backoff+jitter retry; low volume in Phase 1 (approval-gated). |
| R8 | **Charge `status` enum unconfirmed** → misclassifying paid/unpaid | Medium | `classify_charge` fails to `needs_review` → operator; confirm the enum empirically before go-live (Q-C). |
| R9 | **False-positive trigger** — link sent when the booking isn't really confirmed | Medium | Strict §5.2 conditions; "prefer to ask" instruction; operator **Skip** discards the link. |
| R10 | Customer pays, then disputes the booking terms | Low | Refunds via Nomod dashboard (Phase 1); `booking_summary` stored on the `payments` row as the record of what was sold. |

---

## §12. Rollback plan (per build step)

Every change is **additive** and independently reversible:

| Build step | Rollback |
|---|---|
| `payments` table | `DROP TABLE payments;` — no other consumer reads it. |
| Bridge endpoints/helpers | Revert `server.py` to the pre-feature commit, `systemctl --user restart hermes-bridge`. Endpoints are new paths — removing them affects nothing else. |
| `PAYMENTS_ENABLED` flag | Set `false` → feature instantly inert even with all code deployed (fastest rollback; no redeploy). |
| `Build Prompt` systemPrompt edit | Re-run a revert build script restoring the prior `systemPrompt`; redeploy. |
| `Parse Response` change | Fields are optional with defaults — reverting just stops reading them. |
| Main-workflow nodes | Restore `workflows/phase-1b-telegram.json` from backup via `scripts/rollback_workflow.py`. `Check Payment Trigger` defaults false → if anything is wrong the payment branch is simply never taken. |
| "Nomod Payment Poll" workflow | Deactivate it (separate workflow — zero impact on the main flow). |

**Safest single lever:** `PAYMENTS_ENABLED=false` disables the whole feature without touching code or workflow.

---

## §13. Testing strategy (no sandbox)

Nomod has no sandbox — tests run against production. But **creating a link costs nothing and moves no money**; only a *completed card payment* incurs a fee. Test in this order:

1. **Prereq check** — `GET /v1/currencies` returns 200, lists AED. *(read-only, no money)*
2. **Bridge unit tests** — `compute_charge_amount`, `validate_payment_amount`, `classify_charge`, header builder — pure functions, `test_nomod.py` (mirrors `test_customer_facts.py`). *(no network)*
3. **Link-creation test (free)** — with `PAYMENTS_ENABLED=true`, drive a confirmed-booking conversation through to `Check Payment Trigger`; confirm a real Nomod link is created, the `payments` row is written, and the `💳` card renders. **Do not pay the link.** Then `Skip` → confirm the link is disabled. *(creates real links, no money)*
4. **Guard tests** — amount below 100 / above 100000 → refused; 6th link in a day → refused; rapid double-trigger → second call reuses the first link. *(free)*
5. **One end-to-end paid test** — temporarily lower `NOMOD_MIN_AED`, create a small-amount link, pay it with the **operator's own card**, confirm the poll flips the row to `paid` and the Telegram "paid" card fires. **Record the exact charge `status` string returned** (resolves Q-C). Then **refund** via the Nomod dashboard. *(moves a small real amount, refunded)*
6. **Failure-path test** — stop the bridge mid-draft → confirm the workflow still posts the normal text draft (payment path fails safe).
7. **Restore** `NOMOD_MIN_AED` to 100 after step 5.

No paid test until steps 1–4 pass and the operator explicitly approves step 5.

---

## §14. Build task breakdown & time estimate

Phase 1, approval-gated only. Standard deploy pattern (backup → dry-run → deploy → verify) each step.

| Task | Scope | Est. |
|---|---|---|
| 1 | `payments` table DDL + verify | 0.5 h |
| 2 | Bridge: helpers + pure functions + `test_nomod.py` unit tests | 3.0 h |
| 3 | Bridge: `POST /payment-link` (guards, Nomod create, audit) | 2.0 h |
| 4 | Bridge: `POST /payment-poll` + `POST /payment-disable` | 2.0 h |
| 5 | Hermes detection: `Build Prompt` systemPrompt + `system-prompt.md` mirror | 1.5 h |
| 6 | `Parse Response`: extract payment fields (safe defaults) | 0.5 h |
| 7 | Main workflow: `Check Payment Trigger`, `Generate Payment Link`, `Build Payment Message`, `Queue & Format` rewire | 2.5 h |
| 8 | Operator card: `💳` header + "Edit amount" callback | 2.5 h |
| 9 | "Nomod Payment Poll" workflow (Schedule Trigger → poll → Telegram) | 2.0 h |
| 10 | Deploy, dark-launch, testing §13 steps 1–6, doc updates | 3.0 h |
| — | Contingency (SSH flakiness, prod-test care) | 1.5 h |
| | **Total** | **≈ 21 h — ~3 working days** |

Each task is committed separately. Recommend a **dark launch**: deploy everything with `PAYMENTS_ENABLED=false`, then run testing §13 with it flipped on only in a controlled window.

---

## §15. Operator decisions needed before build

The build should not start until these are answered. Recommendations are given; the operator confirms or overrides.

**From the original spec:**

1. **Nomod link expiry.** *Recommend:* `expiry_date` = `min(today + 7 days, booking_date − 1 day)` (env `NOMOD_LINK_EXPIRY_DAYS=7`). Decoupled from what Maria mentions to the customer. → confirm 7 days?
2. **Discount label trigger.** *Recommend:* use Nomod `discount_percentage` **only when an explicit discount was agreed** in the conversation — never auto-label any below-list price as a "special offer" ("list price" is fuzzy for charters; auto-discounting cheapens the brand). → agree?
3. **Link expires unpaid — auto-follow-up or wait?** *Recommend Phase 1:* the poll notifies the operator; **Maria does not auto-follow-up** (auto-follow-up is Phase-2 autopilot behaviour). Operator decides whether to re-send. → agree?
4. **Sandbox testing.** There is **no sandbox.** *Recommend:* test link-creation freely against production (free); one end-to-end paid test with a small amount on the operator's own card, refunded via dashboard (§13 step 5). → approve this approach?
5. **Per-customer max links/day.** *Recommend:* 5, env-configurable. → 5 ok?
6. **Min/max amount sanity check.** *Recommend:* 100 AED min, 100,000 AED max, applied to the **final charged** amount (after deposit math). → confirm both numbers?
7. **Refunds.** *Recommend Phase 1:* dashboard only; no `/refund` command. The API supports refunds, so a `/refund` operator command is a clean Phase-2 add. → agree?

**New decisions surfaced by research (§0 premise corrections):**

8. **Webhook → polling.** Nomod has no webhooks. *Recommend:* a scheduled poll every **3 minutes**. The operator's "payment received" confirmation can therefore lag up to ~3 min (the customer still sees Nomod's instant on-page success screen). → accept the polling model and a 3-min interval?
9. **Bridge-side Nomod calls.** *Recommend:* the bridge makes all Nomod API calls (key stays in `~/hermes-bridge/.env`, all money guards in one tested place) — a deviation from the spec's implied "n8n calls Nomod directly". → approve bridge-side?
10. **Charge status enum.** The exact Nomod charge `status` strings for a completed payment are not cleanly documented. *Plan:* confirm empirically during §13 step 5; until then `classify_charge` fails any ambiguous state to the operator. → acknowledge this is a verify-during-test item, not a blocker?

**Also worth a steer (not blocking):**
- **Tabby / Tamara BNPL** (`allow_tabby`/`allow_tamara`, default `true`): should buy-now-pay-later be offered on Dubriani charter links? *Recommend:* operator's call — leave default (`true`) unless the operator wants card-only.
- **`allow_service_fee`** — does the customer see Nomod's service fee added, or does Dubriani absorb it? Tied to prereq Q-E (confirm the account's fee rate).

---

## §16. Explicitly NOT touched

Per the task constraints, this plan does not modify: the FR-5 improver, autonomous mode, the customer-context-header feature, or the `/lead` / `/send` / `/caps` / `/manual` commands. The payment path is a new branch off `Check Payment Trigger`; when `should_send_payment` is false (the default), every existing flow behaves exactly as today.
