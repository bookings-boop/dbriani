-- 006_customer_merged_into.sql
-- Identity layer for the customer_facts duplicate-row problem
-- (production bug 2026-05-26: ~10 customers have 2 rows in
-- customer_facts under different cid formats — @lid+@c.us, or two
-- different @lid hashes — because WhatsApp assigns inconsistent IDs
-- across sessions/devices/clients. This causes drafts to queue under
-- one cid while Send fetches from another, leading to wrong-version
-- and double-send bugs).
--
-- Design: one cid per customer remains CANONICAL. The other cid
-- points at it via `merged_into`. All bridge reads/writes resolve
-- the pointer first, so the canonical row receives every write
-- regardless of which cid the inbound event uses.
--
-- A row is canonical iff merged_into IS NULL. Merged rows are kept
-- (not deleted) for two reasons:
--   1. Foreign references (customer_label_history, autonomous_sends,
--      customer_notes, conversation_state) may still point at the
--      old cid — migration of those is best-effort but new writes
--      land on canonical going forward.
--   2. WAHA may surface the merged cid again on a future message;
--      the bridge then follows the pointer to the canonical row.
--
-- Additive only; safe to re-run.

BEGIN;

ALTER TABLE customer_facts
  ADD COLUMN IF NOT EXISTS merged_into TEXT;

-- Fast lookup: 'who points to this canonical cid' (rarely used) and
-- 'where does this cid point' (used on every read/write resolution).
CREATE INDEX IF NOT EXISTS idx_customer_facts_merged_into
  ON customer_facts (merged_into) WHERE merged_into IS NOT NULL;

-- Self-reference safety: a row cannot point at itself.
ALTER TABLE customer_facts
  DROP CONSTRAINT IF EXISTS customer_facts_no_self_merge;
ALTER TABLE customer_facts
  ADD CONSTRAINT customer_facts_no_self_merge
  CHECK (merged_into IS NULL OR merged_into <> customer_id);

COMMIT;
