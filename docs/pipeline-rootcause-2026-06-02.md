# Hermes /review Pipeline — Definitive Root-Cause & Permanent-Fix Report

**Date:** 2026-06-02 · **Scope:** the recurring "/review analysis" bugs (false identity merges + post-event mis-rendering) · **Status:** READ-ONLY investigation — no prod data/code/labels changed. Produced by an 11-agent adversarially-verified workflow (`wf_2edcf8cc-9df`); raw output at `tasks/w4qs7e6b5.output`.

---

## Executive summary

Two **independent** root-cause families explain every symptom. They need separate fixes.

**Family A — Identity reconcile trusts WhatsApp's contact directory blindly.**
An identity-merge step asks WAHA "what phone is behind this WhatsApp ID?" and, whatever WAHA answers, merges two customer records into one — with **zero check** that the two records are the same human (no name / yacht / party-size / date comparison). The record with more messages always wins. So one bad WAHA mapping silently collapses two different people into one card, and the absorbed person's card disappears from /review. It keeps coming back because every past fix tuned *which WAHA endpoint to trust* and never added a *"are these two actually the same person?"* gate.

**Family B — A booked deal has no real "finished" state and no fixed booking date.**
"CONFIRMED" is forced to mean both *booked-and-upcoming* and *booked-and-already-happened*. There is no WON/COMPLETED terminal state, and the booking date is stored as free text ("today", "May 28, 4–7 PM", "May 25 (completed), May 28 (proposed)") that is never pinned to a calendar date. 6+ pieces of code each re-guess "has the trip happened?" from that text with slightly different rules, so they disagree. Result: a paid cash customer was auto-killed to COLD, and won customers show contradictory "upsell / re-engage / Hermes: 0/100" guidance. It recurs because every patch added *one more guesser* instead of storing the answer once.

---

## What you saw vs the real cause

### Bug 1 — Qurbani/Zayn (Royalty 136) vanished into "Antonio" (Bliss 55) — Family A
- **Saw:** a high-value Royalty 136 lead disappeared from /review; only an Antonio / Bliss 55 card shows.
- **Cause:** WAHA's LID directory returns a phone for Antonio's WhatsApp ID that is actually **Zayn's** number. `lid_to_cus('274942918680787@lid')` live-returns `971509767187@c.us`. The reconcile handler treats that as proof they are the same person and merges, **with no fact check**. Antonio has 154 msgs vs Zayn's 12, so Antonio wins; Zayn's row is demoted (`merged_into` set). All reads/writes for Zayn now route to Antonio via `canonicalize_cid`; /review hides merged rows, so the card is buried — and ranked at Bliss 55's ~1,400/hr instead of Royalty 136's ~15,000/hr (so the rate-sort buries it instead of putting it on top).
- **Proof:**
  - `routes.py:3219` — canonical chosen purely by message count: `canon, dup = (lid, cus) if lmc >= cmc else (cus, lid)`.
  - `routes.py:3221` — the merge UPDATE; **the only `SET merged_into` writer in the codebase**.
  - `waha.py:324-341` — `lid_to_cus` returns WAHA's `.pn` verbatim, no cross-check.
  - Between canonical lookup (`routes.py:3210`) and merge (`routes.py:3221`): **no** name/yacht/party/date comparison.
  - DB: `971509767187@c.us` (Zayn / HOT / Royalty 136 / 3 guests / 12 msgs) `merged_into=274942918680787@lid` (Antonio / Bliss 55 / 154 msgs), updated 2026-05-30 08:17:54.
- **Adversarial corrections (honored):**
  - The merged-row hide filter lives in `routes.py:2498/2623/2711` as `merged_into IS NULL`. `review.py` has **zero** `merged_into` references (an earlier draft cited review.py — wrong file). The *effect* (merged rows hidden) is correct.
  - The Zayn→Antonio merge has **no `auto:identity_merge` history row** → it was **not** the hourly cron; provenance is migration-006 / a manual UPDATE. The mechanism (no validation gate) is identical; the cron attribution is not.
  - Zayn's row carries an `HCTEST-…` health-check seed in its history → **confirm it is a real customer before relying on the un-merged card.**

