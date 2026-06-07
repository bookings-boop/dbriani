-- 011_booking_detail_fields.sql — persist the structured booking detail.
--
-- W5+W4 (2026-06-07) taught the bridge to CAPTURE three structured booking
-- facts in extract_customer_facts and to RENDER them (review._booking_detail_line
-- itemises the CONFIRMED card; routes.handle_info shows them on /info; the
-- analyzer's relative-date veto reads facts['booking_date_abs']) — BUT they were
-- never persisted: _upsert_facts_sql wrote only name/dates/yachts/party_size, so
-- every captured value was dropped on the next message and all of it was dead.
-- This migration adds the columns so _upsert_facts_sql can persist them and the
-- read-sites (get_customer_facts, read_lead_summary, handle_info) can SELECT them
-- back, closing the loop:
--   - booking_date_abs : the normalized absolute booking date (ISO YYYY-MM-DD),
--                        resolved from relative phrases ('tomorrow') at capture.
--   - booking_time     : the requested time window ('5PM-9PM').
--   - addons           : extras requested (BBQ, jetski, decoration, ...).
--
-- WRITE-PATH PREREQUISITE: _upsert_facts_sql references these columns on EVERY
-- inbound (exactly like name_locked / migration 009). The bridge DB user
-- (hermes_rw) is NOT the table owner and cannot ADD them, so the OWNER role (n8n)
-- applies this migration. server._ensure_schema does a read-only existence check
-- and LOUDLY warns if they are missing. Apply this BEFORE (or together with) the
-- new server.py so the facts UPSERT never freezes message_count system-wide.
--
-- Additive only; safe online (PG 11+ stores a nullable ADD COLUMN as catalog-only
-- metadata, no table rewrite, no default); idempotent (ADD COLUMN IF NOT EXISTS),
-- safe to re-run. Table-level grants already held by hermes_rw automatically
-- cover the new columns, so no GRANT is needed (matches migration 009).

BEGIN;

ALTER TABLE customer_facts
  ADD COLUMN IF NOT EXISTS booking_date_abs text;
ALTER TABLE customer_facts
  ADD COLUMN IF NOT EXISTS booking_time text;
ALTER TABLE customer_facts
  ADD COLUMN IF NOT EXISTS addons text;

COMMIT;
