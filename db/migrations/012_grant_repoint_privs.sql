-- 012_grant_repoint_privs.sql (2026-06-09)
-- 4-B identity re-point on merge (handle_reconcile_identities + server._repoint_identity_sql)
-- folds a merged dup's durable rows onto its canonical survivor. That requires the
-- bridge role (hermes_rw) to MOVE and DELETE durable rows:
--   * conversation_messages: UPDATE customer_id (move dup rows to canon) +
--     DELETE the dup's msg_ids already present under canon (shared echoes).
--     (It was previously append-only for hermes_rw: SELECT/INSERT only.)
--   * conversation_state: DELETE the dup's state row after the GREATEST-merge.
-- Grant exactly those. Applied live on the box 2026-06-09 (owner n8n).
GRANT UPDATE, DELETE ON conversation_messages TO hermes_rw;
GRANT DELETE ON conversation_state TO hermes_rw;
