# Pipeline Review — Implementation Plan

> **Status:** PLAN ONLY. No code written. No workflow changes deployed.
> Built artifacts will follow the standard pattern (`scripts/build_*.py`,
> `safe_put` via `n8n_deploy.N8N`, bridge edits compile-checked then
> restarted, behavioural verify on test phone `+971509767187`).
>
> **Supersedes:** `lead-tracker-plan.md` (7a), `followup-engine-plan.md`
> (7b), `lead-intelligence-plan.md` (7c). Those docs are kept for
> reference but **not** the build target.

**Goal:** Behind the scenes, the bot labels every conversation as
messages arrive. Twice a day (and on demand via `/review`) it posts
**one ranked report** to the operator chat — leads most-worth-acting-on
first, with a one-line "why" and a [Draft nudge] button on each item.
No individual proactive cards, no transition alerts, no spam — except
one specific interrupt: a customer asking about same-day booking when
no draft has been generated yet.

**Architecture (one paragraph):** Three layers of analysis feed one
report. **(a)** Per-message: `/label-eval` runs inline with every
customer message — cheap regex/trigger heuristics, real-time labeling,
real-time same-day interrupt. **(b)** Hourly: a sweep job re-analyzes
every chat that has had activity since its last analysis — deeper
look at the full conversation, Hermes-driven sentiment for ambiguous
cases, cold-decay, and self-improvement adjustments based on past
operator corrections. **(c)** 09:00 + 17:00 Dubai (and on-demand
`/review`): the same scoring + rendering pipeline produces the
ranked report posted to the operator chat. Schema lives in
`customer_facts.label`, `customer_label_history`, `conversation_state`
(with `last_analyzed_at` for skip-if-unchanged), and
`label_corrections` (records manual overrides → drives
self-improvement). Inline [Draft nudge] buttons fire the existing
`/draft-followup` path which posts a normal approval card — the
existing Send/Auto/Edit/Skip pipeline does the sending work. One
narrow interrupt (`SAMEDAY_NO_DRAFT`) is the only unsolicited ping.

**Build sequence:** 5 steps, ~6.5 h across 2 sessions. Each step
independently revertable.

---

## 1. Database changes

### 1a. `customer_facts.label` (NEW columns)

```sql
ALTER TABLE customer_facts
  ADD COLUMN label                TEXT NOT NULL DEFAULT 'NEW',
  ADD COLUMN label_updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  ADD COLUMN label_locked_until   TIMESTAMPTZ,
  ADD COLUMN label_locked_reason  TEXT;

CREATE INDEX idx_customer_facts_label         ON customer_facts (label);
CREATE INDEX idx_customer_facts_label_updated ON customer_facts (label_updated_at DESC);
```

**Allowed labels:** `NEW`, `WARM`, `HOT`, `NEEDS_ATTENTION`, `COLD`,
`PAUSED_SPAM`, `PAUSED_B2B`, `PAUSED_PERSONAL`. No DB enum (string,
validated bridge-side, zero-downtime for future additions).

### 1b. `customer_label_history` (NEW table)

```sql
CREATE TABLE customer_label_history (
  id            SERIAL PRIMARY KEY,
  customer_id   TEXT NOT NULL,
  from_label    TEXT,
  to_label      TEXT NOT NULL,
  signal        TEXT NOT NULL,     -- 'auto:money_mentioned', 'manual:/label', 'scheduled:cold_decay', ...
  evidence      TEXT,
  message_count INTEGER,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  created_by    TEXT NOT NULL DEFAULT 'system'
);

CREATE INDEX idx_label_hist_customer ON customer_label_history (customer_id, created_at DESC);
CREATE INDEX idx_label_hist_to_label ON customer_label_history (to_label, created_at DESC);
```

### 1c. `conversation_state` (NEW table)

```sql
CREATE TABLE conversation_state (
  customer_id              TEXT PRIMARY KEY,
  last_customer_message_at TIMESTAMPTZ,
  last_operator_reply_at   TIMESTAMPTZ,
  last_review_seen_at      TIMESTAMPTZ,    -- when this customer last appeared in /review
  last_nudge_drafted_at    TIMESTAMPTZ,    -- [Draft nudge] button last pressed
  last_analyzed_at         TIMESTAMPTZ,    -- last hourly-sweep analysis ran
  last_analysis_signal     TEXT,           -- top signal from last sweep
  last_analysis_confidence REAL,           -- 0.0–1.0, dampened by past corrections
  reengage_attempts        INTEGER NOT NULL DEFAULT 0,
  updated_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_conv_state_last_cust ON conversation_state (last_customer_message_at DESC);
CREATE INDEX idx_conv_state_last_op   ON conversation_state (last_operator_reply_at  DESC);
```

### 1d. Audit-prep — minor additions to existing tables

Two un-tracked pieces the report needs:
- **Operator-approved-send timestamp.** Add an `INSERT INTO
  conversation_state (customer_id, last_operator_reply_at)
  ON CONFLICT (customer_id) DO UPDATE …` call from
  `Send Reply via WhatsApp` (approved) AND from the autonomous send
  arm. No new table.
- **Draft-posted flag.** Redis key `draft:posted:<customer_id>` SET
  with TTL 86400 by `Send Draft to Telegram` for the
  `SAMEDAY_NO_DRAFT` interrupt detection. No new table.

### 1e. `label_corrections` (NEW table — feeds self-improvement)

Every time the operator manually overrides an auto-set label via
`/label <phone> <LABEL>`, a row is written here. The hourly sweep
reads this table to compute per-signal confidence dampening.

