-- 005_followup_count.sql
-- Bounded proactive-follow-up cap. Production bug 2026-05-25: customer
-- says "Okay", operator replies "take your time! 😊", but a STALE
-- queued proactive card later sends as an unsolicited Zenith 64 specs
-- upsell. Migration 005 + the freshness-gate fix (P0) + directive
-- hardening (P2) together close all three layers of the problem.
--
-- followup_count tracks proactive follow-ups SENT since the customer
-- last spoke. Reset to 0 on customer_message (in code). Incremented
-- on nudge_drafted (proactive engine path). scan_followup_eligibility
-- skips customers where followup_count >= 2 — bounds the queue depth
-- and prevents repeat-upselling silent customers.
--
-- Additive only; safe to re-run.

BEGIN;

ALTER TABLE conversation_state
  ADD COLUMN IF NOT EXISTS followup_count INTEGER NOT NULL DEFAULT 0;

-- For active leads currently in the queue, optimistically reset to 0
-- so the migration doesn't immediately silence existing customers.
-- Anyone who's already had >= 2 followups will naturally get capped on
-- their next nudge_drafted event.
-- (Default 0 from ADD COLUMN handles this — no UPDATE needed for new
-- column; column starts at 0 for all existing rows.)

COMMIT;
