-- 014_analyze_attempt_at.sql (2026-06-13) — T-1 sweep rotation stall
-- The hourly pipeline sweep selects active leads "stalest score first"
--   ORDER BY importance_analyzed_at ASC NULLS FIRST
-- and a Hermes failure (rc=1 / "no JSON") returns ('error', cid) WITHOUT bumping
-- importance_analyzed_at. So a chronic failer keeps its old (or NULL) timestamp,
-- stays at the FRONT of the rotation, and is re-picked + re-fails every sweep —
-- 5 such leads were each failing 13-19x, burning ~5 of the 24 slots per sweep and
-- starving fresh leads (the latency that left Violetta's analysis 18h stale).
--
-- Fix: track when we last ATTEMPTED a lead (success OR failure) and rotate on
-- THAT, so a failer moves to the back of the queue after one try instead of
-- blocking the front. Seeded from importance_analyzed_at so the rotation starts
-- sensibly (== current behaviour) and only failers shift back as they're retried.
-- Apply as owner (n8n); hermes_rw already has table-level UPDATE on customer_facts.
ALTER TABLE customer_facts ADD COLUMN IF NOT EXISTS last_analyze_attempt_at timestamptz;
UPDATE customer_facts SET last_analyze_attempt_at = importance_analyzed_at
  WHERE last_analyze_attempt_at IS NULL;
GRANT SELECT, UPDATE ON customer_facts TO hermes_rw;
