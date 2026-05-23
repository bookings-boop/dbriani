# Proactive Follow-up Engine — Implementation Plan (7b)

> ⚠️ **SUPERSEDED — 2026-05-23.** This plan has been merged with 7a
> and 7c into a single unified plan:
> **`docs/pipeline-review-plan.md`**. Kept here for reference only.
> Do not build from this file.

> **Status:** PLAN ONLY. No code written. No workflow changes deployed.
> Built artifacts will follow the standard pattern (`scripts/build_*.py`,
> `safe_put` via `n8n_deploy.N8N`, bridge edits compile-checked then
> restarted, behavioural verify on test phone).

**Goal:** Detect conversations that have gone quiet at thresholds that
matter (HOT > 30 min, WARM > 2 h / > 24 h, COLD > 7 d, same-day > 15
min, booking-intent-no-payment > 24 h) and put a Hermes-drafted
follow-up message into the existing operator approval queue. The
operator approves / edits / skips exactly as for a normal customer
draft.

**Architecture (one paragraph):** A new `conversation_state` table
tracks last-customer-msg and last-operator-reply timestamps per
customer (populated by the live workflow as a side-effect of normal
sends). A new scheduled workflow runs every 15 min, calls
`POST /followup-scan` on the bridge, receives back the list of
eligible customers (label + trigger reason), then for each one calls
`POST /draft-followup`, posts the result to the operator chat with a
distinctive "PROACTIVE FOLLOW-UP" header on the approval card. The
existing Send / Auto / Edit / Skip buttons reuse the existing
`pendingQueue` path. A `follow_up_queue` table guards against
duplicates and auto-skips items the customer pre-empted by replying.

**Hard dependency:** the `customer_facts.label` column from plan 7a.
If 7b is built first, the plan ships with a fallback that treats every
customer as WARM and uses only the time-based thresholds (no HOT
30-min trigger).

**Build sequence:** 5 build scripts, ~3.5 h total.

---

## 1. Database changes

### 1a. `conversation_state` (NEW table)

Tracks per-customer timing so the scanner doesn't have to JOIN through
WAHA or scrape staticData.

```sql
CREATE TABLE conversation_state (
  customer_id              TEXT PRIMARY KEY,
  last_customer_message_at TIMESTAMPTZ,
  last_operator_reply_at   TIMESTAMPTZ,    -- approved sends, auto-sends, /send
  last_followup_at         TIMESTAMPTZ,    -- proactive follow-up sent (any outcome)
  reengage_attempts        INTEGER NOT NULL DEFAULT 0,
  updated_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_conv_state_last_cust  ON conversation_state (last_customer_message_at DESC);
CREATE INDEX idx_conv_state_last_op    ON conversation_state (last_operator_reply_at  DESC);
```

### 1b. `follow_up_queue` (NEW table)

```sql
CREATE TABLE follow_up_queue (
  id                SERIAL PRIMARY KEY,
  customer_id       TEXT NOT NULL,
  trigger_reason    TEXT NOT NULL,    -- 'hot_silent_30m', 'warm_silent_2h', 'warm_24h_reengage', 'cold_7d_reengage', 'sameday_15m', 'booking_no_payment_24h', 'manual:/reengage'
  scheduled_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  status            TEXT NOT NULL DEFAULT 'queued',
                    -- queued | drafted | approved | sent | skipped | expired | preempted | failed
  draft_message_id  TEXT,              -- pendingQueue draft id once posted
  reason_evidence   TEXT,              -- snapshot quote / signal
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_followup_customer       ON follow_up_queue (customer_id, status, created_at DESC);
CREATE INDEX idx_followup_status         ON follow_up_queue (status, scheduled_at);
CREATE UNIQUE INDEX idx_followup_one_per_day
  ON follow_up_queue (customer_id, DATE(created_at))
  WHERE status NOT IN ('skipped','preempted','expired');
```

The partial unique index enforces "max 1 active follow-up per
customer per 24 h" at the DB layer (defence-in-depth with the bridge's
own check).

### 1c. Grants

`hermes_rw` inherits via default privileges.

---

## 2. Bridge changes (`hermes-bridge/server.py`)

