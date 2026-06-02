#!/usr/bin/env python3
"""Regression test: render_review must Markdown-escape dynamic values (name,
yacht, dates, reasoning, suggestion) so a single '_' or '*' in customer text
cannot break Telegram Markdown parsing (HTTP 400 -> the WHOLE /review fails).

Bug 2026-06-02 (QC C1): render_review interpolated raw name/yacht/why/reasoning
into parse_mode=Markdown without _md_escape — one underscore in a name silently
400'd the entire pipeline view.

Run: python3 hermes-bridge/test_review_md_escape.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from review import render_review  # noqa: E402


def _row(**kw):
    base = {
        "customer_id": "111222333@c.us",
        "name": "A_J Marine",                       # underscore in name
        "label": "HOT",
        "message_count": 3,
        "yachts": "Bliss 55",
        "dates": "Jun 7, 5 PM",
        "importance_score": 80,
        "importance_reasoning": "keen *VIP* client, wants Bliss_55",
        "suggested_action": "send quote for Bliss_55",
        "last_customer_message_at_seconds": 3600,
        "last_operator_reply_at_seconds": None,
        "importance_analyzed_at_seconds": 7200,
    }
    base.update(kw)
    return base


def _text(out):
    if out.get("per_lead_messages"):
        return "\n".join(m["text"] for m in out["per_lead_messages"])
    return out.get("telegram_text", "")


def _unescaped_count(s, ch):
    """Count occurrences of ch NOT preceded by a backslash."""
    n = 0
    for i, c in enumerate(s):
        if c == ch and (i == 0 or s[i - 1] != "\\"):
            n += 1
    return n


def test_name_underscore_is_escaped():
    txt = _text(render_review([(80, _row())], {}, "ondemand"))
    assert "A\\_J Marine" in txt, "name underscore not escaped"


def test_markdown_markers_balanced():
    # the 400 cause: an ODD number of unescaped _ or * in the message
    txt = _text(render_review([(80, _row())], {}, "ondemand"))
    assert _unescaped_count(txt, "_") % 2 == 0, "unbalanced underscores"
    assert _unescaped_count(txt, "*") % 2 == 0, "unbalanced asterisks"


def test_reasoning_asterisks_escaped():
    txt = _text(render_review([(80, _row(suggested_action=""))], {}, "ondemand"))
    # the *VIP* in reasoning must not introduce raw bold markers
    assert "\\*VIP\\*" in txt or "*VIP*" not in txt.replace("\\*", "")


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} review-md-escape tests passed")
