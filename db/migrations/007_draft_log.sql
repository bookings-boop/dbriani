-- 007_draft_log.sql — durable draft observability log.
--
-- Part 1 of the draft-logging + shadow-mode foundation. ADDITIVE ONLY: this
-- table only RECORDS what the bot drafted/scored/did — it never changes what is
-- sent. Postgres (NOT Redis/TTL) so the review trail survives restarts and the
-- operator can review, for any day, every draft + its score + its outcome
-- (including shadow-mode "would_send / would_hold" simulations).
--
-- One row per draft, keyed by draft_id; lifecycle events UPSERT it
-- (created -> scored -> outcome). Rows with a NULL draft_id (rare, un-linkable)
-- are kept as standalone rows (NULLs are distinct in a unique index).
--
-- Retention: 90 days, swept by the daily cron (see cron-daily-summary).
-- Additive only; safe to re-run.

BEGIN;

CREATE TABLE IF NOT EXISTS draft_log (
  id               BIGSERIAL PRIMARY KEY,
  draft_id         TEXT,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  customer_id      TEXT,
  customer_name    TEXT,
  trigger_kind     TEXT,        -- inbound | nudge | lead | assist
  incoming_message TEXT,
  draft_text       TEXT,
  mode             TEXT,        -- approval | autonomous | shadow
  score            INT,         -- scorecard 1-10; NULL until scored
  score_flags      TEXT,
  score_summary    TEXT,
  outcome          TEXT,        -- held|sent|edited|skipped|auto_sent|would_send|would_hold
  final_text       TEXT,        -- if edited: what actually went to the customer
  is_shadow        BOOLEAN NOT NULL DEFAULT false
);

-- Lifecycle upsert key. UNIQUE on draft_id; Postgres treats NULLs as distinct
-- so multiple un-linkable rows are allowed. Enables INSERT ... ON CONFLICT
-- (draft_id) DO UPDATE for the created -> scored -> outcome progression.
CREATE UNIQUE INDEX IF NOT EXISTS idx_draft_log_draft_id ON draft_log (draft_id);

-- Day-range review (/log <day>) + retention sweep.
CREATE INDEX IF NOT EXISTS idx_draft_log_created_at ON draft_log (created_at);

-- The bridge writes as the limited BRIDGE_PG_USER role (default hermes_rw), so
-- it needs table + sequence privileges. Idempotent; harmless if hermes_rw
-- already owns the table.
GRANT SELECT, INSERT, UPDATE, DELETE ON draft_log TO hermes_rw;
GRANT USAGE, SELECT ON SEQUENCE draft_log_id_seq TO hermes_rw;

COMMIT;
