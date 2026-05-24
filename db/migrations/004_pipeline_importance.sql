-- 004_pipeline_importance.sql
-- Hermes-driven pipeline importance analyzer + Disregard feature.
-- Additive only.
--
-- Adds:
--   1. customer_facts importance + disregard fields (NULL = not yet analyzed)
--   2. v_lead_summary exposes the new fields to /review's scorer
--   3. (no new indexes — importance is a sort key downstream of label filter
--      which is already indexed; query selectivity stays cheap)

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. customer_facts.importance_* — populated by /pipeline-analyze hourly cron
--    customer_facts.disregard_* — populated by [🛑 Disregard] button
-- ---------------------------------------------------------------------------
ALTER TABLE customer_facts
  ADD COLUMN IF NOT EXISTS importance_score        INTEGER,
  ADD COLUMN IF NOT EXISTS importance_reasoning    TEXT,
  ADD COLUMN IF NOT EXISTS suggested_action        TEXT,
  ADD COLUMN IF NOT EXISTS importance_analyzed_at  TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS disregard_verdict       TEXT,
  ADD COLUMN IF NOT EXISTS disregard_reasoning     TEXT,
  ADD COLUMN IF NOT EXISTS disregard_analyzed_at   TIMESTAMPTZ;

-- ---------------------------------------------------------------------------
-- 2. v_lead_summary — expose importance + disregard for /review sort + display.
--    DROP + recreate because CREATE OR REPLACE VIEW cannot insert columns in
--    the middle of an existing column list (PG reports
--    "cannot change name of view column").
-- ---------------------------------------------------------------------------
DROP VIEW IF EXISTS v_lead_summary;
CREATE VIEW v_lead_summary AS
SELECT
  cf.customer_id,
  cf.name,
  cf.label,
  cf.label_updated_at,
  cf.label_locked_until,
  cf.message_count,
  cf.yachts,
  cf.dates,
  cf.party_size,
  cf.updated_at AS facts_updated_at,
  cf.importance_score,
  cf.importance_reasoning,
  cf.suggested_action,
  cf.importance_analyzed_at,
  cf.disregard_verdict,
  cf.disregard_reasoning,
  cf.disregard_analyzed_at,
  cs.last_customer_message_at,
  cs.last_operator_reply_at,
  cs.last_review_seen_at,
  cs.last_nudge_drafted_at,
  cs.reengage_attempts,
  cm.mode AS conversation_mode,

  (SELECT max(ct.detected_at) FROM customer_triggers ct
    WHERE ct.customer_id = cf.customer_id
      AND ct.trigger_type = 'payment_link_sent')    AS last_payment_link_at,
  (SELECT max(ct.detected_at) FROM customer_triggers ct
    WHERE ct.customer_id = cf.customer_id
      AND ct.trigger_type = 'payment_promised')     AS last_payment_promised_at,
  (SELECT max(ct.detected_at) FROM customer_triggers ct
    WHERE ct.customer_id = cf.customer_id
      AND ct.trigger_type = 'booking_intent')       AS last_booking_intent_at,
  (SELECT max(ct.detected_at) FROM customer_triggers ct
    WHERE ct.customer_id = cf.customer_id
      AND ct.trigger_type IN ('rejected_price','rejected_timing','rejected_other'))
                                                    AS last_rejection_at,
  (SELECT ct.trigger_type FROM customer_triggers ct
    WHERE ct.customer_id = cf.customer_id
      AND ct.trigger_type IN ('rejected_price','rejected_timing','rejected_other')
    ORDER BY ct.detected_at DESC LIMIT 1)           AS last_rejection_kind,

  (SELECT string_agg(note_text, ' | ' ORDER BY created_at DESC)
    FROM (SELECT note_text, created_at FROM customer_notes
          WHERE customer_id = cf.customer_id AND active = true
          ORDER BY created_at DESC LIMIT 5) n)
                                            AS recent_notes
FROM customer_facts cf
LEFT JOIN conversation_state cs ON cs.customer_id = cf.customer_id
LEFT JOIN LATERAL (
  SELECT mode FROM conversation_modes
  WHERE customer_id = cf.customer_id
  ORDER BY id DESC LIMIT 1
) cm ON TRUE;

GRANT SELECT ON v_lead_summary TO hermes_rw;

COMMIT;
