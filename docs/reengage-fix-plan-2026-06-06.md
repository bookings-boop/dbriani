# Reengage engine fix (BUG G) — build spec · 2026-06-06

**Scope chosen by owner:** Option A — coherent fix (G-Fix1 + G-Fix2 + G-Fix3a).
**Blast radius:** bridge-only (`hermes-bridge/server.py`, + trivial pass-through in `routes.py`). **No n8n PUT** — `silence_window` is an opaque passthrough in the workflow.
**Safety:** engine stays PROPOSE-only (operator-approval card, never auto-send). All existing anti-spam caps kept. TDD. Deploy = scp → py_compile → restart → health, after explicit go.
**Baseline:** HEAD `48e9406`, box == repo == remote (sha256 verified).

## Root cause (confirmed live)
`scan_followup_eligibility()` candidate SQL (server.py:3333-3335) selects the
**inverted** population — `last_operator_reply_at < last_customer_message_at`
= "customer spoke last, WE owe a reply". The reengage target is the opposite:
**we replied last, customer went silent (ghosted)**. The correct `we_replied`
flag (3318) is computed but never read (dead). Compounding: cold_lastshot
unreachable (G-2), dead-zone bands (G-3), hard one-shot gate (G-4).

## Cadence (silence measured since OUR last reply)
| Nudge | Trigger | Labels | Phrase |
|---|---|---|---|
| #1 soft | 24h ≤ silence ≤ 72h | HOT, NEEDS_ATTENTION, WARM | "Just checking in if you have any update…" |
| #2 last shot | 72h < silence ≤ 14d (336h) AND ≥48h since nudge #1 | HOT, NEEDS_ATTENTION, WARM, COLD | "Have you given up on booking a private yacht?" |

- Max 2 nudges / silence cycle (`FOLLOWUP_CAP=2`); cycle resets when customer replies (`followup_count→0`).
- NEW excluded (pre-qualification). COLD gets last-shot only (fixes G-2 reachability).
- Population drops out the instant the customer replies (`last_customer_message_at` passes `last_operator_reply_at` → `we_replied` false).

## Edits (server.py)
1. **`GHOST_RECOVERY_PHRASES` (315-323):** collapse to 2 keys — `soft_checkin`, `last_shot`. (Drops the old per-label `hot_30m_2h`/`hot_2h_24h` keys, which were built for the wrong basis.)
2. **`_silence_window_for(label, silent_hrs)` (3277-3291):** rebase bands on hours-since-our-reply → return `soft_checkin` (HOT/NA/WARM, 24-72h) / `last_shot` (HOT/NA/WARM/COLD, 72-336h) / None.
3. **Candidate SQL in `scan_followup_eligibility` (3313-3360):**
   - `silent_hrs` ← `EXTRACT(EPOCH FROM (now() - cs.last_operator_reply_at))/3600`.
   - WHERE population ← `cs.last_operator_reply_at IS NOT NULL AND (cs.last_customer_message_at IS NULL OR cs.last_operator_reply_at > cs.last_customer_message_at)`.
   - Timing band ← `now() - cs.last_operator_reply_at` BETWEEN 24h and 14d.
   - One-shot → 48h cooldown ← `(cs.last_nudge_drafted_at IS NULL OR now() - cs.last_nudge_drafted_at > interval '48 hours')`; keep `followup_count < FOLLOWUP_CAP`.
   - `already_drafted` column redefined as "nudged within 48h" (same parts[] index 7) → Python skip stays as a defensive guard (no index shift).
   - ORDER BY `cs.last_operator_reply_at ASC` (longest-ghosted first).
4. **routes.py consumer:** no logic change — new keys flow via `GHOST_RECOVERY_WINDOWS` (derived from the dict). Note text auto-updates.

## TDD
- **Refactor for testability:** extract the candidate SQL into a pure builder `_followup_candidate_sql()` so the keystone predicates are unit-assertable without a DB.
- **`_silence_window_for` (pure):** soft at 24/48/72h for HOT/NA/WARM; last_shot at 73h/14d for all-incl-COLD; COLD no-soft; NEW none; below 24h / above 336h → None; boundaries.
- **`_followup_candidate_sql` (pure):** asserts we-replied population (`last_operator_reply_at > last_customer_message_at`), band on operator reply, 48h cooldown present, one-shot predicate gone.
- **Regression:** test_reengage, test_dormancy, test_cold_decay, test_awaiting_section, test_followup_note all stay green. Full suite (64) green before deploy.

## Rollback
Single-file revert of server.py to `48e9406` + restart. No schema/migration, no n8n change.
