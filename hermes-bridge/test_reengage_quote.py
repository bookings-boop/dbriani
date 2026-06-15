#!/usr/bin/env python3
"""Unit tests for the proactive follow-up CARD builder.

The targeting, drafting (Voss ghost-recovery phrasing + full WAHA context),
exclusion guard, and cooldown now all live in the bridge's NATIVE engine
(server.scan_followup_eligibility + routes.handle_draft_followup). The only
piece that needs pure unit testing here is the Telegram card the proactive
sweep self-posts: text + the one-tap Send/Edit/Regen/Skip inline keyboard,
whose callback_data drives the existing n8n -> /queue claim-send -> WAHA path.

Plain-assert style. Run: python3 hermes-bridge/test_reengage_quote.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from reengage_quote import build_followup_card  # noqa: E402


def _card(**kw):
    base = dict(header="🔔 PROACTIVE FOLLOW-UP — hot", name="Sam",
                customer_id="971509767187@c.us", label="HOT",
                silence_hours=72.0, badge="", draft_text="have you given up?",
                draft_id="1717800000000_ab12z")
    base.update(kw)
    return build_followup_card(**base)


def test_returns_text_and_reply_markup():
    c = _card()
    assert isinstance(c, dict)
    assert "text" in c and "reply_markup" in c


def test_send_button_carries_send_callback_with_draft_id():
    c = _card(draft_id="DID123")
    kb = c["reply_markup"]["inline_keyboard"]
    flat = [b for row in kb for b in row]
    send_btn = [b for b in flat if b["text"] == "✅ Send"]
    assert len(send_btn) == 1
    assert send_btn[0]["callback_data"] == "send:DID123"


def test_all_four_actions_present_with_draft_id():
    c = _card(draft_id="DID123")
    flat = [b for row in c["reply_markup"]["inline_keyboard"] for b in row]
    cbs = {b["callback_data"] for b in flat}
    assert cbs == {"send:DID123", "edit:DID123", "regen:DID123", "skip:DID123"}


def test_text_includes_draft_and_recipient_and_label():
    c = _card(name="Sam", label="HOT", draft_text="have you given up on booking?")
    assert "have you given up on booking?" in c["text"]
    assert "Sam" in c["text"]
    assert "HOT" in c["text"]


def test_missing_name_falls_back_to_masked_id():
    c = _card(name="", customer_id="971509767187@c.us")
    assert "None" not in c["text"]
    # phone digits shown so operator can still identify the recipient
    assert "7187" in c["text"]


def test_resolved_phone_shown_for_verification():
    # high-ticket one-tap send: the real phone must be visible to verify
    c = _card(name="Sam", customer_id="13578@lid", phone="+971501234567")
    assert "+971501234567" in c["text"]


def test_phone_falls_back_to_cid_digits_when_unresolved():
    c = _card(name="Sam", customer_id="971509767187@c.us", phone=None)
    assert "971509767187" in c["text"]


def test_none_silence_hours_does_not_crash_or_leak():
    c = _card(silence_hours=None)
    assert "None" not in c["text"]


def test_badge_included_when_present_omitted_when_empty():
    with_badge = _card(badge="⭐ 9/10")
    assert "⭐ 9/10" in with_badge["text"]
    without = _card(badge="")
    assert "⭐" not in without["text"]


def test_text_warns_to_confirm_recipient():
    # high-ticket: operator must verify the target before a one-tap send
    assert "confirm" in _card()["text"].lower()


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} followup-card tests passed")
