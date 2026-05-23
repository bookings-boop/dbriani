# `/feedback` Operator Command — Plan

> **STATUS: PLAN ONLY — awaiting operator approval before build.**

**Goal:** Operator types free-form `/feedback <text>` in the Telegram bot. Hermes classifies it as a customer note, global rule, or scenario rule; operator confirms via inline buttons; on Yes, it's stored and automatically picked up by future draft generations.

**Scope:** one new bridge endpoint, one new Postgres table, two new workflow nodes, plus the Hermes classifier prompt, plus loading behavioural context into the live draft prompt (currently *not* loaded — see decision D1).

---

## 1. Flow

```
Operator → Telegram: "/feedback never offer phone calls"
   ↓
Telegram Webhook → … → Process Text Reply → Route Text Action (new branch)
   ↓
Hermes Feedback Classify (NEW http) — POST bridge /feedback action=classify
   bridge: run_hermes with the classification prompt, returns JSON:
   { classification, customer_name?, scenario?, rule_text?, note_text?, summary }
   stores proposal in Redis (feedback:<proposal_id>, TTL 600 s)
   ↓
Send Feedback Card (NEW http, Telegram) — posts a confirmation card:

   📋 Feedback received
   "<the original /feedback text>"

   Classified: <CUSTOMER NOTE | GLOBAL RULE | SCENARIO RULE>
   → <summary>

   [✅ Yes]  [✏️ Modify]  [❌ No]
   ↓
Operator taps a button → Telegram Webhook → … → Parse Callback (existing,
   actions: feedback_yes / feedback_modify / feedback_no)
   ↓
Route Action — new branches:
   feedback_yes    → Feedback Save     (POST /feedback action=save)
   feedback_modify → Feedback Modify   (prompt operator for replacement text,
                                        text-reply flow)
   feedback_no     → Feedback Discard  (POST /feedback action=discard)
```

**Drafts use it:** add **`Fetch Behavioral Context`** (NEW httpRequest) before `Build Prompt`, pulling `{global_rules[], scenario_rules[], customer_notes[]}` for the current `customer_id`. `Build Prompt` interpolates the merged context into the systemPrompt under a new `## Behavioral context` section.

---

## 2. Data model

### New table `customer_notes`
```sql
CREATE TABLE customer_notes (
  id          serial PRIMARY KEY,
  customer_id text NOT NULL,
  note_text   text NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now(),
  created_by  text NOT NULL DEFAULT 'operator',
  active      boolean NOT NULL DEFAULT true
);
CREATE INDEX customer_notes_customer_idx
  ON customer_notes (customer_id) WHERE active = true;
```

### Existing `behavior_rules`
Already has `scope IN ('global','customer','scenario','tier')`. `/feedback` writes:
- **GLOBAL RULE** → `scope='global'`, `customer_id=NULL`, `active=true`.
- **SCENARIO RULE** → `scope='scenario'`, `scenario=<the scenario name>`, `active=true`.

> **Operator already confirmed via Telegram** ⇒ `active=true` immediately. The existing `/rules` (Hermes Rules) review flow stays for the *implicit* rule capture during refines (FR-5 learning loop) — those still go in as `active=false`.

### Redis (proposal staging)
```
feedback:<proposal_id>  STRING  — JSON of the classifier response, TTL 600 s
```

---

## 3. Bridge — `POST /feedback`

Single endpoint, dispatched by `action`:

| `action` | Body | Behaviour |
|---|---|---|
| `classify` | `{operator_id, text}` | `run_hermes` with the classification prompt (fast — short max-tokens, no tools); stores result in `feedback:<id>` (UUID), TTL 600 s. Returns `{ok, proposal_id, classification, summary, …}`. |
| `save` | `{proposal_id}` | GET proposal; INSERT into `customer_notes` or `behavior_rules` per `classification`; DEL the Redis key. Returns `{ok, table, row_id, summary}`. |
| `discard` | `{proposal_id}` | DEL the Redis key. Returns `{ok}`. |
| `behavioral-context` | `{customer_id}` | Reads `behavior_rules` (global + scope=scenario + scope=customer for this cid) **active=true** and `customer_notes` for cid active=true. Returns `{global:[...], scenario:[...], customer_notes:[...]}`. Used by **`Fetch Behavioral Context`** on every draft. |

**Hermes classifier prompt** (kept tight — fast model call, low tokens):
```
Classify this operator feedback into ONE of:
  CUSTOMER_NOTE — a fact about a specific named customer to remember.
  GLOBAL_RULE   — applies to ALL future drafts.
  SCENARIO_RULE — applies in a specific scenario only (proposal, birthday,
                  family group, corporate, B2B, …).
Return ONLY valid JSON, no prose:
  {"classification":"CUSTOMER_NOTE"|"GLOBAL_RULE"|"SCENARIO_RULE",
   "customer_name":"<name or empty>",
   "scenario":"<name or empty>",
   "text":"<the rule/note text, cleaned & imperative>",
   "summary":"<one short sentence for operator confirmation>"}
Feedback: <the operator's text>
```

For `CUSTOMER_NOTE` the bridge then resolves `customer_name → customer_id` via the **latest** `customer_facts` row matching the name case-insensitively. If 0 matches → return `needs_disambiguation:true` in the proposal; the Modify button lets the operator type the customer phone explicitly. If 1 match → use it. If >1 match → return the top 3 and let the operator pick via Modify.