### Bug 2 — Tal Sudai went NEW → COLD despite paying cash — Family B
- **Saw:** a customer who paid 7,600 cash ("we're on the way") ended up COLD / dead-lead.
- **Cause (two compounding gaps):**
  1. Booking date stored as the literal word **"today"**, never converted to a calendar date. Parser can't read it (`_parse_booking_date('today') → None`), so every passed-date safeguard is silently skipped; the LLM analyzer saw 145h silence + an unplaceable date → concluded "event passed" → voted *close*, forcing score 0.
  2. The demote fired because Tal sat at **NEW** (in the allowed-from set), unlocked. He sat at NEW because **a cash deal has no path to CONFIRMED** — CONFIRMED requires a `payment_link_sent` row, which a cash deal never creates.
- **Proof:**
  - `labels.py:238-281` + `:171` — parser only matches month-name+day. `_parse_booking_date('today') → None`; `('Today (May 24)') → 2026-05-24`.
  - `server.py:1948-1954` — analyzer future-guard injected **only when** the date parses to a future date; "today" → None → guard never added.
  - `labels.py:294-310` — close-veto `_passed_date_close_is_wrong` returns **False** (fail-open) when the date won't parse.
  - `routes.py:3401-3415` — demote on `verdict==close or score==0`; allowed-from `{NEW, WARM, HOT, NEEDS_ATTENTION}` (NEW included), no lock → NEW→COLD on 2026-05-30 13:03:12.
  - `server.py:2654-2658` — CONFIRMED only via `PAYMENT_CONFIRMED_RE` AND a recent `payment_link_sent`; no cash path.
  - `server.py:1849-1868` — `upsert_customer_facts` writes `facts.get("dates")` **verbatim** (the un-anchored capture site).
  - DB: Tal `178249900498984@lid` label=COLD, dates='today', score=38, `label_locked_until` NULL, **0 autonomous_sends**.
- **Adversarial corrections (honored):**
  - Currently-COLD-with-relative-date is **≥3**, not 1: Tal ('today'), `279958920409314@lid` ('today (1d ago, unconfirmed)'), `243237000331461@lid` ('this week (flexible)'). A 4th, `234698957684753@lid` ('ثاني يوم العيد / Eid day 2'), is an **Arabic** relative date the English-only regex won't even match — the relative-date capture fix will miss it; flag for manual review.
  - The close-veto (`db5d49e`) deployed 2026-06-02 12:05, **after** Tal (05-30), the Jun-4 lead (05-31), and the Jun-3 lead (06-01) were closed. At Tal's close there was **no veto at all**. Two genuinely-future, parseable bookings were *also* auto-closed pre-veto → the analyzer-hallucinates-passed-date problem is broader than relative dates.

