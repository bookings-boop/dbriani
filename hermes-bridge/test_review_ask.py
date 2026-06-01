#!/usr/bin/env python3
"""Unit tests for the #6-auto-B review-ask helpers (2026-06-01).

After a completed-booking feedback check-in (which sets a Redis marker
fbasked:<cid>), the /review CONFIRMED card flips its draft button to
"⭐ Ask for review"; tapping it drafts a Google-review request with the
operator's review link. MANUAL — the operator decides WHEN to ask (never
auto-sentiment).

Plain-assert style. Run: python3 hermes-bridge/test_review_ask.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from labels import _completed_card_label, _review_ask_directive  # noqa: E402

REVIEW_URL = "https://g.page/r/Cd9gFEpKn3w9EBM/review"


# --- _completed_card_label(trip_passed, feedback_asked) ---------------------
def test_label_upcoming_booking_is_draft_message():
    # not yet completed → logistics/upsell verb, regardless of marker
    assert _completed_card_label(False, False) == "💬 Draft message"
    assert _completed_card_label(False, True) == "💬 Draft message"


def test_label_completed_feedback_pending():
    assert _completed_card_label(True, False) == "💬 Draft feedback check-in"


def test_label_completed_review_ask():
    assert _completed_card_label(True, True) == "⭐ Ask for review"


# --- _review_ask_directive(review_url) --------------------------------------
def test_review_directive_includes_link():
    d = _review_ask_directive(REVIEW_URL)
    assert REVIEW_URL in d
    assert "review" in d.lower()


def test_review_directive_thanks_and_low_pressure():
    d = _review_ask_directive(REVIEW_URL).lower()
    assert "thank" in d  # warm, appreciative


def test_review_directive_no_link_safe():
    for empty in ("", None, "   "):
        d = _review_ask_directive(empty)
        assert "http" not in d        # no broken/empty link
        assert "review" in d.lower()  # still asks for the review


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} review_ask tests passed")
