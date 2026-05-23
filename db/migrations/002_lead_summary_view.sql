-- 002_lead_summary_view.sql
-- Read-only view used exclusively by POST /review (and the on-demand
-- /review operator command). One row per customer with all the
-- fields the priority scorer needs.
--
-- Indexes live on the underlying tables (customer_facts, conversation_state,
-- customer_triggers, customer_notes) — view inherits them.
--
-- Soft dependencies: tolerates missing customer_triggers types (filters
-- by string), tolerates absent conversation_state row via LEFT JOIN,
-- tolerates absent customer_notes (subquery returns NULL).

BEGIN;

CREATE OR REPLACE VIEW v_lead_summary AS
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
  cs.last_customer_message_at,
  cs.last_operator_reply_at,
  cs.last_review_seen_at,
  cs.last_nudge_drafted_at,
  cs.reengage_attempts,
  cm.mode AS conversation_mode,

  -- customer_triggers uses columns `trigger_type` and `detected_at`.
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
-- conversation_modes has multiple rows per customer (audit log style).
-- Pick the most recent mode via DISTINCT ON.
LEFT JOIN LATERAL (
  SELECT mode FROM conversation_modes
  WHERE customer_id = cf.customer_id
  ORDER BY id DESC LIMIT 1
) cm ON TRUE;

GRANT SELECT ON v_lead_summary TO hermes_rw;

COMMIT;
