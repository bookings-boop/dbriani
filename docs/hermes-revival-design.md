# Hermes Revival — Re-integration Design (Phase 3)

**Status:** design — 2026-05-21. Phase 1 found **no Hermes latency bug**
(drafts ~13s; the rollback slowdown was critical-path concurrency). This
document designs the re-integration per the **FR-5 refined model**: Hermes
**off the critical path**, as a background improver + learning brain.

See `docs/feature-backlog.md` (FR-5) and `docs/decisions.md` (Phase 1).

---

## Architecture

```
FAST PATH  (unchanged — the live 52-node workflow):
  customer msg → debounce → Claude drafts (~10s) → draft card → operator approves → sent

BACKGROUND IMPROVER  (new):
  draft card posted → wait ~20s → still pending? → Hermes reviews + improves
                    → if meaningfully better, card updated in place ("✨ improved")
                    → bounded repeat while still pending

LEARNING LOOP  (new):
  operator Sends / Edits / Skips → signal to Hermes
                                 → Hermes derives behaviour rules (INACTIVE)
                                 → operator approves rules (digest review)
                                 → approved rules feed future drafting + improvement
```

The fast direct-Claude draft is **never blocked by Hermes**. If Hermes is slow
or down, the operator still has the fast draft and the system is unaffected.
Additive and fail-safe — this is the structural fix for the rollback incident.

---

## Component A — background improvement loop (n8n)

A new branch off `Save Telegram MsgID` (today a dead-end node):

| Node | Type | Role |
|------|------|------|
| `Improve Wait` | wait | ~20s — gives the operator first look; instant Sends skip the whole branch |
| `Check Pending` | code | proceed only if the draft is still `pending`; stop if sent / skipped / awaiting_edit |
| `Hermes Improve` | httpRequest | POST the bridge `/improve` — current draft + customer message + history |
| `Apply Improvement` | code | if Hermes returned a better draft, update the queue entry; else end |
| `Edit Improved Card` | httpRequest | `editMessageText` — re-render the card with the improved draft, re-attach the 4 buttons, mark `✨ improved` |

`Apply Improvement` loops back to `Improve Wait` — **bounded to ~2 passes** so a
lingering draft is not churned indefinitely. The card-render mirrors the
existing `Edit Telegram (Regen)` / `Parse Refine` nodes.

**Race handling:** `Check Pending` runs immediately before the Hermes call, and
`Apply Improvement` re-checks `pending` before editing the card — so a draft the
operator just Sent/Skipped is not overwritten. Worst case degrades to a no-op.

## Component B — bridge `/improve` endpoint

The `hermes-bridge` (already running on the box) gains one endpoint:

```
POST /improve
  in : { draft, customer_message, history }
  run: hermes chat — "review this draft against Maria's rules + the
       conversation; if you can MEANINGFULLY improve it, return a better
       version, else return it unchanged" — with -t memory (draws on learned
       rules + memory)
  out: { improved: bool, messages: [...], note }
```

`improved:false` ⇒ the workflow leaves the card untouched (no churn).

## Component C — learning loop

Signals, captured from the operator's existing actions:

| Operator action | Signal |
|-----------------|--------|
| **Send** as-is | weak positive — "this draft worked" |
| **Edit** (FR-1 feedback) | the feedback text *is* the lesson |
| **Skip** | negative — "this draft was wrong" |

Each signal → bridge → Hermes records it → `behavior_rules` (Postgres,
**`active=false`**). The operator reviews + approves rules in a periodic digest;
approved rules are injected into future drafting and the `/improve` pass. This
reuses the overnight build's `behavior_rules` table and `/save-rule` code
(the Hermes Postgres tables still exist; only the workflow was rolled back).

---

## Decisions made
- **Hermes off the critical path** (FR-5 refined). The fast path is untouched.
- Improvement **updates the card in place** with a `✨ improved` marker.
- **Bounded passes** (~2) — diminishing returns; never churn the card.
- Learned rules default **INACTIVE until the operator approves** (project rule
  — no autonomous behaviour change).
- Hermes is reached via the **existing bridge** (HTTP), never n8n→shell.
- Autonomous mode (FR-4) stays a **separate, later** track — not bundled here.

## Resolved + open
- **"Check all skills" — RESOLVED (operator, 2026-05-21):** the improver draws
  on **all our knowledge** — the full system prompt (pricing, yachts, packages,
  hard rules), the conversation history, learned behaviour rules, and Hermes's
  memory. No separate skill-module system; `/improve` simply receives the full
  context. (Simplifies Phase 4.)
- Rule-approval cadence — assumed: a periodic digest of pending rules to
  approve, not per-rule interruptions. Confirm if you'd prefer otherwise.

## Phase 4 — build scope
1. **Bridge** — add `/improve` (and a `/learn` signal endpoint) to `server.py`;
   restart `hermes-bridge`.
2. **Workflow** — ~6 nodes for the improver branch off `Save Telegram MsgID`;
   learning-signal hooks on `Mark Sent` / `Mark Skipped` / the FR-1 refine.
3. **Test** — exercise the bridge `/improve` by hand → wire into the workflow
   (dry-run + staged validation, as with FR-1/2/3) → behavioural test.