---

## 4. Workflow changes

Three new branches off `Route Text Action` (the `/feedback ...` command) and three new branches off `Route Action` (the inline-button confirmations).

| Node | Type | Role |
|---|---|---|
| `Route Text Action` (modify) | switch | add output #7 = `/feedback` |
| `Hermes Feedback Classify` (NEW) | httpRequest | POST /feedback action=classify |
| `Send Feedback Card` (NEW) | httpRequest | Telegram sendMessage with Yes/Modify/No inline_keyboard |
| `Route Action` (modify) | switch | add outputs for `feedback_yes` / `feedback_modify` / `feedback_no` |
| `Feedback Save` (NEW) | httpRequest | POST /feedback action=save |
| `Feedback Modify` (NEW, optional v1) | code | sets a Redis "awaiting modify text from operator" flag |
| `Feedback Discard` (NEW) | httpRequest | POST /feedback action=discard |
| `Send Feedback Result` (NEW) | httpRequest | Telegram editMessageText — "✅ saved as GLOBAL RULE: …" or "❌ discarded" |
| **`Fetch Behavioral Context`** (NEW) | httpRequest | inserted between `Format Context` and `Build Prompt` — fetches `{global, scenario, customer_notes}`; `Build Prompt` reads it under a new `behavioralContext` assignment |

The error message in `Ack No Pending` / unknown-command path is updated to include `/feedback` in the valid-commands list.

---

## 5. Build sequence (separate commits, each with backup → dry-run → deploy → verify)

1. **Bridge — DDL + endpoint + classifier prompt + unit tests** (`customer_notes` table; `_feedback` handler; `test_feedback.py`).
2. **Workflow — text-command branch + confirm card** (add `Route Text Action` #7, `Hermes Feedback Classify`, `Send Feedback Card`).
3. **Workflow — callback branches + save** (`Route Action` additions, `Feedback Save` / `Discard` / `Send Feedback Result`).
4. **Workflow — load behavioural context into drafts** (`Fetch Behavioral Context` before `Build Prompt`; `Build Prompt` systemPrompt gets a new `## Behavioral context` section pulling from `{{ $('Fetch Behavioral Context').item.json }}`).
5. **End-to-end test:** three feedback flavours, then verify a fresh customer-msg draft picks the new context up.

---

## 6. Risks

- **Bad classification:** Hermes mis-labels a global rule as a scenario rule (or vice versa). Mitigated by the operator-confirm step (Modify / No). No silent storage.
- **Customer-name ambiguity:** "Mark" might match multiple customers. Mitigated by the 0/1/many fallback (operator types phone explicitly via Modify).
- **Behavioural-context bloat:** Many rules in `behavior_rules` could grow the system prompt. Mitigation: load only `active=true`; recommend max-N=20 cap (configurable env), oldest deactivated first.
- **Loading context on every draft:** adds one bridge call per inbound message. Bridge is local-network; should be <100 ms. `onError: continueRegularOutput` so a bridge hiccup never blocks the draft.
- **Modify flow timing:** the proposal Redis key has 600 s TTL. If the operator takes >10 min to Modify, the proposal is gone — they re-type `/feedback`. Acceptable.

---

## 7. Time estimate

| Step | Effort |
|---|---|
| 1. Bridge DDL + `_feedback` handler + classifier prompt + unit tests | 75 m |
| 2. Workflow command branch + confirmation card | 50 m |
| 3. Workflow callback branches (Save / Discard / Send Result) | 45 m |
| 4. `Fetch Behavioral Context` + `Build Prompt` integration | 45 m |
| 5. Test phone E2E (3 feedback types + drafts using the new context) | 35 m |
| Total | **~4.2 h** |

---

## 8. Operator decisions to confirm before Step 1

- **D1 — Behavioural context loading.** Confirm: a new `Fetch Behavioral Context` httpRequest before `Build Prompt`, results interpolated into a `## Behavioral context` block in the systemPrompt. *Recommend yes — this is what the task implies and how rules become draft-effective.*
- **D2 — Activate on confirm.** Confirm: `/feedback Yes` writes `active=true` immediately (skipping the `/rules` review flow). *Recommend yes — operator already confirmed via inline buttons.*
- **D3 — Customer-name matching strategy.** Recommend: latest `customer_facts` row matching the name case-insensitively. If 0/many, surface to operator via Modify (they type the phone). *Confirm or override.*
- **D4 — Active-rule cap per scope.** Recommend a configurable cap (e.g., max 20 active global rules; max 5 per scenario; max 5 customer notes per customer) — oldest deactivated when new ones land. *Confirm cap values, or leave uncapped for v1.*
- **D5 — Modify button v1 scope.** v1: simplest — Modify discards the proposal and prompts the operator to retype the `/feedback` command with corrections. v2 (later): operator replies with corrected JSON / free-text patch and the bridge re-stores. *Recommend v1 simple for this build.*
- **D6 — Visibility.** Should there be a `/feedback list` and `/feedback delete <id>` sub-command for review? *Recommend defer to v2; for v1, operator can use `/rules` for behavior_rules and manual DB inspection for customer_notes.*
