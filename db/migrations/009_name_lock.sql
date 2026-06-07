-- 009_name_lock.sql (2026-06-06)
-- Bug: the per-message fact-extraction upsert (upsert_customer_facts) wrote
-- `name = EXCLUDED.name` UNCONDITIONALLY, so a fresh LLM-extracted name
-- overwrote the stored one on every inbound message. An operator's manual
-- rename never stuck — customer 971509767187 ("Zayn") kept reverting to a
-- mis-extracted "Antonio" each time they messaged.
--
-- Fix: a name_locked flag. _upsert_facts_sql now keeps the existing name when
-- name_locked is true; UNLOCKED rows still adopt the latest extracted name
-- (auto-extraction can still fill/refine names nobody has corrected). Set
-- name_locked=true (and name_lock_reason) whenever an operator manually
-- corrects a customer's name.
--
-- Additive only; safe online (PG 11+ stores the NOT NULL DEFAULT as metadata,
-- no table rewrite); safe to re-run.
ALTER TABLE customer_facts
  ADD COLUMN IF NOT EXISTS name_locked boolean NOT NULL DEFAULT false;
ALTER TABLE customer_facts
  ADD COLUMN IF NOT EXISTS name_lock_reason text;
