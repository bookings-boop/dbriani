-- 008_booked_yacht.sql (2026-06-02)
-- The `yachts` field accumulates EVERY yacht discussed; a booked customer needs
-- the SINGLE yacht actually confirmed. Add a nullable booked_yacht column,
-- populated from the chat (analyzer auto-extract) + manual backfill. Additive,
-- safe online. The bridge falls back to the `yachts` list when this is NULL.
ALTER TABLE customer_facts ADD COLUMN IF NOT EXISTS booked_yacht text;
