# Lead Tracker + Auto-labeling + Digests — Implementation Plan (7a)

> **Status:** PLAN ONLY. No code written. No workflow changes deployed.
> Built artifacts will follow the standard pattern (`scripts/build_*.py`,
> `safe_put` via `n8n_deploy.N8N`, bridge edits compile-checked then restarted).

**Goal:** Give the operator a labelled view of every lead, refresh the
label automatically as new messages arrive, and push 3 daily digests +
immediate alerts whenever a lead crosses into HOT.

**Architecture (one paragraph):** Add a `label` column to `customer_facts`
and a sibling `customer_label_history` table. The customer-message branch
of the workflow calls a new bridge endpoint `/label-eval` right after
`Customer Facts`; the bridge runs the signal heuristics, decides the new
label, persists the row, and on any transition into HOT (or any
NEEDS-ATTENTION condition) fires an immediate Telegram alert to the
admin chat. A separate scheduled workflow (3 cron triggers) calls
`/digest` and posts the resulting markdown to the admin chat. Manual
commands (`/label`, `/snooze`, `/info`) live in `Process Text Reply` and
route through the bridge.

**Build sequence:** 4 build scripts, ~3.5 h total. Each step is
independently revertable.

---

## 1. Database changes

### 1a. `customer_facts.label` (NEW column)

```sql
ALTER TABLE customer_facts
  ADD COLUMN label TEXT NOT NULL DEFAULT 'NEW',
  ADD COLUMN label_updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  ADD COLUMN label_locked_until TIMESTAMPTZ,            -- /snooze writes here
  ADD COLUMN label_locked_reason TEXT;                  -- '/snooze', 'manual', etc.

CREATE INDEX idx_customer_facts_label ON customer_facts (label);
CREATE INDEX idx_customer_facts_label_updated ON customer_facts (label_updated_at DESC);
```

**Allowed values for `label`** (string enum, validated in bridge — no DB
constraint so future additions are zero-downtime):

| Label | Meaning |
|---|---|
| `NEW` | first 3 messages, no signals yet |
| `WARM` | pricing inquired OR >5 messages OR date asked, no commitment |
| `HOT` | money/budget mentioned, "let's do it", same-day booking ask, multi-yacht positive engagement, payment intent surfaced |
| `NEEDS_ATTENTION` | same-day-booking customer with no draft yet **OR** HOT lead with no operator reply > 30 min |
| `COLD` | > 7 days since last customer message and no booking signals |
| `PAUSED_SPAM` | manual — operator-set, never auto-revisited |
| `PAUSED_B2B` | manual — agency / corporate enquiry, deferred |
| `PAUSED_PERSONAL` | manual — friend / personal contact, not a real lead |

### 1b. `customer_label_history` (NEW table)

```sql
CREATE TABLE customer_label_history (
  id              SERIAL PRIMARY KEY,
  customer_id     TEXT NOT NULL,
  from_label      TEXT,           -- nullable for first row
  to_label        TEXT NOT NULL,
  signal          TEXT NOT NULL,  -- 'auto:money_mentioned', 'manual:/label', 'manual:/snooze', 'scheduled:cold_decay', ...
  evidence        TEXT,           -- short quote or trigger excerpt
  message_count   INTEGER,        -- snapshot at transition
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  created_by      TEXT NOT NULL DEFAULT 'system'  -- 'system' | 'operator'
);

CREATE INDEX idx_label_hist_customer ON customer_label_history (customer_id, created_at DESC);
CREATE INDEX idx_label_hist_to_label ON customer_label_history (to_label, created_at DESC);
```

### 1c. Grants

`hermes_rw` already has INSERT/SELECT/UPDATE on `public.*`; the new
table inherits via default privileges (verify with `\dp`).

---

## 2. Bridge changes (`hermes-bridge/server.py`)

### 2a. New endpoint `POST /label-eval`

Request:
```json
{ "customer_id": "274942918680787@lid", "latest_message": "let's lock it in for tonight", "skip_if_locked": true }
```

Behaviour:
1. Read current row from `customer_facts` (label, message_count,
   label_locked_until, party_size, dates, yachts).
2. If `label_locked_until > now()` and `skip_if_locked` true → return
   the existing label, `changed: false`, `reason: "locked"`.
3. Run signal heuristics in priority order (return first match):

