#!/usr/bin/env python3
"""PART 3a — durable READ-PATH for the analyzer (migration 010).

hermes_analyze_lead is fed `history`. Today that history comes from a LIVE WAHA
fetch (routes._analyze_one) which evicts/truncates (8% empty, 17% degraded; a
157-msg CONFIRMED booking -> WAHA 0 -> scored 0/100). PART 3a repoints the
history source to the durable conversation_messages store, TOPPED UP with a live
WAHA fetch for anything newer than the last stored ts, and FALLS BACK to today's
WAHA-only fetch when the table is empty/absent for a cid.

This locks:
  - the pure SQL builder + parser (safe encoding: bodies contain '|' + newlines,
    and _psql is fixed `-tA` so we encode fields with US/RS control chars);
  - smarter-than-`with_body[-20:]` windowing: earliest booking-signal messages +
    a recent window so a long booking's date/payment is never lost;
  - the analyzer history-line format (identical to waha_fetch_history);
  - build_analyzer_history FAIL-SAFE behaviour: graceful WAHA-only fallback when
    the table is absent (psql error), empty for the cid, or _psql raises.

Run: python3 hermes-bridge/test_durable_read.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402

FS = server._CONV_FS
NL = server._CONV_NL


def _enc(ts, direction, body, msg_id=""):
    """Encode one row exactly as _durable_history_sql would (body newlines ->
    RS placeholder, fields joined by US)."""
    body_enc = (body or "").replace("\n", NL)
    return FS.join([str(ts), direction, body_enc, msg_id or ""])


class _Patch:
    """Hermetic swap of server._psql / waha_fetch_history / waha_fetch_raw /
    canonicalize_cid for the duration of a block."""

    def __init__(self, psql=None, waha_history=None, waha_raw=None):
        self._psql = psql
        self._wh = waha_history
        self._wr = waha_raw

    def __enter__(self):
        self._o_psql = server._psql
        self._o_wh = server.waha_fetch_history
        self._o_wr = server.waha_fetch_raw
        self._o_canon = server.canonicalize_cid
        if self._psql is not None:
            server._psql = self._psql
        if self._wh is not None:
            server.waha_fetch_history = self._wh
        if self._wr is not None:
            server.waha_fetch_raw = self._wr
        server.canonicalize_cid = lambda c: c
        return self

    def __exit__(self, *exc):
        server._psql = self._o_psql
        server.waha_fetch_history = self._o_wh
        server.waha_fetch_raw = self._o_wr
        server.canonicalize_cid = self._o_canon
        return False


# --- pure SQL builder + parser ---------------------------------------

def test_durable_history_sql_shape():
    sql = server._durable_history_sql("971547366777@c.us")
    assert "FROM conversation_messages" in sql
    assert "ORDER BY ts ASC" in sql
    assert "'971547366777@c.us'" in sql
    # Safe encoding: control-char field/newline separators so bodies with '|'
    # and embedded newlines survive psql -tA single-column output.
    assert "chr(31)" in sql and "chr(30)" in sql
    assert "EXTRACT(EPOCH FROM ts)" in sql


def test_parse_conv_rows_roundtrip():
    out = "\n".join([
        _enc(1000, "in", "hi there", "M1"),
        _enc(2000, "out", "line one\nline two", "M2"),
        _enc(3000, "in", "uses a | pipe", ""),
    ]) + "\n"
    rows = server._parse_conv_rows(out)
    assert len(rows) == 3, rows
    assert rows[0] == {"ts": 1000, "direction": "in",
                       "body": "hi there", "msg_id": "M1"}
    # RS placeholder decoded back to a real newline
    assert rows[1]["body"] == "line one\nline two"
    assert rows[1]["direction"] == "out"
    # pipe preserved; empty msg_id -> None
    assert rows[2]["body"] == "uses a | pipe"
    assert rows[2]["msg_id"] is None


def test_parse_conv_rows_tolerates_garbage():
    # Bad / short lines are skipped, never raise.
    out = "not-an-int" + FS + "in" + FS + "x" + FS + "M\n\n" + _enc(5, "in", "ok")
    rows = server._parse_conv_rows(out)
    assert [r["body"] for r in rows] == ["ok"], rows


# --- booking-signal windowing ----------------------------------------

def test_has_booking_signal():
    assert server._has_booking_signal("I'll send the deposit tomorrow")
    assert server._has_booking_signal("payment link please")
    assert server._has_booking_signal("booking for Saturday 4-7pm")
    assert not server._has_booking_signal("hello, just looking around")


def test_window_short_chat_returns_all():
    rows = [{"ts": i, "direction": "in", "body": f"msg {i}"}
            for i in range(10)]
    window, truncated = server._select_history_window(rows)
    assert truncated is False
    assert len(window) == 10


def test_window_long_chat_keeps_earliest_signal_plus_recent():
    # 60-msg chat: the booking/deposit is message #2 (earliest), the rest is
    # chatter + a recent tail. The blunt `with_body[-20:]` would DROP the
    # booking; our window must keep it.
    rows = []
    rows.append({"ts": 100, "direction": "in", "body": "hi"})
    rows.append({"ts": 101, "direction": "in",
                 "body": "deposit sent for Saturday booking"})  # earliest signal
    for i in range(2, 60):
        rows.append({"ts": 100 + i, "direction": "in",
                     "body": f"random chatter {i}"})
    window, truncated = server._select_history_window(
        rows, recent_n=20, max_signal=6)
    assert truncated is True
    bodies = [r["body"] for r in window]
    assert "deposit sent for Saturday booking" in bodies, "earliest signal lost"
    # recent tail present
    assert "random chatter 59" in bodies
    # chronological order preserved (ts ascending)
    ts_seq = [r["ts"] for r in window]
    assert ts_seq == sorted(ts_seq), ts_seq
    # bounded: no more than max_signal + recent_n
    assert len(window) <= 26


# --- history formatting (identical to waha_fetch_history) -------------

def test_format_conv_history_matches_waha_shape():
    now = 100000
    rows = [
        {"ts": now - 7200, "direction": "out", "body": "Welcome to Dubriani"},
        {"ts": now - 600, "direction": "in", "body": "is the boat free?"},
    ]
    h = server._format_conv_history(rows, now)
    lines = h.splitlines()
    assert lines[0].startswith('Dubriani (2h ago): "Welcome to Dubriani"')
    assert lines[1].startswith('Customer (10m ago): "is the boat free?"')


def test_format_truncates_long_body_to_240():
    now = 1000
    rows = [{"ts": now, "direction": "in", "body": "x" * 400}]
    h = server._format_conv_history(rows, now)
    # 240 chars of body inside the quotes
    assert ('"' + "x" * 240 + '"') in h
    assert ("x" * 241) not in h


# --- build_analyzer_history: fail-safe + fallback + top-up ------------

_SENTINEL = {"history": "WAHA-ONLY-FALLBACK", "last_message": "",
             "push_name": "", "count": 0, "err": None}


def test_falls_back_to_waha_when_table_absent():
    err = 'ERROR:  relation "conversation_messages" does not exist'
    with _Patch(psql=lambda sql, **k: (None, err),
                waha_history=lambda cid, **k: dict(_SENTINEL)):
        out = server.build_analyzer_history("x@c.us")
    assert out["history"] == "WAHA-ONLY-FALLBACK"


def test_falls_back_to_waha_when_cid_empty():
    with _Patch(psql=lambda sql, **k: ("", None),
                waha_history=lambda cid, **k: dict(_SENTINEL)):
        out = server.build_analyzer_history("x@c.us")
    assert out["history"] == "WAHA-ONLY-FALLBACK"


def test_never_raises_when_psql_raises():
    def boom(sql, **k):
        raise RuntimeError("psql exploded")
    with _Patch(psql=boom, waha_history=lambda cid, **k: dict(_SENTINEL)):
        out = server.build_analyzer_history("x@c.us")
    assert out["history"] == "WAHA-ONLY-FALLBACK"


def test_durable_present_merges_all_waha_deduped():
    # F1 (2026-06-09): WAHA history is now MERGED IN FULL, deduped against durable
    # by msg_id + (direction, body). Older WAHA rows are NO LONGER dropped — the
    # old 'newer-than-max-only' top-up hid real inbound behind a single recent
    # durable echo (the Devanshu/Youssra hollow 'first contact' bug).
    now = int(time.time())
    durable_out = "\n".join([
        _enc(now - 7200, "in", "I want to book Saturday", "D1"),
        _enc(now - 3600, "out", "Great — deposit is AED 2000", "D2"),
    ]) + "\n"

    def waha_raw(cid, **k):
        return [
            # OLDER than the oldest durable row — old code DROPPED it; F1 keeps it.
            {"ts": now - 99999, "direction": "in",
             "body": "earlier message we used to lose", "msg_id": "OLD"},
            # a WAHA echo of a durable row (same msg_id) — must NOT double-add.
            {"ts": now - 7200, "direction": "in",
             "body": "I want to book Saturday", "msg_id": "D1"},
            # newer than durable — topped up as before.
            {"ts": now - 60, "direction": "in",
             "body": "paid the deposit just now", "msg_id": "NEW"},
        ]

    with _Patch(psql=lambda sql, **k: (durable_out, None),
                waha_history=lambda cid, **k: dict(_SENTINEL),
                waha_raw=waha_raw):
        out = server.build_analyzer_history("x@c.us")
    assert out.get("source") == "durable", out
    h = out["history"]
    assert "I want to book Saturday" in h
    assert "Great — deposit is AED 2000" in h
    assert "paid the deposit just now" in h
    # F1: the OLDER WAHA row is now MERGED, not dropped
    assert "earlier message we used to lose" in h, "F1 must keep older WAHA history"
    # dedup: the D1 echo did NOT double-add (2 durable + OLD + NEW = 4, not 5)
    assert out["count"] == 4, out
    assert out["last_message"] == "paid the deposit just now"


def test_topup_failure_still_returns_durable():
    now = int(time.time())
    durable_out = _enc(now - 100, "in", "durable body", "D1") + "\n"

    def waha_raw_boom(cid, **k):
        raise RuntimeError("waha down")

    with _Patch(psql=lambda sql, **k: (durable_out, None),
                waha_history=lambda cid, **k: dict(_SENTINEL),
                waha_raw=waha_raw_boom):
        out = server.build_analyzer_history("x@c.us")
    assert out.get("source") == "durable"
    assert "durable body" in out["history"]


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