### 2a. New endpoint `POST /conversation-state` (writer)

Called by the workflow on every customer message AND every operator
send. Body:
```json
{ "customer_id":"…", "event":"customer_message" | "operator_reply" | "followup_sent" }
```
- `customer_message` → UPSERT, set `last_customer_message_at = now()`.
  Also: if `last_followup_at IS NOT NULL` and there's an OPEN
  `follow_up_queue` row for this customer (status in
  `queued | drafted`), `UPDATE ... SET status='preempted'` — the
  customer replied before approval.
- `operator_reply` → UPSERT, set `last_operator_reply_at = now()`,
  reset `reengage_attempts = 0`.
- `followup_sent` → UPSERT, set `last_followup_at = now()`,
  increment `reengage_attempts` IF trigger was a re-engage type.

Always 200, fail-open.

### 2b. New endpoint `POST /followup-scan` (scheduler call)

Runs every 15 min from the workflow. Body: `{}` (no args).

Algorithm:
1. Read all rows from `conversation_state` JOIN `customer_facts`
   (left join to tolerate missing labels — those default WARM in
   the fallback case).
2. For each row, evaluate triggers in priority order (return at most
   ONE trigger per customer per scan):

| Priority | Trigger key | Condition | Quiet hours apply? |
|---|---|---|---|
| 1 | `sameday_15m` | `customer_facts.dates ILIKE '%today%'` OR `'%tonight%'` OR matches today's date; `now() - last_customer_message_at > 15 min`; no operator reply since | No (urgent) |
| 2 | `hot_silent_30m` | `label='HOT'`; `now() - last_customer_message_at > 30 min`; `last_operator_reply_at IS NULL OR last_operator_reply_at < last_customer_message_at` | Yes |
| 3 | `booking_no_payment_24h` | Any `customer_triggers` row of `type='booking_intent'` in last 7 d AND no `type='payment_promised'` AND `customer_facts.updated_at < now() - 24h` | Yes |
| 4 | `warm_silent_2h` | `label='WARM'`; > 2 h since last customer msg; no operator reply since | Yes |
| 5 | `warm_24h_reengage` | `label='WARM'`; > 24 h since last customer msg; `reengage_attempts < 2` | Yes |
| 6 | `cold_7d_reengage` | **manual only** — never auto-fires; populated by `/reengage <phone>` command (see §3c). Included here for symmetry of trigger codes. | n/a |

3. **Filter out** any customer who:
   - is `label IN (PAUSED_SPAM, PAUSED_B2B, PAUSED_PERSONAL)`
   - has `reengage_attempts >= 2` for re-engage-type triggers
   - has an OPEN `follow_up_queue` row (`status IN (queued, drafted)`)
   - had a `follow_up_queue` row in the last 24 h that ended
     `approved | sent` (the "1 per 24 h" cap)
   - is in quiet hours (22:00–09:00 Dubai = 18:00–05:00 UTC) for
     triggers where quiet-hours apply.
4. INSERT one row into `follow_up_queue` with `status='queued'` per
   eligible customer.
5. Return:
```json
{
  "ok": true,
  "scanned": 47,
  "eligible": [
    { "customer_id":"…", "trigger":"hot_silent_30m", "evidence":"30m since 'AED 10k ok'", "queue_id": 124 }
  ]
}
```

### 2c. New endpoint `POST /draft-followup`

Body: `{ "customer_id":"…", "trigger":"hot_silent_30m", "queue_id": 124 }`

1. Read recent history from WAHA (existing helper, used by `/draft`).
2. Read `customer_facts`, `customer_label_history` (last 3 rows),
   `customer_notes`, recent `customer_triggers`, recent
   `behavior_rules` (the same behavioural context block the live
   workflow loads — DRY this up by reusing `behavioral_context()`).
3. Build a Hermes prompt with:
   - The full `system-prompt.md` content (Maria persona, rules).
   - The behavioural context block.
   - A new injected **Follow-up directive** specific to the trigger:

