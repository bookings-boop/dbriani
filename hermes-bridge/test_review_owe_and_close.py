#!/usr/bin/env python3
"""2026-06-02: (1) owed-reply leads must never be hidden in a capped tier's
overflow (a HOT 'needs your reply' fishing lead with no yacht ranked 19/24 and
vanished); they float to the top of their tier. (2) CONFIRMED cards get a
'Close chat' button (operator request).

Run: python3 hermes-bridge/test_review_owe_and_close.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from review import _owes_reply, render_review  # noqa: E402


def test_owes_reply_logic():
    assert _owes_reply({"last_customer_message_at_seconds": 100}) is True
    assert _owes_reply({"last_customer_message_at_seconds": 100,
                        "last_operator_reply_at_seconds": 50}) is False
    assert _owes_reply({"last_customer_message_at_seconds": 100,
                        "last_operator_reply_at_seconds": 200}) is True
    assert _owes_reply({}) is False


def _hot(cid, name, yachts, owe):
    r = {"customer_id": cid, "name": name, "label": "HOT", "yachts": yachts,
         "message_count": 3, "last_customer_message_at_seconds": 100}
    if not owe:
        r["last_operator_reply_at_seconds"] = 50  # we replied more recently
    return r


def test_owed_lowrate_floats_above_notowed_highrate():
    owed = _hot("owed@lid", "Owed", "", True)              # rate 0, owed
    rich = _hot("rich@lid", "Rich", "Royalty 136", False)  # rate 15000, not owed
    out = render_review([(800, rich), (800, owed)], {"total": 2})
    order = [p["customer_id"] for p in out["per_lead_messages"]]
    assert order.index("owed@lid") < order.index("rich@lid"), order


def test_confirmed_card_has_close_chat_button():
    r = {"customer_id": "saif@lid", "name": "Saif", "label": "CONFIRMED",
         "yachts": "Satoshi 70", "dates": "May 24", "message_count": 5,
         "importance_score": 0}
    out = render_review([(5000, r)], {"total": 1})
    kb = [p for p in out["per_lead_messages"]
          if p["customer_id"] == "saif@lid"][0]["inline_keyboard"]
    txt = [b["text"] for row in kb for b in row]
    cbs = [b["callback_data"] for row in kb for b in row]
    assert any("Close chat" in t for t in txt), txt
    assert "disregard:saif@lid" in cbs, cbs


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} review owe/close tests passed")
