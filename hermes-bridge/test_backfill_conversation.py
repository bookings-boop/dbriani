#!/usr/bin/env python3
"""PART 3b — idempotent backfill of the durable conversation store (mig 010).

scripts/backfill_conversation_messages.py seeds conversation_messages from
(1) draft_log (incoming_message -> 'in'; final_text/draft_text -> 'out', keyed
by a draft_id-derived msg_id) and (2) each lead's current WAHA history. It is
READ-ONLY except for the INSERTs (which go through the fail-safe
server.record_message, so ON CONFLICT DO NOTHING makes re-runs safe and the
whole thing no-ops if migration 010 is not yet applied).

These tests lock the PURE pieces (SQL builders, the encoded-row decoder, and the
draft_log->rows + WAHA->rows mappers) WITHOUT a DB / WAHA / record_message.

Run: python3 hermes-bridge/test_backfill_conversation.py
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "scripts"))
import backfill_conversation_messages as bf  # noqa: E402

FS = bf._FS
NL = bf._NL


def _enc_row(*fields):
    return FS.join((f or "").replace("\n", NL) for f in fields)


# --- SQL builders ----------------------------------------------------

def test_draftlog_select_sql_shape():
    sql = bf.draftlog_select_sql()
    assert "FROM draft_log" in sql
    for col in ("draft_id", "customer_id", "incoming_message",
                "draft_text", "final_text"):
        assert col in sql, col
    # control-char encoding so bodies with '|' + newlines stay parseable
    assert "chr(31)" in sql and "chr(30)" in sql


def test_active_cids_sql_shape():
    sql = bf.active_cids_sql()
    assert "FROM customer_facts" in sql
    assert "customer_id" in sql
    # never resurrect a merged-away duplicate identity
    assert "merged_into IS NULL" in sql


# --- encoded-row decoder ---------------------------------------------

def test_decode_rows_roundtrip():
    out = "\n".join([
        _enc_row("d1", "x@c.us", "hello", "draft body", "final body"),
        _enc_row("d2", "y@c.us", "multi\nline", "uses | pipe", ""),
    ]) + "\n"
    rows = bf.decode_rows(out, 5)
    assert len(rows) == 2, rows
    assert rows[0] == ["d1", "x@c.us", "hello", "draft body", "final body"]
    # newline restored, pipe preserved
    assert rows[1][2] == "multi\nline"
    assert rows[1][3] == "uses | pipe"


def test_decode_rows_skips_malformed():
    out = "too" + FS + "few\n" + _enc_row("d", "c", "i", "dt", "ft")
    rows = bf.decode_rows(out, 5)
    assert len(rows) == 1, rows


# --- draft_log -> message rows ---------------------------------------

def test_draft_log_in_and_out_with_derived_ids():
    rows = [{"draft_id": "d1", "customer_id": "x@c.us",
             "incoming_message": "is the boat free?",
             "draft_text": "draft", "final_text": "Yes! It's available."}]
    msgs = bf.draft_log_rows_to_messages(rows)
    assert ("x@c.us", "in", "is the boat free?", "draftlog:d1:in") in msgs
    # outbound prefers final_text over draft_text
    assert ("x@c.us", "out", "Yes! It's available.",
            "draftlog:d1:out") in msgs
    assert len(msgs) == 2, msgs


def test_draft_log_uses_draft_text_when_no_final():
    rows = [{"draft_id": "d2", "customer_id": "x@c.us",
             "incoming_message": "", "draft_text": "the drafted reply",
             "final_text": ""}]
    msgs = bf.draft_log_rows_to_messages(rows)
    assert msgs == [("x@c.us", "out", "the drafted reply",
                     "draftlog:d2:out")], msgs


def test_draft_log_skips_missing_cid_and_empty_bodies():
    rows = [
        {"draft_id": "d3", "customer_id": "",   # no cid -> skip entirely
         "incoming_message": "hi", "draft_text": "x", "final_text": ""},
        {"draft_id": "d4", "customer_id": "z@c.us",  # nothing to record
         "incoming_message": "", "draft_text": "", "final_text": ""},
    ]
    assert bf.draft_log_rows_to_messages(rows) == []


def test_draft_log_idempotent_ids_stable_across_runs():
    rows = [{"draft_id": "d9", "customer_id": "x@c.us",
             "incoming_message": "hi", "draft_text": "", "final_text": "bye"}]
    a = bf.draft_log_rows_to_messages(rows)
    b = bf.draft_log_rows_to_messages(rows)
    assert a == b and [m[3] for m in a] == ["draftlog:d9:in",
                                            "draftlog:d9:out"]


# --- WAHA raw -> message records -------------------------------------

def test_waha_rows_use_provider_id_when_present():
    waha = [{"ts": 1000, "direction": "in", "body": "hello", "msg_id": "WID1"}]
    recs = bf.waha_rows_to_records("x@c.us", waha)
    assert recs == [("x@c.us", "in", "hello", "WID1")], recs


def test_waha_rows_synthesize_stable_id_when_no_provider_id():
    waha = [{"ts": 1000, "direction": "out", "body": "hi there",
             "msg_id": None}]
    a = bf.waha_rows_to_records("x@c.us", waha)
    b = bf.waha_rows_to_records("x@c.us", waha)
    assert a == b, "synthetic id must be deterministic (idempotent re-run)"
    cid, direction, body, mid = a[0]
    assert direction == "out" and body == "hi there"
    assert mid and mid.startswith("waha:x@c.us:1000:"), mid


def test_waha_rows_skip_empty_body():
    waha = [{"ts": 1, "direction": "in", "body": "  ", "msg_id": "E"},
            {"ts": 2, "direction": "in", "body": "real", "msg_id": "R"}]
    recs = bf.waha_rows_to_records("x@c.us", waha)
    assert [r[2] for r in recs] == ["real"], recs


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