| Priority | Signal | Detects | Target |
|---|---|---|---|
| 1 | `manual_pause` | label already in `PAUSED_*` and no manual override on this call | unchanged |
| 2 | `payment_intent` | `customer_triggers` row with `type IN ('payment_promised','payment_link_sent','booking_intent')` in last 24 h | HOT |
| 3 | `money_mentioned` | regex on latest_message OR last 5 customer messages: `\bAED\b`, `\$\d`, `price`, `budget`, `cost`, `how much`, digits ≥ 4 near a yacht name | HOT |
| 4 | `lets_do_it` | regex on latest_message: `let'?s (do it\|book\|lock)`, `i'?ll take it`, `sounds (good\|great), book` | HOT |
| 5 | `same_day_booking` | latest_message mentions "today", "tonight", "now", "asap" + a yacht keyword | HOT (and tag NEEDS_ATTENTION-eligible if no draft yet, see §2b) |
| 6 | `multi_yacht_engaged` | `customer_facts.yachts` contains ≥ 2 entries AND message_count ≥ 4 AND last message positive sentiment (Hermes can decide; default heuristic = no `?` and any of `good`, `perfect`, `like`, `available`) | HOT |
| 7 | `pricing_inquired` | last 5 msgs contain price-question regex but no money commitment | WARM |
| 8 | `date_asked_no_commit` | `customer_facts.dates` present AND no HOT signal | WARM |
| 9 | `engaged_5plus` | `message_count >= 5` AND label is `NEW` | WARM |
| 10 | `cold_decay` | scheduled-only signal (see §3c); `now() - updated_at > 7 days` AND label ≠ PAUSED_* | COLD |
| 11 | `new_window` | `message_count <= 3` AND no other signal | NEW |

4. Compute **NEEDS_ATTENTION overlay**:
   - If target = HOT AND no operator reply (i.e. no `autonomous_sends`
     row AND no Telegram outbound recorded — see §5 open question)
     in 30 min → label = NEEDS_ATTENTION.
   - If `same_day_booking` AND no draft posted yet
     (no `pendingQueue` entry visible — derived via Redis key
     `draft:{customer_id}:posted` set by `Send Draft to Telegram`, see
     §5 open question) → label = NEEDS_ATTENTION.
5. If new label ≠ old label:
   - `UPDATE customer_facts SET label=$1, label_updated_at=now()`.
   - `INSERT INTO customer_label_history` with from/to/signal/evidence.
   - If new label IN (`HOT`, `NEEDS_ATTENTION`): set
     `alert_required: true` in the response.
6. Return:
```json
{
  "ok": true,
  "customer_id": "...",
  "label": "HOT",
  "previous_label": "WARM",
  "changed": true,
  "signal": "money_mentioned",
  "evidence": "AED 10000 sounds fair",
  "alert_required": true,
  "alert_text": "🔥 HOT — Mark · AED mentioned · Satoshi 70 · #14"
}
```
Always HTTP 200. On exception, return `{ok:false, label: <existing>}`.

### 2b. New endpoint `POST /digest`

Request: `{ "window": "daily" }` (or `"hot_only"`)

Returns a structured payload:
```json
{
  "ok": true,
  "generated_at": "2026-05-23T08:00:00+04:00",
  "totals": {"hot":4,"needs_attention":2,"warm":7,"new":3,"cold":11},
  "hot": [ { "customer_id":"…", "name":"Mark", "summary":"AED 10k mentioned, Satoshi 70, msg #14, last 2h ago" } ],
  "needs_attention": [ … ],
  "stuck_warm": [ … ],   // warm > 24h with no transition
  "telegram_text": "🌅 Morning Digest — 2026-05-23 …"   // ready to paste
}
```

The `telegram_text` is what the digest workflow sends as-is (one
`sendMessage` per digest, no parsing on the n8n side).

### 2c. New endpoint `POST /label` (manual operator command)