```sql
CREATE TABLE label_corrections (
  id              SERIAL PRIMARY KEY,
  customer_id     TEXT NOT NULL,
  auto_label      TEXT NOT NULL,        -- what the system had set
  auto_signal     TEXT NOT NULL,        -- which signal drove the auto label
  auto_confidence REAL,                 -- the confidence at the time
  manual_label    TEXT NOT NULL,        -- what the operator changed it to
  message_count   INTEGER,              -- snapshot
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  -- enrichment fields populated by weekly review (NULL until then)
  pattern_summary TEXT,                 -- Hermes-derived pattern (e.g. "AED < 5000 + no booking words → operator says WARM")
  reviewed_at     TIMESTAMPTZ
);

CREATE INDEX idx_label_corr_signal      ON label_corrections (auto_signal, created_at DESC);
CREATE INDEX idx_label_corr_customer    ON label_corrections (customer_id, created_at DESC);
```

### 1f. `v_lead_summary` (NEW view, used only by `/review`)

```sql
CREATE OR REPLACE VIEW v_lead_summary AS
SELECT
  cf.customer_id,
  cf.name,
  cf.label,
  cf.label_updated_at,
  cf.label_locked_until,
  cf.message_count,
  cf.yachts,
  cf.dates,
  cf.party_size,
  cs.last_customer_message_at,
  cs.last_operator_reply_at,
  cs.last_review_seen_at,
  cs.last_nudge_drafted_at,
  cs.reengage_attempts,
  cm.mode AS conversation_mode,

  (SELECT max(ct.created_at) FROM customer_triggers ct
    WHERE ct.customer_id = cf.customer_id AND ct.type = 'payment_link_sent')   AS last_payment_link_at,
  (SELECT max(ct.created_at) FROM customer_triggers ct
    WHERE ct.customer_id = cf.customer_id AND ct.type = 'payment_promised')    AS last_payment_promised_at,
  (SELECT max(ct.created_at) FROM customer_triggers ct
    WHERE ct.customer_id = cf.customer_id AND ct.type = 'booking_intent')      AS last_booking_intent_at,
  (SELECT max(ct.created_at) FROM customer_triggers ct
    WHERE ct.customer_id = cf.customer_id
      AND ct.type IN ('rejected_price','rejected_timing','rejected_other'))    AS last_rejection_at,
  (SELECT ct.type FROM customer_triggers ct
    WHERE ct.customer_id = cf.customer_id
      AND ct.type IN ('rejected_price','rejected_timing','rejected_other')
    ORDER BY ct.created_at DESC LIMIT 1)                                       AS last_rejection_kind,
  (SELECT string_agg(note_text, ' | ' ORDER BY created_at DESC)
    FROM customer_notes WHERE customer_id = cf.customer_id AND active = true LIMIT 5)
                                                                               AS recent_notes,

  cf.updated_at
FROM customer_facts cf
LEFT JOIN conversation_state cs ON cs.customer_id = cf.customer_id
LEFT JOIN conversation_modes  cm ON cm.customer_id = cf.customer_id;
```

---

## 2. Bridge changes (`hermes-bridge/server.py`)

### 2a. `POST /label-eval` — silent label updater (per-message)

Called by the live customer-message branch right after `Customer Facts`.
Body: `{ customer_id, latest_message, skip_if_locked: true }`.

Algorithm:
1. Read current row from `customer_facts`. If
   `label_locked_until > now()` and `skip_if_locked`, return current.
2. Pull recent `label_corrections` (last 90 days) for confidence
   dampening (see §2a step 4 below).
3. Run signal heuristics in priority order (single match wins):

| Priority | Signal | Detects | Target |
|---|---|---|---|
| 1 | `payment_intent` | `customer_triggers` row of type `payment_promised`/`payment_link_sent`/`booking_intent` in last 24 h | HOT |
| 2 | `money_mentioned` | regex on latest msg / last 5 msgs: `AED`, `\$\d`, `price`, `budget`, `cost`, `how much`, digits ≥ 4 near a yacht name | HOT |
| 3 | `lets_do_it` | regex: `let'?s (do it|book|lock)`, `i'?ll take it`, `sounds (good|great), book` | HOT |
| 4 | `same_day_booking` | "today", "tonight", "now", "asap" + yacht keyword | HOT (overlays NEEDS_ATTENTION if no draft yet — see §2c) |
| 5 | `multi_yacht_engaged` | `yachts` ≥ 2 entries AND `message_count ≥ 4` AND no `?` in latest customer msg | HOT |
| 6 | `pricing_inquired` | price-question regex, no money commitment | WARM |
| 7 | `date_asked_no_commit` | `dates` populated, no HOT signal | WARM |
| 8 | `engaged_5plus` | `message_count >= 5`, label is `NEW` | WARM |
| 9 | `new_window` | `message_count <= 3` and no other signal | NEW |

4. **Confidence dampening (self-improvement).** Compute
   `confidence = 1.0 - (corrections_for_this_signal_in_last_90d / 5.0)`,
   clamped to `[0.2, 1.0]`. If `confidence < 0.4`, demote the target
   by one tier (HOT → WARM, WARM → NEW). The signal still records
   in history; the label just becomes less aggressive when the
   operator has repeatedly disagreed. The `last_analysis_signal` +
   `last_analysis_confidence` columns get updated regardless of
   whether the label changed.
5. **Cold decay** is NOT in this endpoint — it runs in the hourly
   sweep (§2h). Keeps `/label-eval` cheap per-message.