| trigger | Hermes directive |
|---|---|
| `sameday_15m` | "The customer asked about a same-day booking 15+ minutes ago and we haven't replied. Send a brief reply confirming availability or asking the one specific detail needed to lock it in. Acknowledge the gap with care, not apology. ≤ 2 sentences." |
| `hot_silent_30m` | "We had a hot lead momentum and the conversation has been quiet ~30 min. Send a single short message that picks up where they left off, references the specific yacht/date, and reduces friction toward the next step. ≤ 2 sentences." |
| `warm_silent_2h` | "Warm conversation has been quiet 2+ hours. Send one helpful follow-up that adds value — answer a likely next question, suggest a date alternative, or share a relevant detail. Not 'just checking in'. ≤ 2 sentences." |
| `warm_24h_reengage` | "It's been 24+ hours. One re-engagement message: brief, value-led, easy to respond to. If the customer mentioned a date that's now imminent, lead with that. ≤ 2 sentences." |
| `cold_7d_reengage` | "This lead went cold ~7+ days ago. One soft re-engagement — reference what they were originally interested in, mention something genuinely new (current availability, season, new offer). ≤ 2 sentences." |
| `booking_no_payment_24h` | "Booking intent was clear yesterday but no payment was completed. One message that gently re-surfaces the booking detail and the payment step, without pressure. ≤ 2 sentences." |

4. Call Hermes with the assembled prompt + history. Same JSON
   output contract as `/draft` (text, notes, optional
   `payment_signal`).
5. Build a Telegram approval-card payload (see §3b) with a
   distinct header so the operator can tell follow-ups from normal
   customer replies. Return:
```json
{
  "ok": true,
  "draft_text": "…",
  "notes": "…",
  "approval_card_header": "🔔 PROACTIVE FOLLOW-UP — hot_silent_30m",
  "queue_id": 124
}
```
Always 200; on error returns `{ok:false, error:"…"}` and the
workflow node continues to the next queue item.

### 2d. New endpoint `POST /reengage` (manual operator command)

Body: `{ "customer_id":"…", "actor":"operator" }`. Inserts a
`follow_up_queue` row with `trigger='manual:/reengage'`,
`status='queued'`, bypasses the 24-h dedup and quiet hours, sets
`reengage_attempts -= 0` semantics (manual is exempt from the cap).
Returns the `queue_id`.

### 2e. Helper extracted from existing code

`behavioral_context(customer_id)` — already exists in `server.py`
(returns the `formatted` markdown block). Reuse here unchanged.

---

## 3. Workflow changes

### 3a. Live writes to `conversation_state` (live workflow, every message)

Two single-node insertions into `workflows/phase-1b-telegram.json`:

