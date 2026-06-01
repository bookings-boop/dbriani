# Analysis Audit — #4 "audit all analyses" (2026-06-01)

Read-only audit of the lead-analysis layer. Goal: confirm whether **staleness**
is the root cause behind #2 (ended convos showing as AWAITING) and the
mis-bucketing the operator reported. **It is largely not** — see Finding 1.

Snapshot taken against the live DB (`n8n-postgres-1`, `customer_facts` +
`conversation_state` + `v_lead_summary`). No re-analysis triggered; aggregate
SELECTs only.

## Where analyses live
- **`customer_facts.importance_score` / `importance_reasoning` / `label` /
  `importance_analyzed_at`** — the scoring layer that drives `/review` tiers
  and the 💤 NO-ACTIVE-SALE bucket. `importance_analyzed_at` is the staleness
  anchor.
- **`conversation_state.last_analyzed_at` / `last_analysis_signal` /
  `last_analysis_confidence`** — a *separate, finer-grained* per-conversation
  signal layer (values: `sticky_hot`, `date_asked_no_commit`, `cold_decay`,
  `multi_yacht_engaged`, `new_window`, **`date_passed`**, **`confirmed_terminal`**,
  `engaged_5plus`, `sticky_*`, `no_facts_row`, `locked` …). Written by
  `update_last_analysis()` (routes.py ~77). **This layer already contains the
  "conversation ended / no reply needed" signal #2 was asking for.**

## Snapshot (129 rows; 24 merged → 105 active distinct leads)

**Label distribution (non-merged) & freshness**
| label | n | never analyzed | avg analysis age | max age |
|---|---|---|---|---|
| DISREGARDED | 33 | 0 | 108.9h | 189.8h |
| COLD | 31 | 0 | 3.4h | 7.8h |
| HOT | 19 | 0 | 3.5h | 7.6h |
| NEW | 10 | 0 | 2.1h | 4.8h |
| WARM | 6 | 0 | 2.4h | 5.7h |
| CONFIRMED | 5 | 0 | 3.3h | 5.6h |
| WAITING_FOR_PAYMENT | 1 | 0 | 0.7h | 0.7h |

**Stale analyses (customer messaged AFTER last analysis):** 5 total — **4 HOT, 1 NEW**, everyone else 0.
**Owe-reply cohort:** 6 leads → 1 stale, 3 score-0, 3 CONFIRMED, 1 genuinely-active.
**`conversation_state` signal layer:** 136 rows, 123 have a signal, only **1 stale**, 13 never analyzed.

## Findings

### 1. Staleness is NOT the current bottleneck (worklog premise is now outdated)
0 active leads are unanalyzed; only **5/105** have an analysis older than the
customer's latest message (and 4 of those are HOT — the cohort that matters,
but small). The old analyses (26 leads >3 days) are almost entirely
**DISREGARDED dead leads**, where staleness is harmless. The prior sessions'
fix — hourly sweep with `PIPELINE_ANALYZE_CAP=24` + `ORDER BY
importance_analyzed_at ASC NULLS FIRST` (stalest-first) — **is keeping the
~72-lead active cohort fresh (≤ ~8h).** Re-analysis cadence is healthy.

### 2. The real root of #2 is signal-CONSUMPTION, not signal-freshness  ⭐
The analyzer already writes fresh, correct terminal signals
(`confirmed_terminal` ×4, `date_passed` ×6, `cold_decay` ×13) to
`conversation_state.last_analysis_signal`. But the AWAITING_REPLY builder
(`review.py` ~440–479) decides membership **purely on timing + two exclusions**:
`importance_score == 0` → NOT_A_CUSTOMER bucket, and `label == CONFIRMED` →
stays in CONFIRMED. **It never reads `last_analysis_signal`.** So a non-zero-score
lead the analyzer has *already judged terminal/ended* (e.g. Rashid, imp 57)
still surfaces as "📨 awaiting your reply." → Fix = add a signal-based exclusion
to AWAITING (`confirmed_terminal` / `date_passed` / `cold_decay` → drop or
downrank out of the owed-reply section). **Bridge-only, read-side, no n8n / no
live-flow risk; dry-runnable offline.**

### 3. Residual (bounded) staleness risk: no re-analyze on after-hours inbound
The sweep runs hourly **but is gated to Dubai 09:00–21:00**
(`UAE_WORK_HOURS_START=9`, `END=21`), and an inbound customer message does **not**
trigger an immediate re-analysis. So a late-evening inbound can sit up to ~12h
with an outdated score overnight. Low impact today (low volume; max observed
active age ~8h) but it's the structural source of #2's "re-analyze fresher on
new inbound." Cheap mitigation: targeted single re-analysis when a HOT/WARM lead
messages after-hours, or relax the gate for the owe-reply subset only.

### 4. Current actionable list is essentially clean
The only high-value/owed/stale row is **Alma (Fruitful Day)** — a fruit
*supplier*, score 0, mislabeled NEW — and #3's bucket already routes score-0
leads to 💤 NO ACTIVE SALE, so the operator sees it correctly. No genuine
high-value lead is sitting stale right now.

## Recommendation (in priority order)
1. **Implement Finding 2** (highest leverage, lowest risk): AWAITING_REPLY
   reads `last_analysis_signal`; terminal signals leave the owed-reply section.
   This directly closes #2 using a signal that already exists and is fresh.
2. **Finding 3** as a follow-up: re-analyze-on-inbound for hot/warm after-hours
   (closes the structural staleness gap).
3. Bank Findings 1 & 4: staleness itself needs no further work right now.
