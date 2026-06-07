-- READ-ONLY preview of the NEW reengage target population (BUG G fix).
-- Uses a session-local TEMP table (auto-dropped at disconnect; no persistent
-- write). Mirrors _followup_candidate_sql() WHERE + _silence_window_for()
-- mapping + the Python-side locked/autonomous skips.
CREATE TEMP TABLE _reengage_preview AS
WITH cand AS (
  SELECT cs.customer_id,
         COALESCE(cf.name,'')                                        AS name,
         cf.label                                                    AS label,
         ROUND((EXTRACT(EPOCH FROM (now() - cs.last_operator_reply_at))/3600)::numeric, 1) AS hrs_since_reply,
         COALESCE(cs.followup_count,0)                               AS fc,
         COALESCE(cm.mode,'approval')                                AS mode,
         (cf.label_locked_until > now())                             AS locked
  FROM conversation_state cs
  LEFT JOIN customer_facts cf USING (customer_id)
  LEFT JOIN LATERAL (SELECT mode FROM conversation_modes
                     WHERE customer_id = cs.customer_id
                     ORDER BY id DESC LIMIT 1) cm ON TRUE
  WHERE cs.last_operator_reply_at IS NOT NULL
    AND (cs.last_customer_message_at IS NULL
         OR cs.last_operator_reply_at > cs.last_customer_message_at)
    AND now() - cs.last_operator_reply_at > interval '24 hours'
    AND now() - cs.last_operator_reply_at < interval '14 days'
    AND (cs.last_nudge_drafted_at IS NULL
         OR now() - cs.last_nudge_drafted_at > interval '48 hours')
    AND COALESCE(cs.followup_count,0) < 2
    AND (cf.label IS NULL OR cf.label NOT IN
         ('WAITING_FOR_PAYMENT','CONFIRMED','PAUSED_SPAM','PAUSED_B2B',
          'PAUSED_PERSONAL','DISREGARDED','LOST'))
    AND NOT EXISTS (SELECT 1 FROM autonomous_sends a
                    WHERE a.customer_id = cs.customer_id
                      AND a.kind = 'payment_link_sent'
                      AND a.sent_at > now() - interval '24 hours')
)
SELECT *,
  CASE
    WHEN label IN ('HOT','NEEDS_ATTENTION','WARM') AND hrs_since_reply BETWEEN 24 AND 72 THEN 'soft_checkin'
    WHEN label IN ('HOT','NEEDS_ATTENTION','WARM','COLD') AND hrs_since_reply > 72 AND hrs_since_reply <= 336 THEN 'last_shot'
    ELSE NULL
  END AS win
FROM cand
WHERE NOT COALESCE(locked, false) AND mode <> 'autonomous';

\echo == DISTRIBUTION (window x label) ==
SELECT win, label, count(*) AS n FROM _reengage_preview
WHERE win IS NOT NULL GROUP BY win, label ORDER BY win, label;

\echo == TOTAL TO BE NUDGED (sweep cap=10/sweep) ==
SELECT count(*) AS total_targets FROM _reengage_preview WHERE win IS NOT NULL;

\echo == SAMPLE (longest-ghosted first, top 15) ==
SELECT win, label, hrs_since_reply, fc, left(name,24) AS name
FROM _reengage_preview WHERE win IS NOT NULL
ORDER BY hrs_since_reply DESC LIMIT 15;

\echo == CONTROL: rows the OLD inverted query targeted (we-owe-reply) ==
SELECT count(*) AS old_we_owe_population
FROM conversation_state cs LEFT JOIN customer_facts cf USING (customer_id)
WHERE cs.last_customer_message_at IS NOT NULL
  AND (cs.last_operator_reply_at IS NULL
       OR cs.last_operator_reply_at < cs.last_customer_message_at)
  AND now() - cs.last_customer_message_at > interval '30 minutes'
  AND now() - cs.last_customer_message_at < interval '7 days';