### Bug 3 — Émilie & Saif (won, past trips) show wrong guidance — Family B
- **Saw:** completed bookings show "upsell / confirm logistics / re-engage / wait for reply", several yachts at once, and a meaningless "Hermes: 0/100" or "35/100".
- **Cause:** the CONFIRMED card renders **active-deal guidance with no past/future date check**, even though the *button* underneath is date-aware → button and body contradict. No single booked-yacht field; the displayed score is an open-sale priority number meaningless for a won customer.
- **Proof:**
  - `review.py:706-715` — for CONFIRMED, the 🧠 line **unconditionally** appends "booked & paid · confirm logistics or upsell" (no date branch). Verbatim verified.
  - `review.py:919-922` + `:948` — `_why_line` returns stale `suggested_action` verbatim (Saif's "Wait for reply… Re-engage…") or defaults to "booked / paid — share boarding details or upsell". Date-blind.
  - `review.py:807` — prints `row.get('yachts')` verbatim → Émilie shows all 3 (Bliss 55, Sunseeker Satoshi 70, Pershing 82).
  - `server.py:1891` — extractor prompt: "yachts: every yacht name discussed" → accumulator by design; `_merge_facts` (`review.py:273-282`) never prunes.
  - `review.py:396-397` — `score_lead` returns a flat 5000 for CONFIRMED *for sort only*; the number **displayed** (`review.py:706-707`) is the open-sale `importance_score` (→ Émilie 0, Saif 35).
  - `review.py:828-835` + `labels.py:109-118` — only the **button** verb is date-aware (`_cf_passed`); the body text is never branched on it.

---

## Blast radius (how many others are silently affected)

- **Family A:** 137 customer_facts rows; 24 merged children. Of those: 16 likely-same-human, 5 suspect (yacht conflict, all same-human on inspection), 3 name-conflict. **Cross-human false merges needing un-merge: 1 confirmed (Zayn→Antonio).** Pipeline-contaminating merges (active lead buried): **≥2** — Zayn(HOT) under Antonio(HOT), and `971501166671@c.us` (WARM) buried under `70489087193105@lid` (DISREGARDED). **False splits:** 1 group — "Antonio" = `274942918680787@lid` + `31890199318638@lid`. Name/label data-loss from worse-row-wins: ≥3 (Zayn→Antonio, Shanebabu→Shane, unknown→+44…). The hourly cron itself has done **5 merges, all benign** (same-human, no cross-human, no burial) — so the cron is lower-risk than the migration/manual path, but every new @lid hourly is a fresh unguarded candidate.
- **Family B:** 11 auto-close transitions citing a relative/"passed" date; **7 customers currently COLD via auto:analyzer_close / analyzer_score0**; ≥3 currently COLD with a bare relative date stored. The **cash-can't-reach-CONFIRMED** gap is open for **all cash deals system-wide**. Of 5 CONFIRMED canonical rows, **≥3 already mis-served**: Mohammed (parser anchors the 'completed' May 25 token, ignoring the May 28 proposed date), Émilie (3 yachts, no canonical booked yacht; score 0 + past date), Saif (past date but score 35). Blast radius grows with every future won booking.

---

## The permanent fix

Tag legend: **BRIDGE-ONLY** = safe to deploy via scp → `py_compile` → drift-diff → swap → `systemctl --user restart hermes-bridge` → health. **N8N-LIVE** = customer-facing / changes label state or the /review view → do *with* the operator, rollback ready.

### Family A — make merges prove same-person
- **A-fix-1 (BRIDGE-ONLY): Fact-consistency merge guard.** In `handle_reconcile_identities`, between canonical lookup (~`routes.py:3210`) and the UPDATE (~`3221`), fetch name/yachts for both rows and **block the merge when both names are non-empty real names that differ** (treat `''`, `'unknown'`, `'+<phone>'` as empty). **Hard-block on *name* conflict only — NOT on yacht conflict alone** (adversarial scan found legit same-name pairs with zero yacht overlap, e.g. Mohammed "Royal Mirage" vs "Azimut 62, Satoshi 70"; a yacht gate would wrongly block them). Same-name = strong ALLOW. Return a `conflicts[]` list so the operator sees why a merge was skipped. Fail-closed (only prevents merges).
- **A-fix-2 (BRIDGE-ONLY): Canonical by booking, not message count.** When a merge IS valid, prefer the CONFIRMED / booked row as survivor over raw message count, so a booking is never buried under a chattier non-booked row. (`routes.py:3219`.)
- **A-fix-3 (BRIDGE-ONLY): WAHA round-trip plausibility check.** After `lid_to_cus` (~`routes.py:3205`), re-resolve the returned `@c.us` and **quarantine** the candidate if it maps back to a *different* `@lid`. Defends against the corrupt `274942918680787@lid → 971509767187` mapping regardless of WAHA state.
- **A-fix-4/5 (N8N-LIVE): Control the trigger.** Confirm whether an n8n cron calls `/reconcile-identities` (bridge side is manual-only: `server.py:3388`, surfaced at `routes.py:545` — no bridge scheduler). If a cron exists, **pause it first**; re-enable only after A-fix-1..3 are live, or replace it with an operator-confirmed review flow that surfaces `conflicts[]`.

### Family B — anchor the date, name the yacht, add a "done" state
- **B-fix-1 (BRIDGE-ONLY): Anchor relative dates at capture.** At `server.py:1849-1868` (`upsert_customer_facts`) add a pure `labels._resolve_relative_date(raw, captured_at)` ("today"→msg date, "tomorrow"→+1d, weekday→next match), resolved against the **customer-message timestamp** in **Asia/Dubai** (box is UTC; near-midnight bookings must use the local calendar day). Store the resolved ISO date; keep raw text in `dates_raw` for display. This single change re-arms the parser, the analyzer future-guard, and the close-veto for the whole relative-date class. *Known gap:* English-only `_RELATIVE_DATE_RE` won't catch Arabic — flag those for review.
- **B-fix-2 (BRIDGE-ONLY, behavior-changing — confirm): Demote guard.** At `routes.py:3401-3415`, before demoting: (a) **skip** if the lead is booked/paid — `autonomous_sends` has `payment_received`/`payment_link_sent`, OR history ever reached CONFIRMED/WAITING_FOR_PAYMENT; (b) when reasoning claims "passed" but the date is unparseable, **do not close** — route to a reconfirm-date nudge. `apply_label_transition` (`server.py:2733-2753`) is DB-only → never sends a message → approval-first intact.
- **B-fix-3 (BRIDGE-ONLY): Analyzer guard for unresolved relative dates.** At `server.py:1948-1954`, when the date is relative and couldn't be anchored, inject "date cannot be resolved — do NOT assume it passed; treat as unknown, keep open." Removes the LLM's ability to infer "passed" from silence.
- **B-fix-4 (N8N-LIVE — schema + new label + auto flip): Canonical booked anchor + WON/COMPLETED terminal.**
  - Migration (BRIDGE-ONLY, additive/nullable, safe online): `ALTER TABLE customer_facts ADD COLUMN booked_date date NULL, booked_yacht text NULL, completed_at timestamptz NULL;`
  - Add `COMPLETED` to **all four** enumerations in one deploy — `labels.py:25` (LABELS), `:40` (`_LABEL_RANK`), `:55` (`_HARD_DEMOTE_SIGNALS`), `:75` (`_DORMANCY_FROM_OK`) — ranked ≥ CONFIRMED, below DISREGARDED. (Miss one → KeyError / COMPLETED leaks into dormancy-close.)
  - One pure detector `labels.event_passed(row)` (prefers `booked_date`, falls back to `_parse_booking_date(dates)`) **replaces the 6+ ad-hoc copies**: `review.py:103`, inlined `review.py:828-835`, `server.py:2599-2605`, `labels.py:98-106`, `server.py:2613-2634`, `labels.py:313`.
  - Capture the anchor at **all four** CONFIRMED-promotion sites: `routes.py:1129`, `routes.py:1199`, `server.py:2654-2658`, **and `routes.py:5709` (nomod-webhook — the dominant prod path; Antonio & Luke came through here, the earlier 3-site plan missed it)**. The webhook payload carries the charge → best place to record `booked_yacht`/`booked_date`.
  - One place flips CONFIRMED→COMPLETED: the hourly sweep (`routes.py:4081-4095`) when `event_passed(row)`.
- **B-fix-5 (BRIDGE-ONLY): Post-event rendering + single yacht + suppress open-sale score.**
  - `review.py:706-715` — hoist `event_passed` above the 🧠 line; if passed, render "✅ event complete · nurture for repeat / referral (no upsell)" and replace "Hermes: X/100" with a "🏆 won" tag. Keep upsell text only for **future-dated** CONFIRMED (Antonio/Jun 20 stays correct).
  - `review.py:919-922,948` — gate `_why_line` on `event_passed`; ignore stale `suggested_action` for past trips.
  - `review.py:807` — display a single `_booked_yacht(row)` (column if set, else highest-rate fallback, else raw list with "⚠️ confirm which yacht").
  - Keep CONFIRMED/COMPLETED in their **own** /review section (do not re-route to NOT_A_CUSTOMER); keep the reply-badge suppressed (`review.py:797-798`).
- **Tests:** `_resolve_relative_date` (today/tomorrow/weekday/Dubai-midnight/Arabic-unmatched), demote-guard (booked/paid skip + unparseable-no-close), `event_passed` (past/future/unparseable), merge-guard (name-conflict block, same-name allow, round-trip quarantine), render (past vs future CONFIRMED, single-yacht).

---

## Immediate data remediation (reversible, operator-run against prod)

**1. Un-merge Zayn/Qurbani from Antonio** (after confirming it's a real customer, not the HCTEST seed):
```sql
UPDATE customer_facts SET merged_into = NULL, updated_at = now()
WHERE customer_id = '971509767187@c.us' AND merged_into = '274942918680787@lid';
-- Reverse: SET merged_into = '274942918680787@lid'
```
**2. Re-merge Antonio's split rows — DO NOT auto-merge.** The two Antonio rows resolve to **different** WAHA phones (971509767187 vs 971588211789) → no code can safely unify them. Operator confirms they are one human first, then (CONFIRMED row should win so the booking isn't buried):
```sql
-- ONLY after operator confirms direction:
UPDATE customer_facts SET merged_into = '31890199318638@lid', updated_at = now()
WHERE customer_id = '274942918680787@lid';
-- Reverse: SET merged_into = NULL
```
**3. Re-label Tal Sudai** — operator confirms whether the cash trip ran (COMPLETED if ran / CONFIRMED if pending / LOST if fell through):
```sql
UPDATE customer_facts SET label = 'CONFIRMED', updated_at = now()   -- or 'COMPLETED'
WHERE customer_id = '178249900498984@lid';
-- Reverse: SET label = 'COLD'
```
Same per-lead judgement for `279958920409314@lid` and `243237000331461@lid` (other relative-date COLD victims).

**4. Correct Émilie & Saif to post-event** — no label change needed; B-fix-5 render fixes the cards automatically. Optionally seed the anchor (after B-fix-4 migration):
```sql
UPDATE customer_facts SET booked_date='2026-05-28', booked_yacht='<the actually-paid yacht>'
WHERE customer_id='137813169274972@lid';   -- Émilie (operator picks which of the 3)
UPDATE customer_facts SET booked_date='2026-05-24', booked_yacht='Sunseeker Satoshi 70'
WHERE customer_id='253570691645643@lid';   -- Saif
-- Reverse: set columns back to NULL
```

---

## Rollout plan (ordered by risk; READ-ONLY until operator approves each step)

1. **Pause the reconcile trigger** (A-fix-5, reversible) — stops new false merges. *First.*
2. **Bridge-only Family-A code** (A-fix-1/2/3, fail-closed) — only *prevents* merges. Lowest risk.
3. **Data remediation #1 (un-merge Zayn)** — single reversible UPDATE, after confirming real customer. #2 (Antonio split) only on operator confirmation.
4. **Bridge-only Family-B logic** (B-fix-1 anchor, B-fix-2/3 guards) — pure logic, no migration. Confirm fail-closed-on-unparseable (less auto-cleanup of truly-dead leads) is acceptable; `/Disregard` still works manually.
5. **Data remediation #3 (re-label Tal + 2 others)** — per-lead, reversible.
6. **Schema migration** (B-fix-4 columns) — additive/nullable, safe online; bridge falls back to raw text if columns absent.
7. **WON/COMPLETED label + render** (B-fix-4/5, N8N-LIVE) — all enumerations in one deploy; operator eyeballs the 5 backfilled anchors before enabling the CONFIRMED→COMPLETED flip; operator chooses separate "✅ COMPLETED" section vs tagged sub-state.
8. **Re-enable reconcile** (optional) once A-fix-1..3 are live, or replace with an operator-confirmed review flow.

---

## Open questions for the operator
1. **Is Zayn/Qurbani a real customer or partly a test fixture?** History starts with an `HCTEST-…` seed — confirm before relying on the un-merged card.
2. **Why does WAHA map `274942918680787@lid` → `971509767187`?** Stale/recycled LID, a Business profile sharing a number, or a WAHA bug? Fix defends regardless, but the source should be reported.
3. **Are the two Antonio rows one human?** Different authoritative phones → needs your confirmation + which row is canonical (the CONFIRMED one holds the booking).
4. **Did Tal's cash trip run?** → COMPLETED vs CONFIRMED vs LOST. Same for `279958920409314@lid`, `243237000331461@lid`, and the other analyzer-COLD customers.
5. **How should cash bookings be represented?** A first-class CONFIRMED path via an operator "mark cash-confirmed" button, or a new `BOOKED_UNVERIFIED` state? (Auto-promoting cash language to CONFIRMED brushes approval-first for label state — not recommended.)
6. **For Émilie / Mohammed:** which single yacht was actually booked? (Émilie has 3 listed; Mohammed's date is "May 25 (completed), May 28 (proposed)".)
7. **Should COMPLETED auto-archive after the feedback+review cycle, or remain a permanent won-history state?**
