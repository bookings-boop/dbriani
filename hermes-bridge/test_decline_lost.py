#!/usr/bin/env python3
"""_DECLINE_RE — polite-decline / found-elsewhere -> LOST detection (2026-06-07).

Bug: "yes we found thank you" (Anya) wasn't matched -> not LOST -> no graceful
goodbye -> model went silent (rule 55) -> n8n stamped it "spam/B2B". Broadened
to catch bare "we found" / "found one", but a false LOST sends a goodbye to an
ACTIVE lead (terminal label), so benign "found" phrasing MUST NOT match.

Plain-assert. Run: python3 hermes-bridge/test_decline_lost.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from server import _DECLINE_RE  # noqa: E402


def _is_decline(msg):
    # mirror the classifier gate: decline match AND not a question
    return bool(_DECLINE_RE.search(msg)) and "?" not in msg


MUST_DECLINE = [
    "yes we found thank you",
    "we found another option",
    "we found one thanks",
    "found one already",
    "we've found someone else",
    "no thanks",
    "not interested",
    "booked elsewhere",
    "all good thanks",
    "changed my mind",
]

MUST_NOT_DECLINE = [
    "we found the location easily",        # benign object
    "we found your number, thanks",        # benign object
    "we found a date that works",          # picking a date = positive
    "found it, thanks for the pin",        # 'it' benign
    "did you find a yacht for us?",        # question
    "can you check if you found a slot?",  # question
    "we're good to go!",                   # positive confirm
    "we found you on instagram",           # 'you' benign
    "yes let's do it",                     # positive booking
    "we found us a great match with you",  # positive (rare, 'us' benign)
]


def test_must_detect_declines():
    for m in MUST_DECLINE:
        assert _is_decline(m), f"should be LOST: {m!r}"


def test_must_not_false_positive():
    for m in MUST_NOT_DECLINE:
        assert not _is_decline(m), f"must NOT be LOST: {m!r}"


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} decline-lost tests passed")
