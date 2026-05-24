#!/usr/bin/env python3
"""Unit tests for _why_line() — the per-card guidance string in /review.

_why_line renders ONE descriptive line per card describing what the
operator should do. It must:
  - prefer Hermes' suggested_action for CONFIRMED + WAITING_FOR_PAYMENT
    (these are where the generic guidance is most often wrong — e.g.
    'share boarding details' when boarding's already been sent)
  - fall back to deterministic heuristics for HOT/WARM/NEW/COLD when
    no Hermes suggestion is present
  - surface 'payment link sent Xh ago, no commitment' when stale
  - surface 'almost-bought' / 'lost on price' / 'lost on timing' for
    COLD customers with relevant trigger rows

Plain-assert style. Run: python3 hermes-bridge/test_why_line.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from server import _why_line  # noqa: E402


def _row(**kw):
    base = {
        "last_payment_link_at_seconds": None,
        "last_payment_promised_at_seconds": None,
        "last_booking_intent_at": "",
        "last_rejection_kind": "",
        "suggested_action": "",
        "importance_reasoning": "",
    }
    base.update(kw)
    return base


# ---- CONFIRMED + WAITING_FOR_PAYMENT: prefer suggested_action ----

def test_confirmed_uses_suggested_action_when_present():
    # Today's regression: Mohammed CONFIRMED card was showing generic
    # 'share boarding details or upsell' even though boarding was
    # already shared. Hermes' suggested_action MUST win for CONFIRMED.
    row = _row(suggested_action="Send Monday reminder with marina pin")
    result = _why_line(row, "CONFIRMED")
    assert result == "Send Monday reminder with marina pin"


def test_confirmed_falls_back_to_generic_when_no_suggestion():
    # Before pipeline-analyze runs on a fresh-CONFIRMED, suggested_action
    # is empty. Generic guidance shown until Hermes scores them.
    row = _row(suggested_action="")
    result = _why_line(row, "CONFIRMED")
    assert "boarding" in result.lower() or "upsell" in result.lower()


def test_waiting_for_payment_uses_suggested_action():
    row = _row(suggested_action="Nudge — payment 26h overdue, "
                                "offer Apple Pay")
    result = _why_line(row, "WAITING_FOR_PAYMENT")
    assert "Apple Pay" in result


def test_waiting_for_payment_fallback():
    row = _row(suggested_action="")
    result = _why_line(row, "WAITING_FOR_PAYMENT")
    assert "payment" in result.lower()


# ---- Other labels: deterministic heuristics ----

def test_hot_default_message():
    row = _row()
    result = _why_line(row, "HOT")
    assert result  # non-empty
    assert "hot" in result.lower() or "push" in result.lower() or \
           "booking" in result.lower()


def test_warm_default():
    row = _row()
    result = _why_line(row, "WARM")
    assert result
    assert "nudge" in result.lower() or "value" in result.lower() or \
           "engaged" in result.lower()


def test_cold_default():
    row = _row()
    result = _why_line(row, "COLD")
    assert "re-engage" in result.lower() or "soft" in result.lower()


def test_cold_almost_bought():
    # Cold customer who had hit booking_intent before going quiet
    # — special-cased.
    row = _row(last_booking_intent_at="2026-05-20")
    result = _why_line(row, "COLD")
    assert "almost-bought" in result or "booking intent" in result.lower()


def test_cold_lost_on_price():
    row = _row(last_rejection_kind="rejected_price")
    result = _why_line(row, "COLD")
    assert "price" in result.lower()


def test_cold_lost_on_timing():
    row = _row(last_rejection_kind="rejected_timing")
    result = _why_line(row, "COLD")
    assert "timing" in result.lower() or "date" in result.lower()


def test_needs_attention_default():
    row = _row()
    result = _why_line(row, "NEEDS_ATTENTION")
    assert "owe" in result.lower() or "reply" in result.lower()


def test_payment_link_stale_no_commit():
    # Operator-visible signal: payment link sent >24h ago but no
    # payment-promised trigger fired. Surface to /review.
    row = _row(last_payment_link_at_seconds=30 * 3600,  # 30h ago
               last_payment_promised_at_seconds=None)
    result = _why_line(row, "WARM")
    assert "payment link sent" in result.lower()
    assert "30h" in result


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} why_line tests passed")
