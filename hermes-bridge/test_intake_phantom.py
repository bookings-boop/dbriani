#!/usr/bin/env python3
"""Unit tests for the intake-gap phantom-suppression guard (_is_phantom).

Critical safety property under test: we suppress ONLY a hollow WAHA stub that the
bridge DEFINITIVELY confirms has no message — and we KEEP alerting on a transport
error (resp == {}), so a genuine dropped lead is never hidden by a network
hiccup. Run: python3 test_intake_phantom.py"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# cron-intake-gap.py does `sys.path.insert(0, ~/hermes-bridge)` then
# `from intake import ...`. Point HOME at the repo root so that resolves to THIS
# dir's intake.py (not any stray ~/hermes-bridge), and .env is simply absent.
os.environ["HOME"] = os.path.dirname(HERE)
sys.path.insert(0, HERE)

spec = importlib.util.spec_from_file_location(
    "cron_intake_gap", os.path.join(HERE, "cron-intake-gap.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
_is_phantom = mod._is_phantom

# The exact production false-positive: 44861017321513@lid, no name, no text,
# 1e9 no-timestamp sentinel; auto-ingest replied "no messages in WAHA".
HOLLOW = {"cid": "44861017321513@lid", "name": "", "body": "",
          "age_days": 1000000000.0}
REAL = {"cid": "971501234567@c.us", "name": "Omar",
        "body": "Hi, is the yacht available Saturday?", "age_days": 0.4}

NO_MSG_RESP = {"ok": False, "error": "no messages in WAHA"}
OK_RESP = {"ok": True}
TRANSPORT_ERR = {}  # refresh_facts() returns {} on a transport/network error

CASES = [
    # (label, gap, resp, expected)
    ("undated (1e9) content-less + definitive no-msg -> phantom (suppress)",
     HOLLOW, NO_MSG_RESP, True),
    ("STALE (20d) content-less + definitive no-msg -> phantom (suppress)",
     dict(HOLLOW, age_days=20.0), NO_MSG_RESP, True),
    ("stale (8d) content-less + definitive no-msg -> phantom (suppress)",
     dict(HOLLOW, age_days=8.0), NO_MSG_RESP, True),
    ("FRESH (2d) content-less + definitive no-msg -> NOT phantom (nudge)",
     dict(HOLLOW, age_days=2.0), NO_MSG_RESP, False),
    ("undated content-less + TRANSPORT error -> NOT phantom (still alert)",
     HOLLOW, TRANSPORT_ERR, False),
    ("undated content-less + ingest OK -> NOT phantom (it got recovered)",
     HOLLOW, OK_RESP, False),
    ("real lead w/ body (stale) + no-msg resp -> NOT phantom (has text)",
     dict(REAL, age_days=30.0), NO_MSG_RESP, False),
    ("real lead w/ body + transport error -> NOT phantom",
     REAL, TRANSPORT_ERR, False),
    ("stale but has a name -> NOT phantom (some identity present)",
     dict(HOLLOW, age_days=30.0, name="Maybe Someone"), NO_MSG_RESP, False),
    ("stale but has body text -> NOT phantom (a message exists)",
     dict(HOLLOW, age_days=30.0, body="hello?"), NO_MSG_RESP, False),
    ("age_days None -> treated as 0 (fresh), NOT phantom",
     dict(HOLLOW, age_days=None), NO_MSG_RESP, False),
]


def main():
    failed = 0
    for label, gap, resp, expected in CASES:
        got = _is_phantom(gap, resp)
        ok = got is expected
        print("%s %s" % ("PASS" if ok else "FAIL", label))
        if not ok:
            failed += 1
            print("     expected %r, got %r" % (expected, got))
    print("\n%d/%d passed" % (len(CASES) - failed, len(CASES)))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
