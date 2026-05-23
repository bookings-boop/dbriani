-- 001_pipeline_review.sql
-- Schema for the Pipeline Review feature (see docs/pipeline-review-plan.md).
-- Additive only — every change is safe to run on a populated production DB.
--
-- Adds:
--   1. customer_facts.label + label_updated_at + label_locked_until + label_locked_reason
--   2. customer_label_history  (audit log of every label transition)
--   3. conversation_state      (per-customer timing + analysis bookkeeping)
--   4. label_corrections       (operator manual overrides → drives self-improvement dampening)

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. customer_facts.label (and locking columns)
-- ---------------------------------------------------------------------------
ALTER TABLE customer_facts
  ADD COLUMN IF NOT EXISTS label                TEXT NOT NULL DEFAULT 'NEW',
  ADD COLUMN IF NOT EXISTS label_updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  ADD COLUMN IF NOT EXISTS label_locked_until   TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS label_locked_reason  TEXT;

CREATE INDEX IF NOT EXISTS idx_customer_facts_label
  ON customer_facts (label);
CREATE INDEX IF NOT EXISTS idx_customer_facts_label_updated
  ON customer_facts (label_updated_at DESC);

-- ---------------------------------------------------------------------------
-- 2. customer_label_history
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS customer_label_history (
  id            SERIAL PRIMARY KEY,
  customer_id   TEXT NOT NULL,
  from_label    TEXT,
  to_label      TEXT NOT NULL,
  signal        TEXT NOT NULL,
  evidence      TEXT,
  message_count INTEGER,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  created_by    TEXT NOT NULL DEFAULT 'system'
);

CREATE INDEX IF NOT EXISTS idx_label_hist_customer
  ON customer_label_history (customer_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_label_hist_to_label
  ON customer_label_history (to_label, created_at DESC);

-- ---------------------------------------------------------------------------
-- 3. conversation_state
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS conversation_state (
  customer_id              TEXT PRIMARY KEY,
  last_customer_message_at TIMESTAMPTZ,
  last_operator_reply_at   TIMESTAMPTZ,
  last_review_seen_at      TIMESTAMPTZ,
  last_nudge_drafted_at    TIMESTAMPTZ,
  last_analyzed_at         TIMESTAMPTZ,
  last_analysis_signal     TEXT,
  last_analysis_confidence REAL,
  reengage_attempts        INTEGER NOT NULL DEFAULT 0,
  updated_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_conv_state_last_cust
  ON conversation_state (last_customer_message_at DESC);
CREATE INDEX IF NOT EXISTS idx_conv_state_last_op
  ON conversation_state (last_operator_reply_at DESC);
CREATE INDEX IF NOT EXISTS idx_conv_state_last_analyzed
  ON conversation_state (last_analyzed_at DESC NULLS FIRST);

-- ---------------------------------------------------------------------------
-- 4. label_corrections
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS label_corrections (
  id              SERIAL PRIMARY KEY,
  customer_id     TEXT NOT NULL,
  auto_label      TEXT NOT NULL,
  auto_signal     TEXT NOT NULL,
  auto_confidence REAL,
  manual_label    TEXT NOT NULL,
  message_count   INTEGER,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  pattern_summary TEXT,
  reviewed_at     TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_label_corr_signal
  ON label_corrections (auto_signal, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_label_corr_customer
  ON label_corrections (customer_id, created_at DESC);

-- ---------------------------------------------------------------------------
-- Grants — hermes_rw inherits via default privileges in this DB, but be
-- explicit so a fresh role get the right access without a re-grant trip.
-- ---------------------------------------------------------------------------
GRANT SELECT, INSERT, UPDATE ON customer_label_history TO hermes_rw;
GRANT SELECT, INSERT, UPDATE ON conversation_state     TO hermes_rw;
GRANT SELECT, INSERT, UPDATE ON label_corrections      TO hermes_rw;
GRANT USAGE, SELECT ON SEQUENCE customer_label_history_id_seq TO hermes_rw;
GRANT USAGE, SELECT ON SEQUENCE label_corrections_id_seq      TO hermes_rw;

COMMIT;
