-- 013_analysis_unreliable.sql (2026-06-12) — Bug #2
-- The /review "⚠️ analysis unreliable — verify (history incomplete)" flag was
-- recomputed at RENDER time from the analyzer's reasoning prose, false-positiving
-- on its normal rule vocabulary ('Rule 8 — first contact', 'never replied') which
-- describes CUSTOMER behaviour, not history availability — flagging 15+ healthy
-- analyses with no way to clear. Move the verdict to ANALYZE time, where the REAL
-- fetched-history length is known, and STORE it (analysis_guard.analysis_unreliable_verdict).
-- The /review render then reads this column (gated by REVIEW_UNRELIABLE_FROM_STORE_ENABLED),
-- falling back to the legacy heuristic only for NULL (pre-migration / not-yet-re-analyzed) rows.
-- Nullable, no default: NULL == "verdict not yet stored" → render fallback.
-- Apply as owner (n8n). hermes_rw already holds table-level UPDATE on customer_facts
-- (the bridge upserts it), which covers new columns; the GRANT below is belt-and-suspenders.
ALTER TABLE customer_facts ADD COLUMN IF NOT EXISTS analysis_unreliable boolean;
GRANT SELECT, UPDATE ON customer_facts TO hermes_rw;
