# Permanent cure for the recycled-LID identity mess — phone-keyed identity

**Date:** 2026-06-02 · **Status:** PLAN + Stage 0 detection shipped (read-only). Behavior-changing stages are a single-owner staged migration — do NOT dual-edit `canonicalize_cid`/reconcile while another session is in them.

## The problem (root, confirmed live)
Identity is keyed on the WhatsApp-delivered id — `<lid>@lid` or `<digits>@c.us`. **WhatsApp RECYCLES `@lid` values**: an id that was Antonio's can later resolve to Qurbani's number. Two failure modes:
1. **False merge** — reconcile merges `@lid → @c.us` on a live `lid_to_cus` lookup with weak validation → two people collapse (original Qurbani→Antonio).
2. **Stuck recycled LID** — once the merge guard (correctly) refuses to merge "Antonio"↔"Qurbani", Qurbani's *new* messages keep landing on the Antonio-named `@lid` row → "Qurbani shows as Antonio" (2026-06-02, fixed manually per-instance).

The volatile id is the wrong identity key. **The phone number is stable** — that's the cure.

## Cure: make the phone the identity key (staged)

- **Stage 0 — Detection (SHIPPED, read-only, no conflict):** `scripts/recycled_lid_audit.py` — for active `@lid` rows, resolve `lid_to_cus` and flag rows whose resolved phone (a) also exists as a *separate* `@c.us` row, or (b) conflicts with the row's own name. Surfaces Qurbani-class problems proactively. Safe to run anytime (capped WAHA calls).
- **Stage 1 — Capture (additive):** `ALTER TABLE customer_facts ADD COLUMN resolved_phone text NULL, resolved_phone_at timestamptz NULL`. In `upsert_customer_facts` / `canonicalize_cid`, resolve the id→phone once and store it. *Touches `canonicalize_cid` — coordinate with the other session's #10.*
- **Stage 2 — Recycle detection at capture:** when an `@lid`'s freshly-resolved phone ≠ its stored `resolved_phone`, the LID was recycled → do NOT merge; quarantine + alert; treat the new traffic as a new phone-keyed identity.
- **Stage 3 — Phone-keyed canonicalize:** `canonicalize_cid(@lid)` resolves to the phone's canonical `@c.us` row when a confident WAHA mapping + matching stored phone exist — so messages route by PHONE, not id. This is what makes the recycled-LID class disappear (a recycled LID's messages route to the *current* phone owner automatically).
- **Stage 4 — Merge by phone:** reconcile merges rows sharing `resolved_phone` (authoritative), name guard secondary; rows whose phone CHANGED are split off, never merged.
- **Stage 5 — Backfill + remediation:** populate `resolved_phone` for existing rows (capped WAHA sweep), run Stage-0 detection, fix flagged rows (rename + merge into the phone owner, as done manually for Qurbani).

## Risks & sequencing (read before implementing)
- **Highest blast radius in the system.** Each behavior stage: pure helpers + tests + a canary, deploy bridge-only, daytime + a real test message, rollback ready.
- **Concurrency hazard:** Stages 1/3/4 touch `canonicalize_cid` + `handle_reconcile_identities`, which another session is actively editing (#10 cid-canonicalize, #11 merge-safety `_names_likely_same_person` + `do_not_merge` pin). **Single-owner these stages** — merge/settle the other session's identity work FIRST, then execute Stages 1→5 in one focused pass. Dual-editing this is the #1 way to cause a customer-mixup incident.
- **WAHA dependency:** `lid_to_cus` can be wrong/stale (it returned Qurbani's number for Antonio's old LID). Phone capture must store the resolution *and* its timestamp; a changed resolution is a recycle SIGNAL, not a merge trigger.

## Interim (already in place, no rewrite needed)
- Merge-safety guard refuses cross-name merges (`_merge_blocked` / other session's `_names_likely_same_person`) — prevents new false merges.
- Per-instance manual fix pattern (Qurbani 2026-06-02): rename the recycled `@lid` to the true owner + `merged_into` the owner's clean `@c.us`; `canonicalize_cid` then routes future messages correctly. Stage-0 detection surfaces who needs this.
