#!/usr/bin/env python3
"""_scope_change_hint — ⚠️ open-thread reminder on an UPCOMING confirmed booking
(2026-06-07 customer D extended to a 4th hour but the card showed the original
3hrs/paid). Bug 4 (2026-06-17): the helper was DEAD CODE and gated on _owes_reply
only; it is now wired into the confirmed card and gated on
CONFIRMED_OPEN_THREADS_ENABLED, firing on _owes_reply OR recent two-way activity
within CONFIRMED_OPEN_THREAD_WINDOW_DAYS. Pure, non-financial, alert-only.

Run: python3 hermes-bridge/test_scope_change_hint.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import review  # noqa: E402
from review import _scope_change_hint  # noqa: E402


def _row(label, cs, op):
    # seconds-ago: smaller = more recent. customer newer than us => owed.
    # Future booking date so _booking_likely_passed is False (upcoming).
    return {"label": label, "dates": "Dec 31 2099",
            "last_customer_message_at_seconds": cs,
            "last_operator_reply_at_seconds": op}


class _flagon:
    """Enable the open-thread reminder for a block, then restore."""

    def __enter__(self):
        self._p = (review.CONFIRMED_OPEN_THREADS_ENABLED,
                   review.CONFIRMED_OPEN_THREAD_WINDOW_DAYS)
        review.CONFIRMED_OPEN_THREADS_ENABLED = True
        review.CONFIRMED_OPEN_THREAD_WINDOW_DAYS = 10
        return self

    def __exit__(self, *a):
        (review.CONFIRMED_OPEN_THREADS_ENABLED,
         review.CONFIRMED_OPEN_THREAD_WINDOW_DAYS) = self._p


def test_confirmed_owed_shows_hint_when_flag_on():
    with _flagon():
        h = _scope_change_hint(_row("CONFIRMED", 100, 500))  # customer newer -> owed
        assert "open thread" in h.lower()
        assert "top-up" in h.lower()
        assert "original" in h.lower()


def test_recent_activity_fires_even_when_we_replied_last():
    # Antonio: we replied last (op newer) but the customer was active within the
    # window -> still an open thread.
    with _flagon():
        h = _scope_change_hint(_row("CONFIRMED", 39 * 3600, 3600))
        assert "open thread" in h.lower()


def test_settled_booking_no_hint():
    # old activity + we replied since -> neither owed nor recent.
    with _flagon():
        assert _scope_change_hint(_row("CONFIRMED", 30 * 86400, 20 * 86400)) == ""


def test_dormant_when_flag_off():
    assert _scope_change_hint(_row("CONFIRMED", 100, 500)) == ""


def test_non_confirmed_never_hints():
    with _flagon():
        assert _scope_change_hint(_row("WARM", 100, 500)) == ""
        assert _scope_change_hint(_row("HOT", 100, 500)) == ""


def test_none_and_missing_safe():
    with _flagon():
        assert _scope_change_hint({}) == ""
        assert _scope_change_hint({"label": "CONFIRMED"}) == ""  # no timing


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} scope-change-hint tests passed")
