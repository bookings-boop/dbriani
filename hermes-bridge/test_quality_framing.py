#!/usr/bin/env python3
"""Scorecard bug (2026-06-02): the quality scorer (build_quality_query) frames
every draft as a 'reply' to the customer's newest message and scores it 'against
the conversation'. An OUTBOUND first contact has NO incoming message and NO
history, so the scorer grades an opener as a reply to nothing and wrongly
penalizes it -> the scorecard is useless for new outbound leads.

This locks the framing decision: an outbound first contact (no incoming + no
history) is scored as an OUTREACH OPENER and the empty 'newest message' section
is omitted; everything else keeps the reply rubric.

Run: python3 hermes-bridge/test_quality_framing.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from server import _quality_task_framing  # noqa: E402


def test_first_contact_uses_outreach_rubric():
    task, show_msg = _quality_task_framing("", "")
    low = task.lower()
    assert "first" in low and ("outreach" in low or "opener" in low
                               or "reaching out" in low)
    assert "reply" not in low or "first-contact" in low  # not a reply rubric
    assert show_msg is False   # omit the empty 'newest message' section


def test_normal_reply_keeps_reply_rubric():
    task, show_msg = _quality_task_framing("hi do you have yachts available?", "")
    assert "reply" in task.lower()
    assert show_msg is True


def test_history_means_not_first_contact():
    # If there's prior history, it's an ongoing conversation -> reply rubric,
    # even if this turn has no fresh incoming text.
    task, show_msg = _quality_task_framing("", "customer: hi\nMaria: hello!")
    assert show_msg is True
    assert "reply" in task.lower()


def test_whitespace_is_treated_as_empty():
    _, show_msg = _quality_task_framing("   ", "  \n ")
    assert show_msg is False


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} quality-framing tests passed")