Request:
```json
{ "customer_id":"…", "label":"PAUSED_SPAM", "actor":"operator", "reason":"spam keywords" }
```
Writes the row, inserts a history line with `created_by='operator'` and
`signal='manual:/label'`. Sets `label_locked_until = now() + interval '7 days'`
when label is in `PAUSED_*` (so auto-eval can't undo it).

### 2d. New endpoint `POST /snooze`

Request: `{ "customer_id":"…", "duration":"24h" }` (parses `\d+(m|h|d)`).
Sets `label_locked_until` only; doesn't change `label`. Inserts a
history line with `signal='manual:/snooze'`.

### 2e. New endpoint `POST /lead-info` (full context view)

Request: `{ "customer_id":"…" }` — returns a multi-section markdown
string ready to send to Telegram:

```
🔎 Lead — Mark  (274942918680787@lid)
   Label: HOT (auto, 1h ago — money_mentioned)
   Facts: Satoshi 70 · this Saturday · 2 guests · msg #14
   Notes:
     • prefers evening boarding (2026-05-22)
   Triggers (last 7d):
     • callback_promised — 2026-05-22
   Last customer msg: "let's lock it in" — 2026-05-23 09:14
   Last operator reply: 2026-05-22 18:02
```

Reused by `/info` command (renamed — see §5 open question on `/lead`
collision) AND by the lead-intelligence query interface (7c).

### 2f. Module-level helpers (added to `server.py`)

- `LABELS` constant (set of allowed strings).
- `compute_label(customer_id, latest_message, facts)` — pure function
  returning `(label, signal, evidence)`. Unit-testable.
- `MONEY_RE`, `LETS_DO_IT_RE`, `SAME_DAY_RE` regex constants.
- `apply_needs_attention(label, customer_id, facts)` — wraps the
  overlay rule.

---

## 3. Workflow changes (`workflows/phase-1b-telegram.json`)

### 3a. Customer-message branch (live every message)

Insert one new node between `Customer Facts` and `Build Prompt`:

```
Customer Facts ─▶ Label Eval ─▶ Label Alert?  ─▶  (yes) Send HOT Alert ─▶ Build Prompt
                                              └─▶  (no) ─────────────────▶ Build Prompt
```

- **`Label Eval`** — HTTP Request, `POST /label-eval`, body `{ customer_id, latest_message, skip_if_locked:true }`. `onError: continueRegularOutput` (degrades the same way Customer Facts does).
- **`Label Alert?`** — IF node, condition `{{ $json.alert_required === true }}`.
- **`Send HOT Alert`** — Telegram `sendMessage` to admin chat, text = `$json.alert_text` + a short context line + inline keyboard `[/info <id>] [/snooze <id> 4h]`. Does NOT interrupt the draft flow — it's a side-arm; main path continues to Build Prompt.
- Connect the IF "false" output directly to Build Prompt as well; both branches converge.

### 3b. Manual command branches (`Process Text Reply` + `Route Text Action`)

Add 3 new switch outputs after `Route Text Action`:

| Command | Parse | Bridge call | Reply |
|---|---|---|---|
| `/label <phone> <LABEL>` | parse phone + label tokens; phone-to-customer_id resolution via existing logic | `POST /label` | echo `✅ Mark → HOT (manual)` |
| `/snooze <phone> <duration>` | parse phone + `\d+[mhd]` | `POST /snooze` | echo `💤 Mark snoozed for 4h` |
| `/info <phone>` | parse phone OR name | `POST /lead-info` | relay markdown as-is |

`Process Text Reply` (JS code node) — new branches added in priority
above `/send`/`/lead`. Action codes: `label_cmd`, `snooze_cmd`,
`info_cmd`. `Route Text Action` switch — add the 3 outputs.

### 3c. Daily digest (NEW workflow file: `workflows/digest.json`)

Three Schedule Triggers, all in one workflow:
- Cron `0 4 * * *` UTC → 08:00 Dubai
- Cron `0 9 * * *` UTC → 13:00 Dubai
- Cron `0 16 * * *` UTC → 20:00 Dubai

Each → `Hermes Digest` (HTTP `POST /digest`) → `Send Digest`
(Telegram sendMessage to admin chat using `telegram_text`). Three
trigger nodes, all wired to the same two-node chain.

### 3d. Cold-decay sweep (NEW scheduled branch in the digest workflow)

Cron `0 3 * * *` UTC (07:00 Dubai, before the morning digest):
`Hermes Cold Sweep` → `POST /label-eval` with
`{ "mode":"sweep", "rule":"cold_decay" }`. Bridge iterates all
non-PAUSED rows with `now() - updated_at > 7 days AND label != 'COLD'`
and transitions them to COLD with `signal='scheduled:cold_decay'`. No
Telegram output (silent; transitions surface in the morning digest).

---

## 4. Build steps (with time estimates)

> **Build pattern reminder.** Each script: `python3 scripts/build_<n>.py`
> dry-run → diff review → `--deploy` → behavioural verify on test phone
> (`+971509767187` = `274942918680787@lid`) → `git add -p` + commit.
> Secret-scan staged diff before every commit. `--rollback` available
> via the `PRE-<TAG>` workflow backup `safe_put` auto-saves.

### Step 1 — DB migration + bridge endpoint `/label-eval` (1.0 h)

- [ ] Write SQL migration `db/migrations/001_customer_labels.sql` with
      §1a + §1b. Apply via `docker exec n8n-postgres-1 psql ...`.
- [ ] Verify: `\d customer_facts` shows `label`; `SELECT label,
      COUNT(*) FROM customer_facts GROUP BY label;` returns every
      existing customer as `NEW` (the default).
- [ ] Add `LABELS`, `compute_label`, regex constants, `_label_eval`
      handler in `server.py`. Plus a self-test guarded by
      `if __name__ == '__main__'` that runs `compute_label` against
      ~6 canned inputs.
- [ ] Deploy bridge via `scripts/deploy_bridge.py` (existing).
- [ ] Smoke: `curl -s -H "X-Bridge-Token: …" http://localhost:8788/label-eval
      -d '{"customer_id":"274942918680787@lid","latest_message":"AED 10000 ok"}'`
      → expect `label:"HOT", signal:"money_mentioned"`.
- [ ] Commit: `feat(bridge): /label-eval endpoint + customer_labels schema`.

### Step 2 — Workflow customer-message branch (`Label Eval` + HOT alert) (0.75 h)

- [ ] `scripts/build_label_eval_branch.py` — inserts the 3 nodes from
      §3a, rewires Customer Facts → Build Prompt to go through them.
- [ ] Dry run, eyeball diff, deploy with safe_put (tag `PRE-LABELBRANCH`).
- [ ] Verify on test phone: send a clearly HOT message
      ("AED 10000 sounds fair, lets book"); confirm
      - card posts as before,
      - admin chat receives a `🔥 HOT` alert,
      - `customer_label_history` has one new row with
        `to_label='HOT', signal='money_mentioned'`,
      - `customer_facts.label='HOT'`.
- [ ] Verify the no-op path: send a flat "thanks" — no alert, no
      history row, label unchanged.
- [ ] Commit: `feat(workflow): label-eval branch + HOT alerts`.

### Step 3 — Manual commands `/label`, `/snooze`, `/info` (0.75 h)

- [ ] Add `/label`, `/snooze` endpoints in bridge (§2c, §2d). Add
      `/lead-info` (§2e).
- [ ] `scripts/build_label_commands.py` — extends `Process Text
      Reply`, adds 3 `Route Text Action` outputs + 3 HTTP nodes +
      3 Telegram reply nodes.
- [ ] Dry, deploy, verify by Telegram-DM-ing the bot:
      `/label +971509767187 PAUSED_SPAM` → confirm reply, row,
      history line with `created_by='operator'`.
- [ ] Commit: `feat(workflow): /label /snooze /info commands`.

### Step 4 — Daily digests + cold-decay sweep (1.0 h)

- [ ] Add `/digest` endpoint in bridge (§2b). Returns the structured
      payload AND the pre-rendered `telegram_text`.
- [ ] Create `workflows/digest.json` with the 3 schedule triggers
      (§3c) + cold-decay sweep (§3d). Activate via n8n API or
      manually in the UI.
- [ ] Verify with a one-off manual trigger of one schedule node →
      digest lands in admin chat.
- [ ] Set the schedule triggers to active; document the cron values
      in `docs/STATUS.md`.
- [ ] Commit: `feat: 3× daily digests + cold-decay sweep`.

### Self-improvement v1 — DEFERRED to a follow-up build session

Track HOT-conversion outcomes weekly: a Sunday cron calls
`/digest?window=weekly_review` and Hermes summarises which HOT
labels (a) led to a payment_link_sent within 48 h, (b) did not.
Surface "I've been wrong about X" patterns to the operator. The
operator can correct via `/feedback`. **Build later** — needs the
HOT-conversion data accumulated first (~2 weeks of live traffic).

---

## 5. Open questions for operator

These are required answers before build, surfaced now to avoid mid-build
churn.

1. **`/lead` command collision.** The existing `/lead <phone>` creates
   an outbound first-contact draft (FR-2). The spec's `/lead <phone>`
   = full context view collides. Recommend: use `/info <phone>` for
   the context view; keep `/lead` for outbound. Confirm?
2. **HOT-alert chat.** Alerts go to the same admin chat (5532831477)
   as draft cards, OR to a separate "alerts" chat? Default plan
   assumes same chat — risk of mixing alerts with approvals during
   busy stretches.
3. **NEEDS_ATTENTION "no draft yet" detection.** We don't currently
   write a Redis flag when a draft posts. Either (a) add
   `SET draft:{customer_id}:posted 1 EX 1800` in
   `Send Draft to Telegram`, or (b) approximate via "no
   pendingQueue draft for this customer". Option (a) is cleaner;
   adds one Redis call per draft.
4. **"Operator replied within 30 min" detection.** No table currently
   logs operator-approved sends with timestamp. Options:
   (a) add an `INSERT INTO operator_sends (customer_id, sent_at)`
   call in `Send Reply via WhatsApp`; (b) infer from
   `autonomous_sends` for AUTO mode + a new column. Plan needs one
   of these wired in Step 2.
5. **Composite vs single-signal HOT.** Spec says "money/budget
   mentioned" is HOT on its own. False-positive risk on customers
   complaining about price. Recommend single-signal HOT for v1
   (operator can `/label` down), revisit after 2 weeks of data.
6. **Timezone.** Confirmed Dubai = UTC+4 year-round (no DST).
   Cron values above assume that. Confirm.
7. **PAUSED label TTL.** Default 7 days on `/label … PAUSED_*`.
   PAUSED_SPAM probably should be permanent. Confirm whether to
   default PAUSED to forever (-1) and require `/snooze` to relax.
8. **Digest content cap.** If 40+ leads are HOT, the morning digest
   could exceed Telegram's 4096-char message limit. Truncate to
   top-10 by `label_updated_at DESC` and append "+N more — /info to
   drill"? Default plan does this.

---

## 6. Edge cases handled

| Case | Handling |
|---|---|
| `/label-eval` called and bridge is down | `onError: continueRegularOutput` on Label Eval node → label unchanged, no alert, card still posts (same fail-open as Customer Facts; verified in Test 5 of header feature) |
| Two messages arrive within debounce window | `Label Eval` runs once per Build Prompt fire (post-debounce); no double-count |
| Operator manual `/label` while autonomous draft in flight | Manual write wins (locked_until set 7d for PAUSED_*); next auto-eval respects the lock |
| `customer_facts` row absent (first-ever message) | `compute_label` returns `NEW` with `signal='new_window'`; history gets a row with `from_label=NULL` |
| Cold-decay sweep on a customer who just messaged | Sweep uses `updated_at > 7 days`; the message just updated it, so they're skipped |
| HOT alert spam when one customer sends 5 hot messages in a row | History stays append-only, but the alert only fires on **transition** (`changed: true`). If already HOT, `alert_required=false`. |
| Digest job fires while bridge is restarting | Schedule Trigger → HTTP Request fails → no Telegram message sent (no half-rendered digests). Next digest at the next cron tick. |

---

## 7. Rollback path

Per-step rollbacks, ordered most-recent-first:

1. **Digests/sweep (Step 4):** deactivate the `digest.json` workflow
   (n8n UI toggle). No DB rollback needed.
2. **Manual commands (Step 3):** restore the `PRE-LABELCMDS`
   workflow backup via `safe_put`. Bridge changes can stay
   (endpoints unused).
3. **Customer-message branch (Step 2):** restore `PRE-LABELBRANCH`
   backup. Customer messages flow as before through Customer Facts →
   Build Prompt.
4. **Schema (Step 1):** keep the columns/table — they're additive,
   zero impact on existing reads. Only drop if the entire feature
   is being abandoned: `DROP TABLE customer_label_history; ALTER
   TABLE customer_facts DROP COLUMN label, DROP COLUMN
   label_updated_at, DROP COLUMN label_locked_until, DROP COLUMN
   label_locked_reason;`

Full feature kill-switch: deactivate the digest workflow + restore
the customer-message branch backup. The schema and bridge endpoints
remain harmless and ready for re-enable.

---

## 8. Dependencies on other plans

- **No hard dependency** on 7b (follow-up engine) or 7c (lead
  intelligence). Each can ship independently.
- 7b **consumes** `customer_facts.label` to pick trigger thresholds —
  if 7b ships first, it has to default-to-WARM until 7a lands.
- 7c **consumes** the same `label` column and the
  `customer_label_history` table in its `v_lead_summary` view.

---

## 9. Files this plan will touch (when built)

- `db/migrations/001_customer_labels.sql` — new
- `hermes-bridge/server.py` — `+~250` lines (5 endpoints + helpers)
- `workflows/phase-1b-telegram.json` — `+~10` nodes
- `workflows/digest.json` — new (~7 nodes)
- `scripts/build_label_eval_branch.py` — new
- `scripts/build_label_commands.py` — new
- `scripts/build_digest_workflow.py` — new (or hand-import the JSON)
- `docs/STATUS.md` — append a "Lead Tracker" section
- `docs/lead-tracker-plan.md` — this file (kept as the implementation
  reference; tick boxes as steps land)

**End of plan 7a.**
