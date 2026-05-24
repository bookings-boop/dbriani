#!/usr/bin/env python3
"""Unit tests for score_lead() — the pipeline-review priority ranker.

score_lead() is the deterministic ordering key used by /review. It must:
  - hard-floor DISREGARDED (so they never surface)
  - top CONFIRMED (5000) and WAITING_FOR_PAYMENT (4000)
  - rank NEEDS_ATTENTION (1000) > HOT (800) > WARM (500) > NEW (300)
  - cold-decay COLD leads conditionally
  - apply Hermes importance_score as ADDITIVE bonus (≤+90) within tier,
    never crossing into a different label section
  - demote PAUSED_*, label-locked
  - tolerate missing/None fields without crashing

Plain-assert style. Run: python3 hermes-bridge/test_score_lead.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from server import score_lead  # noqa: E402


def _row(label="NEW", **overrides):
    """Minimal row builder. Mirrors read_lead_summary's dict shape with
    only the fields score_lead actually reads — keeps tests focused on
    the ranker, not the SQL parse."""
    base = {
        "label": label,
        "label_locked_active": False,
        "last_booking_intent_at": "",
        "last_rejection_kind": "",
        "last_rejection_at_seconds": None,
        "last_customer_message_at_seconds": None,
        "last_operator_reply_at_seconds": None,
        "last_payment_link_at_seconds": None,
        "last_payment_promised_at_seconds": None,
        "importance_score": None,
    }
    base.update(overrides)
    return base


def test_disregarded_hard_floor():
    # Operator-closed leads must never surface in /review. -100000 makes
    # them fall well below the pause_tail threshold (0).
    assert score_lead(_row("DISREGARDED"), None) == -100000


def test_confirmed_top():
    assert score_lead(_row("CONFIRMED"), None) == 5000


def test_waiting_for_payment_above_hot():
    # WAITING_FOR_PAYMENT (4000) must score above HOT's base (800) so
    # payment-link-sent customers surface at the top of /review.
    s_wait = score_lead(_row("WAITING_FOR_PAYMENT"), None)
    s_hot = score_lead(_row("HOT"), None)
    assert s_wait > s_hot
    assert s_wait == 4000


def test_label_tier_ordering():
    # Pure label ordering with no urgency boosts / damping.
    s = {l: score_lead(_row(l), None) for l in
         ("NEEDS_ATTENTION", "HOT", "WARM", "NEW", "COLD")}
    assert s["NEEDS_ATTENTION"] > s["HOT"] > s["WARM"] > s["NEW"]
    # COLD default (no booking intent, no rejection) ranks below NEW.
    assert s["NEW"] > s["COLD"]


def test_paused_demotion():
    # PAUSED_* falls into the negative tail — should NOT appear in any
    # active section.
    assert score_lead(_row("PAUSED_SPAM"), None) < 0
    assert score_lead(_row("PAUSED_B2B"), None) < 0
    assert score_lead(_row("PAUSED_PERSONAL"), None) < 0


def test_label_lock_demotes():
    # A label-locked HOT lead drops by -500 vs un-locked HOT — prevents
    # auto-promoted leads from over-ranking real engagement.
    unlocked = score_lead(_row("HOT"), None)
    locked = score_lead(_row("HOT", label_locked_active=True), None)
    assert locked == unlocked - 500


def test_importance_score_additive_within_tier():
    # Hermes importance is a +0..90 bonus stacked on the label base.
    # A HOT (800) with importance=100 should reach 890 — still below
    # NEEDS_ATTENTION (1000), preserving section boundaries.
    s_base = score_lead(_row("HOT"), None)
    s_imp = score_lead(_row("HOT", importance_score=100), None)
    s_needs = score_lead(_row("NEEDS_ATTENTION"), None)
    assert s_imp == s_base + 90  # capped at +90
    assert s_imp < s_needs


def test_importance_score_zero_and_none():
    # importance_score=None (never analyzed) and =0 (Hermes said 'close')
    # both add nothing.
    s_base = score_lead(_row("WARM"), None)
    assert score_lead(_row("WARM", importance_score=None), None) == s_base
    assert score_lead(_row("WARM", importance_score=0), None) == s_base


def test_cold_with_booking_intent_outranks_plain_cold():
    s_plain = score_lead(_row("COLD"), None)
    s_intent = score_lead(_row("COLD",
                                last_booking_intent_at="2026-05-20"), None)
    assert s_intent > s_plain


def test_missing_label_defaults_to_new():
    # row with no label key defaults to NEW — must not crash, must score
    # as NEW (300).
    r = _row()
    del r["label"]
    s = score_lead(r, None)
    s_new = score_lead(_row("NEW"), None)
    assert s == s_new


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} score_lead tests passed")