1. **`Mark Customer Msg`** — HTTP node, fires right after
   `Customer Facts` (parallel arm, doesn't gate Build Prompt).
   `POST /conversation-state { event:'customer_message', customer_id }`.
   `onError: continueRegularOutput`.
2. **`Mark Operator Reply`** — HTTP node, fires right after
   `Send Reply via WhatsApp` (the existing approved-send node).
   `POST /conversation-state { event:'operator_reply', customer_id }`.
   Also wire it after `Auto Gate ──▶ AUTO send` so autonomous sends
   are recorded too. `onError: continueRegularOutput`.
3. **`Mark Followup Sent`** — HTTP node, fires after the same
   approved-send node BUT only when the just-approved draft's
   `pending.is_followup === true` (see §3b). Posts
   `event:'followup_sent'`. Conditional via a small IF node
   `Followup?`.

### 3b. Approval-card extension (live workflow, `Queue & Format`)

When `/draft-followup` posts a draft into `pendingQueue`, the queue
entry carries `is_followup: true` and `followup_trigger: <key>`. The
`Queue & Format` Telegram payload prepends the
`approval_card_header` line above the existing customer-header block.
The 5-button inline keyboard is identical to a normal draft — Send /
Auto / Edit / Skip / Take Over. The `Send` callback routes through
the existing `Send Reply via WhatsApp` path; we don't reinvent.

On the bridge side, `_draft_followup` builds the pendingQueue entry
in the same shape `/draft` does, with the two new fields. The proactive
scanner workflow (§3c) does the posting.

### 3c. Proactive scanner (NEW workflow file: `workflows/followup-scanner.json`)

Schedule Trigger: every 15 min (`*/15 * * * *`).

```
Schedule (*/15 min)
   │
   ▼
Followup Scan  ─── POST /followup-scan ──▶  ok:true, eligible:[…]
   │
   ▼
Split Out (per eligible item)
   │
   ▼
Draft Followup  ── POST /draft-followup ──▶  draft_text, queue_id, header
   │
   ▼
Queue & Format Followup        (same shape as live workflow's
   │                            Queue & Format — adapter node;
   │                            adds is_followup=true and prepends header)
   ▼
Send Draft to Telegram         (existing-style sendMessage with
   │                            inline keyboard)
   ▼
Save Telegram MsgID            (writes the message_id into the
                                follow_up_queue row + pendingQueue
                                entry, so Send/Skip callbacks resolve)
   ▼
Update Queue Row               (POST /followup-queue-update
                                { queue_id, status:'drafted',
                                  draft_message_id })
```

All HTTP nodes use `onError: continueRegularOutput`. A failure
anywhere just marks that queue item `failed` and the next 15-min
scan will retry (or expire it after N retries).

### 3d. Manual `/reengage <phone>` command (live workflow)

Add a branch in `Process Text Reply`: matches `/reengage <phone>`,
sets `action='reengage_cmd'`. New `Route Text Action` output →
HTTP `POST /reengage` → echo reply
`🔁 Re-engagement follow-up queued for Mark — next 15-min scan will draft it.`

### 3e. Pre-emption (live workflow, side-effect of 3a)

When `Mark Customer Msg` POSTs, the bridge's `_conversation_state`
auto-flips any open follow-up row to `status='preempted'`. The
operator's approval card for that follow-up is now stale; we handle
this two ways:

1. **Soft handling (default):** the card stays in Telegram. If the
   operator clicks Send, the existing customer-reply flow runs and
   the message goes out. Risk: operator sends a "where'd you go"
   line right after the customer replied. Visible but not data-corrupting.
2. **Hard handling (optional, +0.5 h):** add a `Pre-empt Sweep`
   sub-flow that, when `/conversation-state` flips a row to
   `preempted`, edits the Telegram card to add a banner
   "⚠️ Customer just replied — follow-up no longer needed".

Default plan: ship soft handling. Add hard handling later if the
operator reports actual confusion.

---

## 4. Build steps (with time estimates)

> **Build pattern reminder.** Each script: `python3 scripts/build_<n>.py`
> dry-run → diff review → `--deploy` → behavioural verify on test phone
> → `git add -p` + commit. Secret-scan before every commit. `safe_put`
> auto-saves a `PRE-<TAG>` workflow backup.

### Step 1 — DB schema (0.25 h)

- [ ] `db/migrations/002_followup_engine.sql` with §1a + §1b.
- [ ] Apply via `docker exec n8n-postgres-1 psql ...`.
- [ ] Verify `\d conversation_state`, `\d follow_up_queue`, indexes
      and the partial unique constraint exist.
- [ ] Commit: `feat(db): conversation_state + follow_up_queue tables`.

### Step 2 — Bridge writer endpoint + live-workflow timestamps (0.75 h)

- [ ] Add `_conversation_state` handler in `server.py` (§2a).
- [ ] Deploy bridge (`scripts/deploy_bridge.py`).
- [ ] Smoke-test: `curl -d '{"customer_id":"274942918680787@lid",
      "event":"customer_message"}' .../conversation-state` → row
      created.
- [ ] `scripts/build_followup_timestamps.py` — adds `Mark Customer
      Msg` after Customer Facts and `Mark Operator Reply` after
      Send Reply via WhatsApp + Auto-send arm.
- [ ] Dry, deploy, verify on test phone: send one message, confirm
      `last_customer_message_at` updated; press Send on the draft,
      confirm `last_operator_reply_at` updated.
- [ ] Commit: `feat: live conversation_state timestamps`.

### Step 3 — Scanner endpoint + scanner workflow (1.0 h)

- [ ] Add `_followup_scan` handler in `server.py` (§2b). Carefully
      structure the trigger evaluation as a pure function for unit
      testing.
- [ ] Add `_draft_followup` handler in `server.py` (§2c) — reuses
      `behavioral_context()` and the existing Hermes prompt scaffold.
- [ ] Deploy bridge. Smoke: a hand-INSERTed
      `conversation_state` row with `last_customer_message_at = now() - '40 min'`
      and `customer_facts.label='HOT'` triggers `hot_silent_30m`
      when `/followup-scan` is hit.
- [ ] Create `workflows/followup-scanner.json` (§3c). Build via
      `scripts/build_followup_scanner.py` (preferred — versioned)
      rather than hand-importing the JSON.
- [ ] Activate the workflow. Wait one 15-min tick. Verify a
      follow-up card lands in the admin chat.
- [ ] Commit: `feat: proactive follow-up scanner + draft generator`.

### Step 4 — Approval card extension + sent-tracking (0.75 h)

- [ ] In `Queue & Format` (live workflow) — accept `is_followup`
      from the pendingQueue entry and prepend the header line.
- [ ] Add `Followup?` IF + `Mark Followup Sent` HTTP node after
      `Send Reply via WhatsApp`.
- [ ] Verify on test phone:
      - hand-INSERT a queue row with `hot_silent_30m`,
      - wait for next tick,
      - card lands with the `🔔 PROACTIVE FOLLOW-UP` header,
      - press Send,
      - `follow_up_queue.status` flips to `sent`,
      - `conversation_state.last_followup_at` set.
- [ ] Commit: `feat: follow-up approval cards + sent tracking`.

### Step 5 — `/reengage` command + pre-emption (0.75 h)

- [ ] Add `_reengage` handler (§2d).
- [ ] `scripts/build_reengage_command.py` — adds the `Process Text
      Reply` branch + `Route Text Action` output + HTTP node + echo.
- [ ] Deploy, verify: DM `/reengage +971509767187` → row inserted,
      next scan picks it up, card posts.
- [ ] Verify pre-emption: queue a follow-up manually, then send a
      customer message from the test phone before the next scan
      ticks; confirm the row flips to `preempted` and the next scan
      skips it.
- [ ] Commit: `feat: /reengage command + pre-emption handling`.

---

## 5. Open questions for operator

1. **Quiet hours.** Default 22:00–09:00 Dubai. Confirm? Should
   `sameday_15m` ignore quiet hours (urgent) — default plan says yes.
2. **`reengage_attempts` cap.** Plan says 2 for re-engage triggers
   (`warm_24h_reengage`, `cold_7d_reengage`). Sufficient? After 2,
   no more auto-follow-ups; manual `/reengage` still works.
3. **HOT 30-min threshold.** Operator-defined. If 30 min is too
   eager, raise to 45–60 min. Easy to tune (single constant).
4. **Pre-emption handling.** Soft (just stop tracking) or hard
   (banner the Telegram card)? Default plan: soft for v1, hard
   optional add-on.
5. **Manual override before scan picks up.** If operator manually
   replies before a queued follow-up gets drafted, the scanner
   sees `last_operator_reply_at > last_customer_message_at` and
   skips on the next tick. Confirmed correct behaviour.
6. **Approval card spam.** A burst of 10 customers hitting WARM-2h
   at once produces 10 cards in 15 min. Worth a `max_per_tick`
   limit (e.g. 5)? Default plan: no limit; surface as add-on if
   ever needed.
7. **`booking_no_payment_24h` accuracy.** Detection depends on
   `customer_triggers` having a `payment_promised` row when the
   Nomod link was actually paid. Right now the workflow doesn't
   write that — Nomod has no webhook. So this trigger fires
   correctly for "link sent, no follow-up by customer"; mis-fires
   if the customer paid and the operator never confirmed in chat.
   Operator decision: leave as-is and rely on operator marking
   PAID manually (`/feedback` rule "customer paid, snooze"), or
   defer this trigger until we have payment-status tracking?
8. **Trigger collision.** If a customer qualifies for both
   `hot_silent_30m` and `sameday_15m`, plan says
   `sameday_15m` wins (priority 1). Confirm.
9. **Followup workflow as a separate `.json`.** Cleaner separation
   AND simpler activation toggle for kill-switch. Alternative is
   adding 7 nodes to the live workflow (113 → 120). Default plan:
   separate workflow.

---

## 6. Edge cases handled

| Case | Handling |
|---|---|
| Bridge down when scanner runs | HTTP nodes use `continueRegularOutput`; nothing queued; next tick retries 15 min later |
| `customer_facts.label` column missing (7a not built yet) | `_followup_scan` defaults missing labels to WARM; only time-based triggers fire |
| Customer pre-empts (replies before approval) | `_conversation_state` flips open row to `preempted`; soft-handled per §3e default |
| Operator approves a follow-up that's now stale (customer replied 1h ago) | Approved send goes out; operator made the call. If concerning in practice, add hard pre-emption per §3e |
| Two simultaneous scanner ticks (shouldn't happen but…) | DB partial unique index on `(customer_id, DATE(created_at)) WHERE status NOT IN ('skipped',…)` blocks double-queue |
| Re-engage attempt count race | `reengage_attempts` increment happens in a single UPDATE with a CASE; no read-then-write window |
| Hermes returns junk | `_draft_followup` validates draft_text length and rejects under 10 chars; row goes `failed`, scanner retries next tick |
| Customer in PAUSED state | filtered out at scan time; manual `/reengage` still works (overrides) |
| Customer is in AUTONOMOUS mode | follow-up still drafts and posts to operator approval — by design, automation never auto-sends a follow-up; operator must approve. Surface as open question if operator wants AUTO-send for follow-ups too. |
| Customer never had a `conversation_state` row (legacy/older customers) | scanner ignores them — no last-msg timestamp to threshold against. Backfill optional via a one-off SQL from `customer_facts.updated_at` if needed. |

---

## 7. Rollback path

1. **Step 5 (manual + pre-emption):** restore `PRE-REENGAGE`
   backup. Bridge endpoint can stay (unused).
2. **Step 4 (approval card + sent tracking):** restore
   `PRE-FOLLOWUPCARD` backup. Existing draft cards unaffected.
3. **Step 3 (scanner workflow):** deactivate
   `workflows/followup-scanner.json` (n8n UI toggle). No further
   follow-ups generated. Existing queue rows: leave alone or
   `UPDATE follow_up_queue SET status='expired' WHERE status IN
   ('queued','drafted')`.
4. **Step 2 (live timestamps):** restore `PRE-FOLLOWUPTS` backup.
   `conversation_state` rows go stale but harm nothing.
5. **Step 1 (schema):** additive only; keep the tables.
   If abandoning entirely: `DROP TABLE follow_up_queue;
   DROP TABLE conversation_state;`.

Full feature kill-switch (no rollback to Step 1 needed): deactivate
the scanner workflow + restore Step 4 backup. Bridge endpoints
unused, tables idle.

---

## 8. Dependencies on other plans

- **Depends on 7a** (`customer_facts.label`) for HOT/WARM/COLD
  thresholding. Fallback: treat every customer as WARM; lose
  HOT-30m precision.
- Independent of **7c**. 7c can consume `follow_up_queue` rows in
  its `v_lead_summary` view if it wants ("4 follow-ups queued, 1
  expired this week"), but no hard coupling.
- Plays nicely with `/feedback` (built v3.0): operator can
  `/feedback` "stop sending follow-ups to corporate enquiries", and
  the resulting `behavior_rules` row gets loaded into the draft
  generator's behavioural context block.

---

## 9. Files this plan will touch (when built)

- `db/migrations/002_followup_engine.sql` — new
- `hermes-bridge/server.py` — `+~200` lines (4 endpoints + helpers,
  reuses existing Hermes scaffold)
- `workflows/phase-1b-telegram.json` — `+~6` nodes (timestamps,
  followup-IF, /reengage branch)
- `workflows/followup-scanner.json` — new (~8 nodes)
- `scripts/build_followup_timestamps.py` — new
- `scripts/build_followup_scanner.py` — new
- `scripts/build_followup_card.py` — new
- `scripts/build_reengage_command.py` — new
- `docs/STATUS.md` — append "Follow-up Engine" section
- `docs/followup-engine-plan.md` — this file (tick boxes as steps
  land)

**End of plan 7b.**
