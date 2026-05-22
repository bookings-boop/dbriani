#!/usr/bin/env python3
"""Unit tests for the pure customer-facts helpers (feature-header).

No test framework — plain asserts, matching the project's style.
Run:  python3 hermes-bridge/test_customer_facts.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from server import _facts_extract_gate, build_customer_header  # noqa: E402

# emoji as escapes so the test does not depend on source-file encoding
PERSON = "\U0001F464"        # 👤
CAL = "\U0001F4C5"           # 📅
BOAT = "\U0001F6E5️"    # 🛥️
PEOPLE = "\U0001F465"        # 👥
NUM = "\U0001F522"           # 🔢


def test_gate():
    assert _facts_extract_gate("we are 6 people") is True       # digit
    assert _facts_extract_gate("interested in the Satoshi") is True   # yacht
    assert _facts_extract_gate("can we do Saturday?") is True    # date word
    assert _facts_extract_gate("hi, I'm Mark") is True           # name pattern
    assert _facts_extract_gate("we want to book") is True        # booking word
    assert _facts_extract_gate("ok thanks!") is False            # nothing
    assert _facts_extract_gate("sounds great") is False
    assert _facts_extract_gate("") is False
    assert _facts_extract_gate(None) is False


def test_header_full():
    h = build_customer_header({
        "name": "Mark Hassan", "dates": "Saturday Dec 14",
        "yachts": "Aurora II, Satoshi", "party_size": "6-8 guests",
        "message_count": 4})
    assert PERSON + " Mark Hassan" in h
    assert CAL + " Interested in: Saturday Dec 14" in h
    assert BOAT + " Looking at: Aurora II, Satoshi" in h
    assert PEOPLE + " Party size: 6-8 guests" in h
    assert NUM + " Message #4 in conversation" in h
    assert h.rstrip().endswith("─" * 30)


def test_header_sparse():
    h = build_customer_header({
        "name": "", "dates": "", "yachts": "", "party_size": "",
        "message_count": 1})
    assert PERSON + " New contact" in h          # name fallback
    assert NUM + " Message #1 in conversation" in h
    assert CAL not in h and BOAT not in h        # empty facts omit their lines


def test_header_bad_count():
    # a non-numeric message_count must not crash the header
    h = build_customer_header({"name": "X", "message_count": None})
    assert NUM + " Message #0 in conversation" in h


if __name__ == "__main__":
    for fn in (test_gate, test_header_full, test_header_sparse,
               test_header_bad_count):
        fn()
        print("PASS", fn.__name__)
    print("all customer-facts unit tests passed")
