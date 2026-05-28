-- Draft feedback loop — learn from operator edits.
-- Phase 1 schema: edit_corrections table + behavior_rules.source column.
-- Idempotent (IF NOT EXISTS) so re-running is safe.

CREATE TABLE IF NOT EXISTS edit_corrections (
  id                  SERIAL PRIMARY KEY,
  customer_id         TEXT NOT NULL,
  original_text       TEXT NOT NULL,        -- first Hermes draft
  sent_text           TEXT NOT NULL,        -- what actually went to the customer
  context_label       TEXT,                 -- customer label at send time
  context_yacht       TEXT,                 -- primary yacht of interest
  context_country     TEXT,                 -- calling-code country
  reason_tag          TEXT,                 -- too_long|wrong_tone|missed_question|wrong_info|my_style|NULL
  reason_detail       TEXT,                 -- optional operator free-text (guided question)
  similarity          REAL,                 -- difflib ratio at capture (audit)
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  rule_generated_from INTEGER REFERENCES behavior_rules(id)  -- rule this row fed; NULL until used
);

CREATE INDEX IF NOT EXISTS idx_edit_corr_reason_ctx
  ON edit_corrections (reason_tag, context_label, created_at);
CREATE INDEX IF NOT EXISTS idx_edit_corr_customer
  ON edit_corrections (customer_id);

-- Tag behavior_rules born from the edit-learning loop (distinct from created_via).
ALTER TABLE behavior_rules ADD COLUMN IF NOT EXISTS source TEXT;

-- The bridge connects as hermes_rw (read/write) / hermes_ro (read-only); the
-- table is owned by n8n, so new tables need explicit grants or the bridge
-- gets "permission denied". Mirror the grants the other bridge tables have.
GRANT SELECT, INSERT, UPDATE ON edit_corrections TO hermes_rw;
GRANT SELECT ON edit_corrections TO hermes_ro;
GRANT USAGE, SELECT ON SEQUENCE edit_corrections_id_seq TO hermes_rw;