6. If label changes: UPDATE `customer_facts`, INSERT
   `customer_label_history`. Return `{ok, label, previous_label,
   changed, signal, confidence, evidence}`. Always 200, fail-open on
   exception.

### 2b. `POST /conversation-state` — silent timestamp updater

Body: `{ customer_id, event }` where `event ∈ {customer_message,
operator_reply, nudge_drafted}`.

- `customer_message` → UPSERT `last_customer_message_at = now()`.
- `operator_reply` → UPSERT `last_operator_reply_at = now()`,
  `reengage_attempts = 0`.
- `nudge_drafted` → UPSERT `last_nudge_drafted_at = now()`,
  increment `reengage_attempts` if the customer's
  `last_customer_message_at` is more than 24 h ago.

Always 200, fail-open.

### 2c. `POST /sameday-interrupt-check` — the only proactive alert

Called from inside `/label-eval` whenever the matched signal is
`same_day_booking`. The bridge:
1. Reads Redis `draft:posted:<customer_id>`. If present → return
   `{interrupt:false}`.
2. Reads Redis `interrupt:fired:<customer_id>`. If present → return
   `{interrupt:false}` (one ping per customer per 4 h).
3. Otherwise: SET `interrupt:fired:<customer_id>` EX 14400,
   return `{interrupt:true, alert_text:"⚡ SAME-DAY ASK — <name> · <dates> · we haven't drafted yet"}`.

The live workflow's `Label Eval` node looks at `interrupt:true` and
fires a parallel `sendMessage` to the admin chat. Does NOT block the
draft flow.

### 2d. `POST /hourly-sweep` — deep re-analysis with skip-if-unchanged

Called by a new cron workflow every hour at minute 0. Body: `{}`.

Algorithm:
1. **Select work.** Read every `conversation_state` row where
   `last_customer_message_at > COALESCE(last_analyzed_at, '1970-01-01')`
   OR `last_operator_reply_at > COALESCE(last_analyzed_at, '1970-01-01')`.
   This is the "skip-if-unchanged" gate — chats with no new
   activity since their last analysis are skipped entirely. On a
   quiet hour this returns zero rows and the sweep finishes in
   ~50 ms.
2. **Hard cap.** Process at most 200 customers per hour
   (`HOURLY_SWEEP_BATCH_LIMIT`). Anything over goes in the next
   hour. Prevents runaway cost on backlog days.
3. **Per-customer loop:**
   a. Read last ~30 messages from WAHA (existing helper).
   b. Run the deeper analysis (which `/label-eval` can't do
      cheaply per message):
      - Re-run all signal regexes against the *full* recent window,
        not just the latest message.
      - Detect multi-message patterns: 3+ messages with no
        commitment language despite price-mention (→ WARM, not
        HOT), or 2+ enthusiastic messages about the same yacht
        (→ HOT confirmation).
      - If the signal set is ambiguous (multiple competing signals,
        OR `engaged_5plus` triggered without a clearer signal),
        call Hermes once with `system-prompt.md` + the recent
        history + a strict "respond with one of {HOT, WARM, COLD,
        NEW} and a one-sentence reason" prompt. Cap: at most 30
        Hermes calls per sweep run (cost bound), priority-ordered
        by `message_count DESC`.
   c. Apply the same confidence dampening from §2a step 4 using
      `label_corrections`.
   d. **Cold decay** check: if `now() - last_customer_message_at >
      interval '7 days'` AND label NOT LIKE 'PAUSED_%' AND no HOT
      signal → label = COLD.
   e. If label changed: UPDATE `customer_facts`, INSERT
      `customer_label_history` with `signal='hourly_sweep:...'`.
   f. UPDATE `conversation_state` `last_analyzed_at = now()`,
      `last_analysis_signal`, `last_analysis_confidence`.
4. **Return summary:**
```json
{
  "ok": true,
  "scanned": 47,
  "transitions": 6,
  "hermes_calls": 12,
  "skipped_unchanged": 138,
  "elapsed_ms": 4823
}
```
Always 200; fail-open per-customer (one bad row doesn't kill the
sweep — logged and skipped).

### 2e. `POST /review` — the ranked report builder

Body: `{ mode: "scheduled" | "ondemand", filter?: "all" | "hot" | "cold" | "warm" }`

Algorithm:
1. Read all rows from `v_lead_summary` (cold decay already handled
   by the hourly sweep — `/review` no longer needs to do it).
2. Apply optional `filter` (default `all`).
3. **Score** each row (pure-Python, deterministic). Score components:

   ```
   score = 0
   + 1000 if label = NEEDS_ATTENTION
   +  800 if label = HOT
   +  500 if label = WARM
   +  300 if label = NEW
   +  100 if label = COLD AND last_booking_intent_at NOT NULL  (almost-bought)
   +  50  if label = COLD AND last_rejection_kind = 'rejected_price'
            AND last_rejection_at < now() - 30 days
   +  50  if label = COLD AND last_rejection_kind = 'rejected_timing'
            AND dates parseable into the next 14 days
   -  10000 if label LIKE 'PAUSED_%'
   -  500   if label_locked_until > now()              (snoozed)

   # urgency boosts
   + 300 if last_customer_message_at > last_operator_reply_at
            AND now() - last_customer_message_at > 30min     (we owe a reply)
   + 200 if label = HOT AND now() - last_customer_message_at > 2h
   + 400 if dates ILIKE '%today%' OR '%tonight%'
            AND last_operator_reply_at IS NULL
            OR last_operator_reply_at < last_customer_message_at
   + 150 if last_payment_link_at NOT NULL
            AND last_payment_promised_at IS NULL
            AND now() - last_payment_link_at > 24h            (link sent, no payment, 24h+)

   # damping
   - 100 if last_nudge_drafted_at > now() - 24h               (we just nudged)
   - 200 if last_review_seen_at > now() - 6h                  (recently shown)
   ```

