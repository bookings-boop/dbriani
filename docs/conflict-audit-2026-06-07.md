# Conflict audit + remediation — 2026-06-07

Deep multi-agent conflict audit of last week's work (8 domains, 105 agents,
adversarial 2-lens verification) → 22 distinct confirmed conflicts (5 HIGH, 12
MED, 5 LOW) + 3 uncertain. Remediation below. Suite 68/68; bridge deployed +
sha256 repo==box==live.

## FIXED + deployed (18)
| # | Sev | Fix | File |
|---|-----|-----|------|
| 3 | HIGH | LOST/DISREGARDED short-circuit in handle_label_eval + hourly sweep (terminal labels no longer auto-reopen on a 4-digit run) | routes.py |
| 11 | MED | _should_cold_decay excludes LOST/DISREGARDED (no reopen-to-COLD) | labels.py |
| 7 | MED | reengage SQL excludes `auto:analyzer%` signals (don't re-nudge analyzer-killed leads) | server.py |
| 6 | MED | dormancy counts reset-immune `proactive_followup_sent` sends, not `reengage_attempts` (fixes both linger + false-close); moots #22 | routes.py |
| 22 | LOW | (mooted by #6 — dormancy no longer bounded by FOLLOWUP_CAP) | — |
| 13 | MED | /name canonicalizes the cid before write (no orphaned merged-row lock) | routes.py |
| 12 | MED | merge propagates dup→canon name-lock (operator rename survives a merge) | routes.py |
| 15 | MED | startup read-only schema check + loud DR warning if name_locked missing (hermes_rw can't ALTER) | server.py |
| 14 | MED | exclusion guard fails CLOSED for unresolvable @lid on the proactive path | hermes_exclusion_guards.py + routes.py |
| 9/21 | MED/LOW | reminder cron joins customer_facts: suppress terminal/merged + show current canonical name | cron-reminders.py |
| 18 | LOW | _yacht_display gates on real `row['label']`, not section key | review.py |
| 20 | LOW | handle_draft_gated returns original_score=None on scorer failure (no baseScore floor-to-1) | routes.py |
| 16a/1 | MED/HIGH | deploy_bridge asserts n8n import success (no more silent stale/revert) | scripts/deploy_bridge.py |
| 17 | MED | deploy_bridge stops pruning safe_put PRE-{tag} backups | scripts/deploy_bridge.py |
| 2 | HIGH | price validator: Premium BBQ {1500}→{2500,1500} (1,500 was the min-spend, not price), cake {300}→{300,500}, word-boundary anchor | server.py |
| 8 | MED | CONFIRMED card shows FIRST (deposit) payment, not the latest charge (1-AED test no longer masks the deposit) | server.py |
| 5 | HIGH(future) | autosend floor runs validate_draft_prices → blocks fabricated prices before any auto-send | routes.py |
| 10a | MED(future) | reconcile respects label_locked_until (operator pin survives) | routes.py |

Each pure change is TDD'd (tests added/updated in test_cold_decay, test_followup_eligibility, test_exclusion_guards, test_yacht_display, test_price_validator).

## DEFERRED (documented — low/future/financial or n8n-content)
- **#1 node-content / #16b / #19** — n8n-content edits (Apply-Improvement node reconciliation, pass system_prompt to judges, Build-Nudge-Card excluded handling). LOW/FUTURE; #1's *root* (silent revert) is fixed by #16a's import-assert. Need a focused n8n `safe_put` session.
- **#4** — autosend dispatches the arm-time snapshot vs the improved store draft. FUTURE (autonomy OFF) + n8n change. Pair with the supervised autonomy work.
- **#10b** — block reconcile promotion on `pay_mismatch` (changes promotion logic) → supervised daytime.
- **#8 SUM-total** — show total-of-real-payments (needs a malformed-safe numeric cast on the hot /review path) → supervised daytime; the ASC deposit-first fix already removes the scary "AED 1".

## Verified non-issues (7 refuted) — see the audit report in task output; not detailed here.
