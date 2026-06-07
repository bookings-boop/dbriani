#!/usr/bin/env python3
"""AREA C / PART C2 — read-path-persist (Wave-2 eviction-resilience).

build_analyzer_history reads the durable conversation_messages store and TOPS
UP with a live WAHA fetch for anything newer than the last stored ts. PART C2:
after a successful top-up, PERSIST those top-up rows back into the durable store
via record_message (fail-safe, ON CONFLICT DO NOTHING) so each analysis
incrementally durably-captures current WAHA history per-lead — no bulk load that
degrades WAHA, idempotent, and NEVER breaks the read.

Locks:
  - top-up rows (and ONLY the newer rows) are persisted via record_message with
    their direction + body + msg_id;
  - a record_message failure / exception never breaks the returned history;
  - no top-up (WAHA empty or all older) -> no persist calls;
  - the durable-fallback path (table absent / empty cid) does not persist.

record_message is monkeypatched (no DB). Run:
  python3 hermes-bridge/test_readpath_persist.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402

FS = server._CONV_FS
NL = server._CONV_NL


def _enc(ts, direction, body, msg_id=""):
    body_enc = (body or "").replace("\n", NL)
    return FS.join([str(ts), direction, body_enc, msg_id or ""])


class _Patch:
    """Swap server._psql / waha_fetch_history / waha_fetch_raw /
    canonicalize_cid / record_message for a block, capturing record_message."""

    def __init__(self, psql=None, waha_history=None, waha_raw=None,
                 record_raises=False):
        self._psql = psql
        self._wh = waha_history
        self._wr = waha_raw
        self._record_raises = record_raises
        self.records = []

    def _rec(self, cid, direction, body, msg_id=None):
        self.records.append((cid, direction, body, msg_id))
        if self._record_raises:
            raise RuntimeError("record_message exploded")
        return True

    def __enter__(self):
        self._o_psql = server._psql
        self._o_wh = server.waha_fetch_history
        self._o_wr = server.waha_fetch_raw
        self._o_canon = server.canonicalize_cid
        self._o_rec = server.record_message
        if self._psql is not None:
            server._psql = self._psql
        if self._wh is not None:
            server.waha_fetch_history = self._wh
        if self._wr is not None:
            server.waha_fetch_raw = self._wr
        server.canonicalize_cid = lambda c: c
        server.record_message = self._rec
        return self

    def __exit__(self, *exc):
        server._psql = self._o_psql
        server.waha_fetch_history = self._o_wh
        server.waha_fetch_raw = self._o_wr
        server.canonicalize_cid = self._o_canon
        server.record_message = self._o_rec
        return False


_SENTINEL = {"history": "WAHA-ONLY-FALLBACK", "last_message": "",
             "push_name": "", "count": 0, "err": None}


def test_topup_rows_are_persisted():
    now = int(time.time())
    durable_out = "\n".join([
        _enc(now - 7200, "in", "I want to book Saturday", "D1"),
        _enc(now - 3600, "out", "Great — deposit is AED 2000", "D2"),
    ]) + "\n"

    def waha_raw(cid, **k):
        return [
            {"ts": now - 99999, "direction": "in",
             "body": "ANCIENT older than durable", "msg_id": "OLD"},
            {"ts": now - 120, "direction": "out",
             "body": "any update on the deposit?", "msg_id": "NEW1"},
            {"ts": now - 60, "direction": "in",
             "body": "paid just now", "msg_id": "NEW2"},
        ]

    with _Patch(psql=lambda sql, **k: (durable_out, None),
                waha_history=lambda cid, **k: dict(_SENTINEL),
                waha_raw=waha_raw) as p:
        out = server.build_analyzer_history("x@c.us")
    # read still works
    assert out.get("source") == "durable", out
    assert "paid just now" in out["history"]
    # ONLY the two NEWER rows are persisted (the ancient row is NOT)
    recorded_ids = sorted(r[3] for r in p.records)
    assert recorded_ids == ["NEW1", "NEW2"], p.records
    # direction + body flow through unchanged
    by_id = {r[3]: r for r in p.records}
    assert by_id["NEW1"][1] == "out" and by_id["NEW1"][2] == "any update on the deposit?"
    assert by_id["NEW2"][1] == "in" and by_id["NEW2"][2] == "paid just now"
    # persisted under the same customer id
    assert all(r[0] == "x@c.us" for r in p.records), p.records


def test_no_topup_means_no_persist():
    now = int(time.time())
    durable_out = _enc(now - 100, "in", "durable body", "D1") + "\n"
    # WAHA returns nothing newer than the durable tail -> nothing to persist.
    with _Patch(psql=lambda sql, **k: (durable_out, None),
                waha_history=lambda cid, **k: dict(_SENTINEL),
                waha_raw=lambda cid, **k: []) as p:
        out = server.build_analyzer_history("x@c.us")
    assert out.get("source") == "durable"
    assert p.records == [], p.records


def test_persist_failure_never_breaks_read():
    now = int(time.time())
    durable_out = _enc(now - 7200, "in", "durable", "D1") + "\n"

    def waha_raw(cid, **k):
        return [{"ts": now - 30, "direction": "in",
                 "body": "newest", "msg_id": "NEW"}]

    with _Patch(psql=lambda sql, **k: (durable_out, None),
                waha_history=lambda cid, **k: dict(_SENTINEL),
                waha_raw=waha_raw, record_raises=True) as p:
        out = server.build_analyzer_history("x@c.us")
    # even though record_message raised, the read returns the topped-up history
    assert out.get("source") == "durable", out
    assert "newest" in out["history"], out
    assert len(p.records) == 1, p.records


def test_topup_failure_does_not_persist_and_returns_durable():
    now = int(time.time())
    durable_out = _enc(now - 100, "in", "durable body", "D1") + "\n"

    def waha_raw_boom(cid, **k):
        raise RuntimeError("waha down")

    with _Patch(psql=lambda sql, **k: (durable_out, None),
                waha_history=lambda cid, **k: dict(_SENTINEL),
                waha_raw=waha_raw_boom) as p:
        out = server.build_analyzer_history("x@c.us")
    assert out.get("source") == "durable"
    assert "durable body" in out["history"]
    assert p.records == [], p.records


def test_waha_only_fallback_does_not_persist():
    # Empty / absent store -> WAHA-only fallback path; that path does not run
    # the top-up persist (no durable rows to top up against).
    with _Patch(psql=lambda sql, **k: ("", None),
                waha_history=lambda cid, **k: dict(_SENTINEL),
                waha_raw=lambda cid, **k: [
                    {"ts": 1, "direction": "in", "body": "x", "msg_id": "Z"}]) as p:
        out = server.build_analyzer_history("x@c.us")
    assert out["history"] == "WAHA-ONLY-FALLBACK"
    assert p.records == [], p.records


def test_blank_body_topup_rows_not_persisted():
    now = int(time.time())
    durable_out = _enc(now - 7200, "in", "durable", "D1") + "\n"

    def waha_raw(cid, **k):
        return [
            {"ts": now - 50, "direction": "in", "body": "   ", "msg_id": "BLANK"},
            {"ts": now - 40, "direction": "in", "body": "real", "msg_id": "REAL"},
        ]

    with _Patch(psql=lambda sql, **k: (durable_out, None),
                waha_history=lambda cid, **k: dict(_SENTINEL),
                waha_raw=waha_raw) as p:
        server.build_analyzer_history("x@c.us")
    ids = sorted(r[3] for r in p.records)
    assert ids == ["REAL"], p.records


def test_build_analyzer_history_references_record_message():
    assert "record_message" in server.build_analyzer_history.__code__.co_names


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
