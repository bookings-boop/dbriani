#!/usr/bin/env python3
"""Bug 4 (2026-06-17): an open thread on a confirmed booking was missed. Antonio
(Bliss 55, paid) accepted a 4th-hour extension whose payment link we hadn't sent,
and was undecided on catering — but the /review card showed only the generic
"confirm logistics or upsell" line. Root: _scope_change_hint (the ⚠️ open-thread
reminder) was DEAD CODE (never called) AND gated on _owes_reply only, which is
False when WE sent the last message (as with Antonio).

Fix (flag-gated CONFIRMED_OPEN_THREADS_ENABLED, render-only, ALERT-only — never
auto-mints a link): wire _scope_change_hint into the confirmed card and broaden
its trigger to recent two-way activity (within CONFIRMED_OPEN_THREAD_WINDOW_DAYS),
not just _owes_reply. Upcoming bookings only. Dormant → byte-identical when off.

Run: python3 hermes-bridge/test_confirmed_open_threads.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import review  # noqa: E402
from review import render_review  # noqa: E402
from test_render_review import _row, _card_for, _dated  # noqa: E402

_HINT = "open thread on a confirmed booking"


class _rflags:
    def __init__(self, **kw):
        self.kw = kw
        self._prev = {}

    def __enter__(self):
        for k, v in self.kw.items():
            self._prev[k] = getattr(review, k, None)
            setattr(review, k, v)
        return self

    def __exit__(self, *a):
        for k, v in self._prev.items():
            setattr(review, k, v)


# ---- unit: _scope_change_hint trigger --------------------------------------
def test_fires_on_recent_activity_even_when_we_replied_last():
    # The Antonio case: we sent the last message (not _owes_reply) but the
    # customer was active 39h ago on an upcoming booking.
    with _rflags(CONFIRMED_OPEN_THREADS_ENABLED=True,
                 CONFIRMED_OPEN_THREAD_WINDOW_DAYS=10):
        row = {"label": "CONFIRMED", "dates": _dated(10),
               "last_customer_message_at_seconds": 39 * 3600,
               "last_operator_reply_at_seconds": 3600}
        assert _HINT in review._scope_change_hint(row)


def test_fires_on_owes_reply():
    with _rflags(CONFIRMED_OPEN_THREADS_ENABLED=True,
                 CONFIRMED_OPEN_THREAD_WINDOW_DAYS=10):
        row = {"label": "CONFIRMED", "dates": _dated(10),
               "last_customer_message_at_seconds": 600,       # customer last
               "last_operator_reply_at_seconds": 7200}
        assert _HINT in review._scope_change_hint(row)


def test_silent_when_activity_old_and_not_owed():
    with _rflags(CONFIRMED_OPEN_THREADS_ENABLED=True,
                 CONFIRMED_OPEN_THREAD_WINDOW_DAYS=10):
        row = {"label": "CONFIRMED", "dates": _dated(10),
               "last_customer_message_at_seconds": 30 * 86400,  # 30d ago
               "last_operator_reply_at_seconds": 20 * 86400}    # we replied since
        assert review._scope_change_hint(row) == ""


def test_silent_when_flag_off():
    with _rflags(CONFIRMED_OPEN_THREADS_ENABLED=False):
        row = {"label": "CONFIRMED", "dates": _dated(10),
               "last_customer_message_at_seconds": 39 * 3600,
               "last_operator_reply_at_seconds": 3600}
        assert review._scope_change_hint(row) == ""


def test_silent_when_not_confirmed():
    with _rflags(CONFIRMED_OPEN_THREADS_ENABLED=True):
        row = {"label": "WARM", "dates": _dated(10),
               "last_customer_message_at_seconds": 600}
        assert review._scope_change_hint(row) == ""


def test_silent_when_booking_passed():
    with _rflags(CONFIRMED_OPEN_THREADS_ENABLED=True):
        row = {"label": "CONFIRMED", "dates": _dated(-3),
               "last_customer_message_at_seconds": 600,
               "last_operator_reply_at_seconds": 7200}
        assert review._scope_change_hint(row) == ""


# ---- integration: wired into the confirmed card ----------------------------
def _antonio():
    # upcoming confirmed, we replied last (not owed), customer active 39h ago
    return _row("ant@lid", label="CONFIRMED", name="Antonio",
                booked_yacht="Bliss 55", dates=_dated(10), party_size="12 guests",
                paid_amount="AED 3534.3", importance_score=62,
                last_customer_message_at_seconds=39 * 3600,
                last_operator_reply_at_seconds=3600)


def test_confirmed_card_surfaces_open_thread_when_flag_on():
    with _rflags(CONFIRMED_OPEN_THREADS_ENABLED=True,
                 CONFIRMED_OPEN_THREAD_WINDOW_DAYS=10):
        res = render_review([(5000, _antonio())], {}, "on-demand")
        card = _card_for(res, "ant@lid")
        assert card, "confirmed lead must be visible"
        assert "booked & paid" in card["text"], card["text"]
        assert _HINT in card["text"], "open-thread reminder must be wired into the card"


def test_confirmed_card_unchanged_when_flag_off():
    with _rflags(CONFIRMED_OPEN_THREADS_ENABLED=False):
        res = render_review([(5000, _antonio())], {}, "on-demand")
        card = _card_for(res, "ant@lid")
        assert card and "booked & paid" in card["text"]
        assert _HINT not in card["text"], "must be byte-identical (dormant) when off"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn(); print("PASS", fn.__name__)
        except AssertionError as e:
            failed += 1; print("FAIL", fn.__name__, "-", e or "assert")
        except Exception as e:  # noqa: BLE001
            failed += 1; print("ERROR", fn.__name__, "-", repr(e))
    print(f"{len(fns)-failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