4. **Sort** by score descending. **Section** by label for the report
   (HOT / NEEDS_ATTENTION / WARM / NEW-engaged / COLD-reengage). Drop
   anything with `score < 0` (pause/snooze) into a tail summary line.
5. **Render** the Telegram report. For each section, max items by
   section (HOT: 10, NEEDS_ATTENTION: 10, WARM: 8, COLD-reengage: 5).
   If more, append `+N more — /review <section> to see all`.
6. **Hermes "why" line** — for the top 3 items overall, the bridge
   calls Hermes once with the structured snapshot of those 3 rows and
   asks for a one-sentence "why this lead, what to do" for each. The
   rest get a heuristic template line ("⏱ silent 9h · payment link
   sent 24h ago, no commitment"). Keeps Hermes cost bounded to 1 call
   per report.
7. **Build the inline keyboard.** Each top item gets:
   - `[Draft nudge]` → callback `nudge:<customer_id_short>`
   - `[Snooze 4h]` → callback `snooze:<customer_id_short>:4h`
   - `[Info]` → callback `info:<customer_id_short>`

   Customer IDs are long (`274942918680787@lid`) and Telegram
   callback_data is capped at 64 bytes. Map IDs to short codes via a
   Redis hash `review:<message_id>:codes` set at render time and read
   on callback (5 min TTL — operator reads the report soon or not at
   all).
8. **Mark seen** — UPDATE `conversation_state SET
   last_review_seen_at = now()` for every customer included.
9. Return `{ok, telegram_text, inline_keyboards:[ [...row1], [...row2], ... ], totals: {...}}`.

Always 200, fail-open on exception (returns `{ok:false, error}` and
the workflow shows a fallback line).

### 2f. `POST /draft-followup` — backs [Draft nudge]

Body: `{ customer_id, trigger?: <derived from current label/signal> }`.

Reuses the existing `/draft` scaffold:
- Read WAHA history.
- Read behavioural context (`behavioral_context(customer_id)`).
- Compose a Hermes prompt with the existing `system-prompt.md` +
  behavioural block + a small directive specific to the customer's
  current label (HOT → close-oriented; WARM → value-add; COLD →
  soft re-engage).
- Hermes generates draft + notes.
- Bridge builds a pendingQueue entry shape `{ id, type:'nudge',
  is_followup:true, draft:{messages:[…]}, ... }` and writes it via
  the existing pendingQueue path (Redis-backed migration plan exists
  in `docs/pendingqueue-redis-migration-plan.md` and remains the
  cleaner future state — for now use staticData).
- The workflow's `/nudge` callback handler posts the resulting card
  to the admin chat using the existing `Queue & Format` adapter
  (so the operator sees a normal Send/Auto/Edit/Skip card).
- Bridge also POSTs `event: nudge_drafted` to `/conversation-state`.

Returns `{ok, draft_id, telegram_text, telegram_keyboard}` — the
workflow callback handler just relays it.

### 2g. Existing commands carried over

`/info <phone|name>` — full single-lead context dump (used by [Info]
button AND as a slash command).
`/label <phone> <LABEL>` — manual label override; sets
`label_locked_until = now() + 7 days` for PAUSED_*. **Also inserts
a `label_corrections` row** with the auto label that was active +
its signal + confidence at the moment of override (this is the data
that feeds the self-improvement loop in §2a step 4 and §2h).
`/snooze <phone> <duration>` — sets `label_locked_until` only,
parses `\d+[mhd]`.

All three: small handlers, ~30 lines each, share helpers with
`/review`.

### 2h. `POST /weekly-review` — self-improvement digest

Called once a week (Sunday 09:00 Dubai, replacing that day's first
scheduled `/review`). Body: `{}`.

1. Read all `label_corrections` rows where `reviewed_at IS NULL`.
2. Group by `auto_signal`. For each group with ≥ 3 corrections:
   - Send the rows' surrounding context to Hermes with the prompt:
     "These are leads I labeled HOT/WARM/etc that the operator
     corrected. What's the pattern? Reply with one sentence per
     signal group. Be specific."
   - Update each row's `pattern_summary` + `reviewed_at`.
3. Build a digest report — same render style as `/review` but
   replaces the normal sections with:

```
🪞 Self-improvement — week of 2026-05-23
   Corrections processed: 17 (across 12 leads)

   📉 Patterns I'm getting wrong:
   • money_mentioned → HOT: corrected 6× this week
     "AED amounts under 5k + no booking words → you usually
      mark WARM. I'll downgrade confidence by 50%."
   • multi_yacht_engaged → HOT: corrected 2× this week
     "Customers comparing 3+ yachts without dates → you usually
      mark NEW. I'll dampen this signal."

   ✅ Patterns confirmed working:
   • lets_do_it → HOT: 5 transitions, 0 corrections
   • payment_intent → HOT: 8 transitions, 0 corrections

   Adjustments take effect on the next per-message and hourly
   sweep (already updated).
```

4. Post to admin chat at the usual 09:00 slot.

The dampening from §2a step 4 is automatic and continuous — this
weekly digest is **visibility into what's being dampened**, not the
mechanism itself. The operator can `/feedback` an additional rule
("Stop calling things HOT just for AED mentions") at any time and
it goes into the global rules table — that takes effect immediately
in the next draft, separate from this signal dampening which only
affects labels.

### 2i. Module-level helpers

- `LABELS` constant.
- `compute_label(...)` — pure function, unit-testable.
- `score_lead(row, now)` — pure function, unit-testable.
- `render_review(rows, scored, top3_hermes_why)` — pure function.
- `_mask_phone(s)` — last-4-only masking, applied to any markdown
  string built from chat data.
- `MONEY_RE`, `LETS_DO_IT_RE`, `SAME_DAY_RE` regex constants.
- Reuse existing `behavioral_context(customer_id)`.

---

## 3. Workflow changes

### 3a. Live workflow — `Label Eval` branch (inserted post-`Customer Facts`)

```
Customer Facts ─▶ Label Eval ─▶ Interrupt?  ─(yes)─▶ Send Sameday Alert ─┐
                              └─(no)──────────────────────────────────────┴─▶ Build Prompt
```

- **`Label Eval`** — HTTP `POST /label-eval`, body `{ customer_id,
  latest_message, skip_if_locked:true }`. `onError: continueRegularOutput`.
- **`Interrupt?`** — IF `{{ $json.interrupt_required === true }}`.
- **`Send Sameday Alert`** — Telegram `sendMessage` to admin chat,
  text from `$json.alert_text`. Does NOT block; reconverges into
  Build Prompt.

### 3b. Live workflow — timestamps

Two new HTTP nodes, both `onError: continueRegularOutput`:
- **`Mark Customer Msg`** — fires parallel to Build Prompt (off
  Customer Facts). `POST /conversation-state {event:customer_message}`.
- **`Mark Operator Reply`** — fires after `Send Reply via WhatsApp`
  (approved-send arm) AND after the Auto-send arm.
  `POST /conversation-state {event:operator_reply}`.

### 3c. Live workflow — Redis "draft posted" flag

Modify `Send Draft to Telegram` to also `SET draft:posted:<customer_id>
1 EX 86400` via the existing Redis HTTP node pattern. One line of
change.

### 3d. Live workflow — `/review` operator command

`Process Text Reply` — new branch matching `/review` (optional
suffix: `hot`, `cold`, `warm`, `all`). Action code `review_cmd`.
`Route Text Action` — new output `review_cmd` → `Hermes Review`
(HTTP `POST /review` with mode `ondemand`) → `Send Review`
(Telegram `sendMessage` with `parse_mode: Markdown` and
`reply_markup` = `$json.inline_keyboard_json`).

### 3e. Live workflow — `/info`, `/label`, `/snooze` commands

Three more `Process Text Reply` branches + three `Route Text Action`
outputs + three HTTP nodes + three reply nodes. Standard pattern,
small.

### 3f. Live workflow — `Parse Callback` extension

Add 3 new callback action keys: `nudge`, `snooze_btn`, `info_btn`
(plus the `feedback_*` already there). `Parse Callback` resolves
the short code from Redis (`review:<message_id>:codes`) into the
full `customer_id`. New `Route Action` outputs:
- `nudge` → HTTP `POST /draft-followup` → reuse existing
  `Queue & Format` → `Send Draft to Telegram`. New card joins
  pendingQueue exactly like an autonomous-mode draft.
- `snooze_btn` → HTTP `POST /snooze` → answer callback +
  edit the review message's row inline ("💤 snoozed 4h").
- `info_btn` → HTTP `POST /info` → answer callback +
  sendMessage with the full info dump.

### 3g. NEW workflow file — `workflows/pipeline-review-cron.json`

Two Schedule Triggers, both wired to the same `/review` call:
- Cron `0 5 * * *` UTC → 09:00 Dubai
- Cron `0 13 * * *` UTC → 17:00 Dubai

Chain: `Schedule → Hermes Review (POST /review mode=scheduled) →
Send Review (sendMessage with inline keyboard)`. Three nodes per
schedule, six total in the workflow.

On Sundays only, the 09:00 schedule routes to `/weekly-review`
instead of `/review`. Achieved with an IF node after Schedule:
`{{ $now.setZone('Asia/Dubai').weekday === 7 }}` (true on Sunday)
→ Hermes Weekly Review; false → Hermes Review. Single workflow,
zero extra cron entries.

### 3h. NEW workflow file — `workflows/pipeline-hourly-sweep.json`

One Schedule Trigger:
- Cron `0 * * * *` UTC → top of every hour, year-round.

Chain: `Schedule (hourly) → Hermes Hourly Sweep
(POST /hourly-sweep) → (no-op terminal — sweep is silent)`. No
Telegram output; success/failure logged in n8n executions. If the
sweep returns `transitions > 10` in one hour, the workflow posts
an informational line to the admin chat ("🪞 hourly sweep: 12
label transitions") so the operator can spot if something is
auto-relabeling aggressively. Two nodes total.

---

## 4. Build steps (with time estimates)

> **Build pattern reminder.** Each script: `python3 scripts/build_<n>.py`
> dry-run → diff review → `--deploy` → behavioural verify on test
> phone → secret-scan staged diff → `git add -p` + commit.
> `safe_put` auto-saves a `PRE-<TAG>` workflow backup.

### Session 1 — Foundation (~3.0 h)

#### Step 1 — Schema + bridge silent updaters (1.5 h)

- [ ] `db/migrations/001_pipeline_review.sql` with §1a + §1b + §1c
      + §1e (`label_corrections`).
      Apply via `docker exec n8n-postgres-1 psql ...`.
- [ ] Verify `\d customer_facts`, `\d customer_label_history`,
      `\d conversation_state`, `\d label_corrections` — columns and
      indexes present.
- [ ] Add `LABELS`, regex constants, `compute_label` (with
      confidence dampening from `label_corrections`), `_label_eval`,
      `_conversation_state`, `_sameday_interrupt_check` in
      `server.py`. Self-test block under `if __name__ == '__main__'`
      covering ~6 canned inputs (including one that exercises the
      dampening path: pre-seed 5 `label_corrections` rows for
      `money_mentioned`, then verify `compute_label` demotes
      HOT→WARM).
- [ ] Deploy bridge via `scripts/deploy_bridge.py`.
- [ ] Smoke:
      - `curl -s .../label-eval -d '{"customer_id":"…","latest_message":"AED 10000 ok"}'`
        → `label:"HOT", signal:"money_mentioned"`.
      - `curl -s .../conversation-state -d '{"customer_id":"…","event":"customer_message"}'`
        → row written, 200.
      - `curl -s .../label-eval -d '{"customer_id":"…","latest_message":"can we do tonight?"}'`
        → check Redis `interrupt:fired:<id>` set, alert_text returned.
- [ ] Commit: `feat(bridge,db): pipeline-review schema + label-eval +
      conversation-state + sameday interrupt`.

#### Step 2 — Live workflow Label Eval branch + timestamps + interrupt + draft-posted flag (1.25 h)

- [ ] `scripts/build_label_eval_branch.py` — inserts Label Eval +
      Interrupt? IF + Send Sameday Alert (§3a), Mark Customer Msg
      (§3b), Mark Operator Reply on both send arms (§3b),
      draft-posted Redis SET in Send Draft to Telegram (§3c).
- [ ] Dry, deploy with `safe_put` (tag `PRE-LABELBRANCH`).
- [ ] Verify on test phone:
      - Send a clearly HOT message ("AED 10000 sounds fair") →
        no interrupt (not same-day), card posts normally,
        `customer_label_history` row, `customer_facts.label='HOT'`,
        `conversation_state.last_customer_message_at` updated.
      - Press Send on the draft → `last_operator_reply_at` updated.
      - Send a same-day message ("can we do tonight?") with no
        existing posted draft → admin chat receives ⚡ alert,
        card still posts.
      - Send the same same-day message again within 4 h → no
        second alert (deduped).
      - Verify Redis `draft:posted:<id>` set with TTL.
- [ ] Commit: `feat(workflow): label-eval branch + timestamps +
      sameday interrupt + draft-posted flag`.

#### Step 3 — Hourly sweep endpoint + workflow (1.0 h)

- [ ] Add `_hourly_sweep` handler in `server.py` (§2d). Reuse
      `compute_label` from Step 1. Add `HOURLY_SWEEP_BATCH_LIMIT =
      200` and `HOURLY_SWEEP_HERMES_CAP = 30` constants.
- [ ] Deploy bridge.
- [ ] Smoke: `curl .../hourly-sweep` with the bridge in a test box
      → returns `{scanned, transitions, hermes_calls,
      skipped_unchanged, elapsed_ms}`. Hand-mutate one
      `conversation_state` row's `last_customer_message_at` to
      `now()` and re-run — verify it's scanned this time and
      `last_analyzed_at` updated.
- [ ] Create `workflows/pipeline-hourly-sweep.json` (§3h) — single
      cron `0 * * * *` UTC → hourly-sweep HTTP node → IF
      (`transitions > 10`) → optional admin chat info line.
      Activate.
- [ ] Watch one full hour cycle in n8n executions; verify it runs
      at minute 0, the duration is sub-30-s on a quiet hour
      (skip-if-unchanged effective), and transitions are recorded
      in `customer_label_history` with `signal='hourly_sweep:...'`.
- [ ] Commit: `feat(bridge,workflow): hourly pattern-recognition
      sweep + skip-if-unchanged`.

### Session 2 — Report + self-improvement (~3.5 h)

#### Step 4 — `v_lead_summary` view + `/review` endpoint (1.5 h)

- [ ] `db/migrations/002_lead_summary_view.sql` with §1f. Apply.
- [ ] Verify `SELECT customer_id, name, label, message_count,
      last_payment_link_at FROM v_lead_summary LIMIT 5;` returns
      sensible rows.
- [ ] Add `score_lead`, `render_review`, `_review`,
      `_draft_followup` in `server.py`. Reuse `behavioral_context()`.
      Self-test block: feed 5 canned rows, verify scoring +
      ranking.
- [ ] Deploy bridge.
- [ ] Smoke 5 questions hand-curled:
      1. `POST /review {mode:"ondemand"}` → report markdown +
         inline_keyboard_json returned.
      2. `POST /review {mode:"ondemand", filter:"hot"}` → only HOT
         section.
      3. `POST /draft-followup {customer_id:"…"}` → draft
         generated, pendingQueue entry exists, telegram_text
         filled.
      4. Verify `last_review_seen_at` updates after `/review` for
         every customer included.
      5. Verify Hermes "why" line is set for top-3 only.
- [ ] Commit: `feat(bridge,db): /review endpoint + v_lead_summary
      view + /draft-followup`.

#### Step 5 — `/review` command + cron workflow + callback handlers + weekly review (2.0 h)

- [ ] Add `_weekly_review` handler in `server.py` (§2h). Reuses the
      same `render_review` scaffold with a different section
      structure. Marks `label_corrections.reviewed_at` as it goes.
- [ ] `scripts/build_review_command.py` — extends `Process Text
      Reply` with `/review` branch + `Route Text Action`
      `review_cmd` output + Hermes Review HTTP + Send Review
      sendMessage. Also adds `/info`, `/label`, `/snooze` branches.
      `/label` writes a `label_corrections` row in addition to the
      label flip (§2g).
      Adds callback handlers `nudge`/`snooze_btn`/`info_btn` to
      `Parse Callback` + new `Route Action` outputs (§3f).
- [ ] `scripts/build_pipeline_cron.py` (or hand-import) creates
      `workflows/pipeline-review-cron.json` per §3g with the 2
      cron triggers + Sunday weekly-review IF branch.
- [ ] Dry-run all, deploy.
- [ ] Verify E2E by Telegram DM:
      - DM `/review` → ranked report lands within ~3 s, inline
        keyboards render.
      - Tap [Draft nudge] on the top item → approval card posts
        within ~3 s, looks normal, Send/Skip/Auto buttons work.
      - Tap [Snooze 4h] → review row updates, customer's
        `label_locked_until` set.
      - Tap [Info] → info dump posted as a reply.
      - DM `/review hot` → HOT-only filter.
      - DM `/info <phone>` → info dump.
      - DM `/label <phone> PAUSED_SPAM` → labeled, lock applied.
      - DM `/snooze <phone> 6h` → lock applied without label
        change.
      - DM `/label <phone> WARM` (override an auto-HOT) → verify
        `label_corrections` row inserted with
        `auto_label='HOT', auto_signal=…, manual_label='WARM'`.
      - Manually trigger one of the cron schedule nodes → report
        posts.
      - Force-set `$now.weekday` to 7 (Sunday) via a test exec and
        confirm the IF routes to `/weekly-review` → digest posts
        with the self-improvement format.
- [ ] Activate the cron workflow.
- [ ] Commit: `feat: /review + /info + /label + /snooze commands,
      cron, callback handlers, weekly self-improvement digest`.

---

## 5. Open questions for operator

Most decisions are already locked. These three remain:

1. **`/lead` collision.** The existing `/lead <phone>` creates an
   outbound first-contact draft. The natural "show me this lead"
   command would also be `/lead`. **Default plan: use `/info` for the
   context dump; `/lead` stays unchanged.** Confirm.
2. **PAUSED defaults.** `PAUSED_SPAM` = permanent (no auto-unlock).
   `PAUSED_B2B` / `PAUSED_PERSONAL` = 90 days. Confirm.
3. **Cron values.** Configured to 09:00 + 17:00 Dubai = `0 5 * * *`
   + `0 13 * * *` UTC. Confirm Dubai = UTC+4 (no DST).

These can land with defaults unless corrected.

---

## 6. Edge cases handled

| Case | Handling |
|---|---|
| Bridge down when message arrives | `Label Eval` `continueRegularOutput` → label unchanged, no alert, card still posts (same fail-open as Customer Facts) |
| Bridge down when scheduled cron fires | HTTP node fails → no report posted; next cron tick will retry. No half-rendered reports. |
| Bridge down when [Draft nudge] tapped | Callback answers with "⚠️ couldn't reach the bridge"; review message stays. |
| Two messages arrive in debounce window | Label Eval fires once per Build Prompt (post-debounce); no double-count. |
| Operator manual `/label` while autonomous draft in flight | Manual write wins (lock applied); next auto-eval respects the lock. |
| `customer_facts` row absent (first message) | `compute_label` returns `NEW`; history row inserted with `from_label=NULL`. |
| Same-day interrupt would fire twice for the same customer | Redis `interrupt:fired:<id>` EX 14400 blocks for 4 h. |
| Same-day interrupt would fire when we've already drafted | Redis `draft:posted:<id>` check blocks it. |
| Report exceeds Telegram 4096-char limit | Section caps (HOT 10, NEEDS_ATTENTION 10, WARM 8, COLD 5) + tail line "+N more" keeps every report well under the limit. |
| Hermes "why" line generation fails | Top-3 items fall back to heuristic template lines; report still posts. |
| Inline keyboard callback short-code expired (5 min TTL) | Callback answers "⚠️ this report is stale — run /review for fresh." No crash. |
| Operator presses [Draft nudge] twice on the same lead in 24 h | Bridge sees `last_nudge_drafted_at` and answers callback with "💡 already nudged today — Send/Edit the existing card in the queue." No duplicate draft. |
| Customer is in AUTONOMOUS mode and gets nudge-drafted | Nudge produces an approval card like any other — autonomous mode applies only to direct customer replies, not proactive nudges. By design. |
| Hourly sweep racing with per-message `/label-eval` | Both UPDATEs are idempotent (label + label_updated_at + last_analysis_*). Last-write-wins; history preserves both transitions. |
| Hourly sweep takes > 15 min on a busy hour | `HOURLY_SWEEP_BATCH_LIMIT=200` caps per run; overflow processed next hour. n8n schedule trigger overlap is single-threaded by default (concurrency=1 in the workflow settings — confirmed in Step 3 verification). |
| Self-improvement dampens a real-but-rare signal into uselessness | `confidence` clamped to `[0.2, 1.0]` — the signal still fires, just at lower confidence. The weekly review surfaces what's been dampened so the operator can override with a `/feedback` rule. |
| Operator corrects HOT→PAUSED_SPAM 10 times for the same auto_signal | Confidence falls to 0.2 floor. Auto label stops being HOT for that signal but the per-customer override still depends on the operator pressing `/label`. No runaway. |
| Sunday weekly-review fires but there are zero corrections | Bridge returns an empty digest with "✅ no corrections this week"; cron still posts that one-line message at 09:00. |

---

## 7. Self-improvement design (detail)

Three layers, each independent:

**(1) Per-signal confidence dampening (continuous, automatic).**
`/label-eval` and the hourly sweep both read `label_corrections` and
compute `confidence = 1.0 - (corrections_for_signal_last_90d / 5.0)`,
clamped `[0.2, 1.0]`. When `confidence < 0.4` the target label is
demoted one tier. This is silent and continuous — no operator action
required. The system *learns* from corrections by becoming less
aggressive on signals that the operator has repeatedly disagreed with.

**(2) Weekly visibility digest (Sunday 09:00 Dubai).** The
`/weekly-review` endpoint reads un-reviewed corrections, groups by
signal, calls Hermes once per signal-group with ≥ 3 entries to produce
a pattern summary, and posts a digest that says clearly *what's being
dampened and why*. The operator sees the system's adjustment, can
trust it or override it.

**(3) `/feedback` rules (immediate, manual).** Already built (v3.0).
The operator can DM `/feedback Stop calling AED amounts under 5k HOT`
and the resulting `behavior_rules` row is loaded into draft prompts
AND can be honored by the hourly sweep's Hermes call for ambiguous
cases. The behavioural-context block is the layer where natural-language
rules live; the per-signal dampening is the layer where statistical
patterns live. Both feed labels; they don't overlap.

**What v1 explicitly does NOT do:**
- Auto-tune the *scorer* (priority weights in §2e). The scorer is
  hand-set and stays so until we have months of data showing where
  ordering is wrong. Premature.
- Auto-create new signals. Adding a regex / heuristic stays a code
  change. The corrections table tells us *which signals fail*, not
  which new ones to invent.
- Auto-act on patterns. The system surfaces them; the operator
  decides whether to override via `/feedback` or accept the damping.

---

## 8. Rollback path

1. **Step 5 (commands + cron + weekly):** deactivate the
   pipeline-review-cron workflow (n8n UI toggle); restore the
   `PRE-REVIEWCMDS` backup of the live workflow. Bridge endpoints
   remain harmless.
2. **Step 4 (view + `/review` endpoint):** revert the bridge commit.
   Drop the view: `DROP VIEW v_lead_summary;` — nothing reads it
   once Step 5 is rolled back.
3. **Step 3 (hourly sweep):** deactivate
   `workflows/pipeline-hourly-sweep.json`. Per-message `/label-eval`
   keeps working; the system just loses the deeper hourly pass and
   cold-decay (cold-decay can be hand-run via `curl .../hourly-sweep`
   weekly if needed).
4. **Step 2 (workflow branch):** restore `PRE-LABELBRANCH` backup.
   Customer-message flow returns to pre-feature state.
5. **Step 1 (schema):** additive only; keep the columns/tables.
   Drop only if abandoning the feature entirely: `DROP TABLE
   label_corrections; DROP TABLE conversation_state; DROP TABLE
   customer_label_history; ALTER TABLE customer_facts DROP COLUMN
   label, DROP COLUMN label_updated_at, DROP COLUMN
   label_locked_until, DROP COLUMN label_locked_reason;`.

Full feature kill-switch: deactivate both cron workflows + restore
live-workflow backup. Schema and bridge endpoints idle.

---

## 9. What this supersedes

`docs/lead-tracker-plan.md` (7a) — superseded. Schema and
`/label-eval` survive; daily-digest cadence and immediate
HOT-transition alerts pruned in favour of the unified report and
the single same-day interrupt.

`docs/followup-engine-plan.md` (7b) — superseded.
`conversation_state` survives. The `follow_up_queue` table, the
15-min scanner, and auto-posted follow-up cards are gone — replaced
by the [Draft nudge] button inside the report. `/reengage` is
gone — operator uses [Draft nudge] on COLD-reengage candidates.

`docs/lead-intelligence-plan.md` (7c) — superseded.
`v_lead_summary` view and rejection-trigger extraction survive.
The `/ask` natural-language query endpoint is gone — the report
answers most "how's the pipeline" questions structurally. (If
free-form queries later prove worth it, add `/ask` then; the
Hermes "why" line scaffolding is already there.)

The three superseded plan files are marked with a header banner
linking back here.

---

## 10. Files this plan will touch (when built)

- `db/migrations/001_pipeline_review.sql` — new (schema + corrections)
- `db/migrations/002_lead_summary_view.sql` — new
- `hermes-bridge/server.py` — `+~550` lines (helpers + 7 endpoints:
  `/label-eval`, `/conversation-state`, `/sameday-interrupt-check`,
  `/hourly-sweep`, `/review`, `/draft-followup`, `/weekly-review`,
  plus `/info` / `/label` / `/snooze` slash-command handlers; reuses
  `behavioral_context` + existing Hermes scaffold)
- `workflows/phase-1b-telegram.json` — `+~14` nodes (Label Eval
  branch, timestamps, draft-posted flag, /review + /info + /label +
  /snooze commands, 3 new Parse Callback action keys + handlers)
- `workflows/pipeline-review-cron.json` — new (~8 nodes including
  Sunday weekly-review IF branch)
- `workflows/pipeline-hourly-sweep.json` — new (~3 nodes)
- `scripts/build_label_eval_branch.py` — new
- `scripts/build_hourly_sweep.py` — new
- `scripts/build_review_command.py` — new
- `scripts/build_pipeline_cron.py` — new
- `docs/STATUS.md` — append "Pipeline Review" section
- `docs/pipeline-review-plan.md` — this file (tick boxes as steps land)

**End of plan.**
