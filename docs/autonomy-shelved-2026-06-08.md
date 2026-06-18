# Decision: Autonomous free-text send is SHELVED (2026-06-08)

**Decision:** Autonomous (human-out-of-the-loop) free-text WhatsApp replies are **SHELVED**. The system stays **approval-first** (one-tap ✅ Send cards). The autosend machinery is **PARKED-dormant** — kept in place, **not deleted**; we stop building on it.

Reached via the 5-step (question → delete → simplify → accelerate → automate) process: questioning the requirement showed full autonomy adds little over the already-live one-tap loop while carrying severe, irreversible downside — so it's deleted/shelved, not optimized.

## Why — re-verified read-only (2 multi-agent passes: `wgiqvrik1` + `wc4ty7mnv`)
- **Floor clearance ≈ never.** Since the 8/10 quality floor was added (2026-06-02): exactly **1** real draft cleared ≥8 (`99850070786224@lid`, score 8, 2026-06-03) vs **10 FLOOR-BLOCK** (scores 0,1,2,4,5,7,7 + 3× "no draft") = **~12.5%** of numerically-scored drafts. **27 `kind=auto` sends ever, 0 today.** We'd carry the entire risk stack for a feature that fires ~never.
- **R0 CONFIRMED BROKEN (scored ≠ dispatched).** Auto Commit posts only `{customer_id, commit:true}`; the bridge scores `_draft_latest_for_customer(cid,'pending')` (the live/improved draft) while WAHA dispatches the **arm-time snapshot** from `autosend:<draft_id>`. The verdict carries no `draft_id`/hash — nothing binds scored bytes to sent bytes. The **FR-5 improver** rewrites the pending draft in place, so the floor can pass the *improved* ≥8 text while WAHA sends the *original* <8 snapshot. A purpose-built fix (`/autosend-state action=update`, routes.py:4787-4819) **EXISTS but is dead code** — 0 nodes in live workflow `azPIy9OcDwiPV5uY` call it.
- **Autonomy is fully OFF.** Global default = approval (manual_killswitch 2026-06-03); **0 per-cid autonomous rows** after Nassr `5532505120995@lid` was reverted (conversation_modes id368, 2026-06-08 03:12).

## Parked-dormant (kept, NOT deleted — do not build on it)
- `handle_autosend_check` commit path (routes.py:4519-4665)
- `_anthropic_score` quality floor / `_quality_floor_ok` (routes.py:5779; labels.py:780; `AUTOSEND_MIN_SCORE=8`)
- `_deterministic_break` (labels.py:1264), `validate_draft_prices` on autosend (routes.py:4639), `evaluate_caps`/`log_autosend` (server.py:2238/2231)
- n8n Auto* branch (~14 nodes) + FR-5 improver branch in `azPIy9OcDwiPV5uY`
- orphaned `/autosend-state action=update` (routes.py:4787-4819) — **KEEP** for the eventual R0 fix

**Dormant posture:** NO send-path change needed — the fail-closed `get_mode` gate (global approval + 0 autonomous cids) already prevents any auto-send.

## Redirect the backlog
- **(a) Draft quality** → approval-mode drafts good enough for **edit-free one-tap** (captures autonomy's speed upside on the human-in-the-loop path; the 12.5% floor clearance shows free-text autosend can't carry the load anyway).
- **(b) ROOT A latency** → cut analyzer/quality-check latency (the Hermes-saturation root) so one-tap is fast.

## The ~10% add-back (much later)
Re-introduce autonomy ONLY for **templated, no-price** message types, behind a **fixed R0 guarantee** (scored == dispatched), rolled out shadow → canary → limited. **Not** free-text sales replies.

## Still-open SUPERVISED-DAYTIME items (carried forward — not lost)
- **Root B dropped leads** (+971568241103, ~95 gaps) — **BLOCKED on n8n-owner creds** (`execution_entity` permission denied). Next session starts here.
- **Xeno paylink code idempotency guard** (financial).
- **Reconcile-on-pay-mismatch** (#10b) (financial).
- **Durable-store corruption** (P2-3).
- **"not a customer" lexicon fix** (classifier) + the corrected DISREGARDED→LOST rebucket.
- **phone↔@lid identity split** — structural root under several bugs (Antonio/India/Xeno records live under `@lid`, not the phone).
- **P2-2 backfill decision** for existing NULL confirmed bookings.
