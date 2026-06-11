#!/usr/bin/env python3
"""BUG-3 forward-gate (2026-06-12): operator-forwarded customer messages must
not mint ghost captain/crew leads. Tests the deterministic provenance layer:

  waha._waha_is_fwd(m)          — is a raw WAHA msg a forward?
  server._forward_seeded(rows)  — is a thread operator-forward-seeded?
  server._format_conv_history   — forwarded outbound rendered as NOT-our-words

The intake gate (handle_debounce) and analyzer short-circuit (_analyze_one)
are integration paths proven live on the two real ghost threads; the pure
predicates below are the heart of the gate.

Run: python3 hermes-bridge/test_forward_gate.py
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from waha import _waha_is_fwd  # noqa: E402
from server import _forward_seeded, _format_conv_history  # noqa: E402


# --- _waha_is_fwd: the four marker shapes WAHA uses --------------------------
def test_fwd_toplevel_isforwarded():
    assert _waha_is_fwd({"fromMe": True, "isForwarded": True}) is True


def test_fwd_data_isforwarded():
    assert _waha_is_fwd({"fromMe": True, "_data": {"isForwarded": True}}) is True


def test_fwd_data_forwardingscore_int():
    assert _waha_is_fwd({"_data": {"forwardingScore": 3}}) is True
    assert _waha_is_fwd({"_data": {"forwardingScore": 1}}) is True


def test_fwd_toplevel_forwardingscore():
    assert _waha_is_fwd({"forwardingScore": 2}) is True


def test_not_fwd_absent():
    assert _waha_is_fwd({"fromMe": True, "body": "our own words"}) is False
    assert _waha_is_fwd({"_data": {"isForwarded": False,
                                   "forwardingScore": None}}) is False
    assert _waha_is_fwd({"_data": {"forwardingScore": 0}}) is False


def test_fwd_none_safe():
    assert _waha_is_fwd(None) is False
    assert _waha_is_fwd("not a dict") is False
    assert _waha_is_fwd({}) is False


# --- _forward_seeded: thread-shape predicate (raw WAHA rows) -----------------
def _row(direction, fwd, body="x", ts=1):
    return {"ts": ts, "direction": direction, "body": body,
            "msg_id": None, "fwd": fwd}


def test_seeded_all_out_forwarded_plus_inbound():
    # the ghost shape: every Dubriani-side msg is a forward, captain replied
    rows = [_row("out", True), _row("out", True), _row("out", True),
            _row("in", False)]
    assert _forward_seeded(rows) is True


def test_not_seeded_one_genuine_outbound():
    # a single genuine Dubriani message permanently disarms the gate
    rows = [_row("out", True), _row("out", False), _row("in", False)]
    assert _forward_seeded(rows) is False


def test_not_seeded_no_inbound():
    # forwards with no reply yet — not yet a "recipient is a lead" situation
    rows = [_row("out", True), _row("out", True)]
    assert _forward_seeded(rows) is False


def test_not_seeded_no_outbound():
    # pure inbound (a normal new customer) — never seeded
    rows = [_row("in", False), _row("in", False)]
    assert _forward_seeded(rows) is False


def test_not_seeded_empty_and_none():
    assert _forward_seeded([]) is False
    assert _forward_seeded(None) is False
    assert _forward_seeded([None, "junk", 3]) is False


def test_seeded_genuine_customer_thread_is_false():
    # normal thread: customer in, Dubriani replies (not forwarded)
    rows = [_row("in", False, "hi can I book"), _row("out", False, "sure!"),
            _row("in", False, "great")]
    assert _forward_seeded(rows) is False


# --- _format_conv_history: forwarded outbound must read as NOT our words -----
def test_format_tags_forwarded_outbound():
    now = int(time.time())
    rows = [{"direction": "out", "fwd": True, "body": "wife's words", "ts": now},
            {"direction": "in", "fwd": False, "body": "captain reply", "ts": now}]
    out = _format_conv_history(rows, now)
    assert "FORWARDED 3rd-party content" in out
    # the forwarded line must NOT read as a plain "Dubriani (...)" line
    assert 'Dubriani (' not in out.split("\n")[0]
    # genuine inbound still renders as Customer
    assert 'Customer (' in out


def test_format_genuine_outbound_unchanged():
    now = int(time.time())
    rows = [{"direction": "out", "fwd": False, "body": "our reply", "ts": now}]
    out = _format_conv_history(rows, now)
    assert out.startswith('Dubriani (')
    assert "FORWARDED" not in out


# --- golden fixture: the real captured ghost thread --------------------------
def test_golden_suliman_fixture():
    """The real captured payloads (RCA workspace) reduced to raw-WAHA shape:
    4 forwarded fromMe + 1 captain auto-reply → forward-seeded True."""
    p = ("/Users/macbook/hermes-pipeline-rca-2026-06-12/"
         "suliman_sample_msgs.json")
    if not os.path.exists(p):
        print("SKIP test_golden_suliman_fixture (fixture not present)")
        return
    raw = json.load(open(p))
    # the capture stored _data under _data_subset — remap to the real key so
    # _waha_is_fwd sees the true shape.
    msgs = []
    for m in raw:
        mm = dict(m)
        if "_data_subset" in mm:
            mm["_data"] = mm["_data_subset"]
        msgs.append(mm)
    # every fromMe message in this thread is a forward
    for m in msgs:
        if m.get("fromMe"):
            assert _waha_is_fwd(m) is True, m.get("body", "")[:40]
        else:
            assert _waha_is_fwd(m) is False
    # build the raw rows the way waha_fetch_raw would and assert seeded
    rows = [{"ts": m.get("timestamp") or 0,
             "direction": "out" if m.get("fromMe") else "in",
             "body": m.get("body") or "", "msg_id": None,
             "fwd": _waha_is_fwd(m)} for m in msgs]
    assert _forward_seeded(rows) is True


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} forward_gate tests passed")
