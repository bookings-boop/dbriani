#!/usr/bin/env python3
"""_scope_change_hint — interim flag for a CONFIRMED booking with an open thread
(2026-06-07, customer D extended to a 4th hour but the card showed the original
3hrs/paid with no balance). Non-financial reminder; the structured balance
feature is separate. Pure helper.

Run: python3 hermes-bridge/test_scope_change_hint.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from review import _scope_change_hint  # noqa: E402


def _row(label, cs, op):
    # seconds-ago: smaller = more recent. customer newer than us => owed.
    return {"label": label,
            "last_customer_message_at_seconds": cs,
            "last_operator_reply_at_seconds": op}


def test_confirmed_with_open_thread_shows_hint():
    h = _scope_change_hint(_row("CONFIRMED", 100, 500))  # customer msg newer -> owed
    assert "open thread" in h.lower()
    assert "top-up" in h.lower()
    assert "original" in h.lower()


def test_confirmed_we_replied_last_no_hint():
    # we replied more recently than the customer -> no open thread
    assert _scope_change_hint(_row("CONFIRMED", 500, 100)) == ""


def test_non_confirmed_never_hints():
    assert _scope_change_hint(_row("WARM", 100, 500)) == ""
    assert _scope_change_hint(_row("HOT", 100, 500)) == ""


def test_none_and_missing_safe():
    assert _scope_change_hint({}) == ""
    assert _scope_change_hint({"label": "CONFIRMED"}) == ""  # no timing -> not owed


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} scope-change-hint tests passed")
