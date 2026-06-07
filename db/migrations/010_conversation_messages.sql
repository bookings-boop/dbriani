-- 010_conversation_messages.sql — durable, append-only conversation store.
--
-- THE keystone reliability fix. Today hermes_analyze_lead + the drafter run on a
-- LIVE WAHA fetch that evicts/truncates history (measured 8% empty, 17% degraded;
-- a 157-msg CONFIRMED booking analyzed as first-contact -> scored 0/100). There is
-- NO durable transcript anywhere in the bridge DB (audited migrations 001-009:
-- customer_facts = extracted facts only, conversation_state = timings only,
-- draft_log = the single triggering inbound only). This table persists every
-- inbound + outbound message BODY so analysis is never at WAHA's mercy.
--
-- ADDITIVE ONLY + APPEND-ONLY: it only RECORDS messages — it never changes what is
-- sent and is never UPDATEd/DELETEd by the bridge (hence GRANT is SELECT+INSERT,
-- not UPDATE/DELETE). The write helper (server.record_message) is FAIL-SAFE and
-- runs on every message, so it must tolerate this table NOT yet existing: the
-- OPERATOR applies this migration as table-owner (hermes_rw cannot CREATE/ALTER),
-- and until then every INSERT no-ops (the helper swallows the relation-absent error).
--
-- Idempotent ingest: a partial UNIQUE index on (customer_id, msg_id) dedupes a
-- replayed message carrying the same provider msg_id; rows with no stable id
-- (msg_id NULL) are always appended. Additive; safe to re-run.

BEGIN;

CREATE TABLE IF NOT EXISTS conversation_messages (
  id           BIGSERIAL PRIMARY KEY,
  customer_id  TEXT        NOT NULL,
  ts           TIMESTAMPTZ NOT NULL DEFAULT now(),
  direction    TEXT        NOT NULL CHECK (direction IN ('in','out')),
  body         TEXT,
  msg_id       TEXT,
  created_at   TIMESTAMPTZ DEFAULT now()
);

-- Time-ordered per-customer history read (the analyzer / backfill scan path).
CREATE INDEX IF NOT EXISTS idx_conversation_messages_cid_ts
  ON conversation_messages (customer_id, ts);

-- Idempotent ingest. UNIQUE on (customer_id, msg_id) but PARTIAL so that rows
-- with no stable provider id (msg_id IS NULL) are never deduped and always
-- appended. The INSERT ... ON CONFLICT in record_message specifies the matching
-- WHERE predicate so this partial index is used as the conflict arbiter.
CREATE UNIQUE INDEX IF NOT EXISTS idx_conversation_messages_cid_msgid
  ON conversation_messages (customer_id, msg_id)
  WHERE msg_id IS NOT NULL;

-- The bridge writes as the limited BRIDGE_PG_USER role (default hermes_rw). It
-- only ever appends + reads, never updates/deletes. Idempotent; harmless if
-- hermes_rw already owns the table.
GRANT SELECT, INSERT ON conversation_messages TO hermes_rw;
GRANT USAGE, SELECT ON SEQUENCE conversation_messages_id_seq TO hermes_rw;

COMMIT;
