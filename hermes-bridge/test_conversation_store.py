#!/usr/bin/env python3
"""Durable conversation store — fail-safe write helper (migration 010).

record_message() runs on EVERY inbound + outbound, so — exactly like
_draft_log_write — it MUST NEVER raise and MUST NEVER break message flow,
especially BEFORE migration 010 is applied (table absent -> psql returns a
relation-does-not-exist error -> must be caught + return False, no-op).

This locks: (1) the pure _record_message_sql() builder emits the exact
idempotent INSERT ... ON CONFLICT shape; (2) record_message returns False
(never raises) when _psql raises OR when _psql returns an error (table
absent), and True only on a clean insert.

Run: python3 hermes-bridge/test_conversation_store.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402


class _Patch:
    """Swap server._psql (and pin canonicalize_cid to identity so the test
    is hermetic — no docker, deterministic cid) for the duration of a block."""

    def __init__(self, psql):
        self._psql = psql

    def __enter__(self):
        self._orig_psql = server._psql
        self._orig_canon = server.canonicalize_cid
        server._psql = self._psql
        server.canonicalize_cid = lambda c: c
        return self

    def __exit__(self, *exc):
        server._psql = self._orig_psql
        server.canonicalize_cid = self._orig_canon
        return False


# --- pure SQL builder -------------------------------------------------

def test_sql_builder_idempotent_shape():
    sql = server._record_message_sql("x@c.us", "in", "hello", "MID1")
    assert "INSERT INTO conversation_messages" in sql
    for col in ("customer_id", "direction", "body", "msg_id"):
        assert col in sql
    # Idempotent ingest against the PARTIAL unique index from migration 010 —
    # the WHERE predicate is required so the partial index is the arbiter.
    assert ("ON CONFLICT (customer_id, msg_id) WHERE msg_id IS NOT NULL "
            "DO NOTHING") in sql
    for lit in ("'x@c.us'", "'in'", "'hello'", "'MID1'"):
        assert lit in sql


def test_sql_builder_escapes_quotes():
    sql = server._record_message_sql("x@c.us", "out", "it's a yacht", None)
    assert "'it''s a yacht'" in sql  # single-quote doubled, no injection


def test_sql_builder_null_msgid_always_appended():
    # No stable provider id -> msg_id literal is NULL (never deduped).
    sql = server._record_message_sql("x@c.us", "out", "hi", None)
    assert ", NULL)" in sql
    assert sql.rstrip().endswith("DO NOTHING")


# --- fail-safe behaviour ---------------------------------------------

def test_returns_false_when_psql_raises():
    def boom(sql, **k):
        raise RuntimeError("psql exploded")
    with _Patch(boom):
        assert server.record_message("x@c.us", "in", "hi", "M1") is False


def test_returns_false_when_table_absent():
    # BEFORE migration 010: psql exits non-zero -> (None, err). Must no-op.
    err = 'ERROR:  relation "conversation_messages" does not exist'
    with _Patch(lambda sql, **k: (None, err)):
        assert server.record_message("x@c.us", "in", "hi", "M1") is False


def test_returns_true_on_clean_insert():
    captured = {}

    def ok(sql, **k):
        captured["sql"] = sql
        return ("INSERT 0 1", None)
    with _Patch(ok):
        assert server.record_message("x@c.us", "out", "hello", "M2") is True
    assert "conversation_messages" in captured["sql"]
    assert "'M2'" in captured["sql"]


def test_invalid_direction_returns_false_without_db():
    # Cheap validation before any _psql call — a bad direction never touches DB.
    def must_not_call(sql, **k):
        raise AssertionError("_psql must not be called for invalid direction")
    with _Patch(must_not_call):
        assert server.record_message("x@c.us", "sideways", "hi") is False


def test_empty_cid_returns_false_without_db():
    def must_not_call(sql, **k):
        raise AssertionError("_psql must not be called for empty cid")
    with _Patch(must_not_call):
        assert server.record_message("", "in", "hi") is False
        assert server.record_message(None, "in", "hi") is False


def test_none_body_is_tolerated():
    # An outbound with no text body must still be recorded (body -> NULL), True.
    with _Patch(lambda sql, **k: ("INSERT 0 1", None)):
        assert server.record_message("x@c.us", "out", None, "M3") is True


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn(); print("PASS", fn.__name__)
        except AssertionError as e:
            failed += 1; print("FAIL", fn.__name__, "-", e or "assert")
        except Exception as e:
            failed += 1; print("ERROR", fn.__name__, "-", repr(e))
    print(f"{len(fns)-failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
