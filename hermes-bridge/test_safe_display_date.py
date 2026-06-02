#!/usr/bin/env python3
"""5d (2026-06-02): lead "Emilie" showed the booking as "tomorrow" while it had
already passed. Root cause: customer_facts.dates stores the LITERAL relative word
the customer typed days ago ("tomorrow") and the /review card prints it verbatim
forever — it never gets resolved to a calendar date, so it goes stale AND defeats
every passed-date safeguard (_parse_booking_date returns None for "tomorrow").

This render guard FLAGS a bare unresolvable relative word so the operator does
not trust a stale "tomorrow". (The deeper fix — resolve relative->absolute at
capture, anchored to the message timestamp — is a separate, careful change.)

Run: python3 hermes-bridge/test_safe_display_date.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from labels import _safe_display_date  # noqa: E402


def test_empty_is_no_date():
    assert _safe_display_date("") == "no date"
    assert _safe_display_date(None) == "no date"


def test_relative_words_flagged():
    for w in ("tomorrow", "today", "tonight", "this weekend", "next week",
              "Friday night"):
        out = _safe_display_date(w)
        assert w in out and "reconfirm" in out.lower(), out


def test_absolute_date_passes_through():
    # A resolvable calendar date is fine as-is (passed-date logic handles it).
    assert _safe_display_date("Jun 6") == "Jun 6"
    assert _safe_display_date("Sat May 23") == "Sat May 23"


def test_non_relative_freetext_unchanged():
    # Not relative + not parseable -> leave it alone (don't add noise).
    assert _safe_display_date("end of the month") == "end of the month"


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} safe-display-date tests passed")
