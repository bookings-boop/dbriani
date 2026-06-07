# Hermes Master Fix Plan — 2026-06-07 (consolidated from 8 deep-analysis workflows)

Governing rule (operator): all analysis done first → ONE coherent plan → execute **once, solid**, no piecemeal/refix. Single source of truth = this repo. n8n edits via `safe_put` + repo-commit + deploy-guard (never `deploy_bridge` for node/prompt-only). Financial = daytime + supervised + show-before-deploy. Autonomy = last, gated.

## THE KEYSTONE (Layer 1) — durable conversation store
`wkk8tao60` proved the deepest root: **no durable transcript exists** — every `hermes_analyze_lead` (and the drafter) runs on a *live WAHA fetch* that evicts/truncates. Measured (102 active leads): **8% empty, 26% sparse, 17% degraded**; Émilie 157 msgs → WAHA 0 → 0/100; 31% scored 0; 263 timeouts + 23 no-JSON/5d.
**Fix:** append-only `conversation_messages(customer_id, ts, direction, body, msg_id)` written on EVERY inbound + outbound; analyzer/drafter read it (topped by WAHA); backfill once. This single fix simultaneously: corrects analysis quality, prevents AI-failure lead-drops (record-before-AI), gives the drafter full context (Charlie), powers the never-miss net, de-risks autonomy. **Highest leverage.**

## Sequenced waves (dependency order)

### WAVE 1 — operator-visible /review fixes (batch-1, ready, non-financial) — SHIP FIRST
- **UNANSWERED pinned to TOP, uncapped, always** — any owe-reply lead (never-replied OR customer-last), independent of temp/label/score. **DoD: Erika + +971568241103 appear at top.**
- Émilie out of top + won-render (`analysis_guard.is_stale_relative_date` + score-0); Zayn visible (rank weights engagement, not just yacht-rate).
- LOST shown as "lost sale", not "not a customer"; SCAM label (Mike) via `reclassify_close_label`.
- Reengage-reopen regression: operator-close STAYS closed (`_is_operator_close`); intent regex tightened (over/under-match); dampening honored; `_merged_label` default-NEW guard.
- Wire `analysis_guard.is_analysis_unreliable` → discard/flag degraded; CONFIRMED never scored as convertible (card too, not just /info); needs-reply correctness.
- **Gate:** review diff + full suite + live `/review` acceptance checks, then deploy bridge bundle (confirm push).

### WAVE 2 — durable store + never-miss (the foundation) [`wkk8tao60`,`wffn6pwdr`]
- Build `conversation_messages` + write-path + analyzer/drafter read-path + backfill.
- **Record-before-AI:** upsert the lead BEFORE the Claude node so a credit/timeout/error can NEVER drop it (root of +971568241103; credits were a transient — verified not active now).
- Bulletproof intake net: page ALL chats (not limit=150 of 585), AUTO-INGEST (not alert-only), one-tap card, 5–15 min cadence, no 7-day cap for never-ingested, fail-CLOSED + alert-if-net-down, @lid handling.

### WAVE 3 — regen + n8n-content (safe_put + repo-commit + deploy-guard) [`wnqgmlaay`]
- Apply-Improvement: deterministic DRAFT-section rebuild from `g.messages` (no verbatim dependency, kills "improved-draft-under-notes" + what-I-see≠what-sends); commit to repo JSON; `deploy_bridge` invariant assert (must contain rebuild marker, never `'improved draft:'`).
- #16b pass `system_prompt` to judges; #19 Build-Nudge-Card honor `{excluded}`.

### WAVE 4 — duplicate follow-up + booking-detail [`wixem3k52`, `wh5td0krq`]
- Milena dedup: ONE owner (`/hourly-sweep` returns `eligible_followups:[]` behind flag) + atomic `reengage:claim:<cid>` NX + honor `no_state_bump` (cap-burn). + situation-summary line on follow-up cards.
- Booking-detail: capture time + add-ons at extraction; itemized card (date+time+yacht+addons).

### WAVE 5 — analysis-quality layers (on the store) [`wkk8tao60`]
- Truncation window includes earliest booking-signal + recent; absolute-date normalization at capture (fixes the 4b VETO); fully wire `analysis_guard` (discard degraded, SCAM, stale-date veto); retry the 263 timeouts; never demote a booked/paid lead on a None result. Measurement dashboard (WAHA-empty%, unreliable-trips, score-0-among-CONFIRMED must be 0).

### WAVE 6 — FINANCIAL (supervised, show-before-deploy) [`wnbc8dxbc`]
- Paylink dup-guard (built local) deploy; payment match-buttons (wire inert helpers + `/paymatch-confirm` re-keying the unmatched row + n8n branch); Feature B (`booking_lines` migration 010, balance, `#8-SUM = total_paid`, top-up link); blank payment-event card → show booking+amount; #10b unified as the held-payment gate.

### WAVE 7 — AUTONOMY (gated, last) [`wm89w0zfp`]
- **R0 PREREQ (safety):** dispatched text == floor-scored text (audit #4) — COMMIT returns `draft_text`; `Auto Send WAHA` sends `Auto Commit.draft_text`. Validated by byte-equal on a single-customer canary (cannot be validated in shadow).
- Observability (log mode early-return, telemeter score-0, cap-fail alert).
- R3 scorer recalibration (eval: ≥90% approved ≥8) — not just drop the threshold.
- R2 arm-open-draft-on-enable + fix over-promising confirm copy; R4 narrow break-regex + scope 24h guard; R5 disarm race.
- Staged: shadow → single-customer canary (validates R0) → widen. **Never bulk `/auto all`** until metrics pass.

## Conflict map / future-proofing
- `review.py` is touched by Wave 1 only (coherent single owner) — analysis-quality (W5) touches server/analysis_guard, not the render, so no collision.
- Durable store (W2) is the foundation W5 + autonomy build on — land before W5.
- `analysis_guard` = the one deterministic guardrail layer (correct/flag); persisted reliability flag so sort/label/autonomy never trust a bad verdict.
- n8n (W3/W4/W6/W7) all via `safe_put` + repo-commit + the deploy-guard invariant → `deploy_bridge` can't revert.
- Financial (W6) fenced; autonomy (W7) gated behind R0 + recalibration. Repo == box == live verified after each wave.
