#!/usr/bin/env python3
"""Lever ③ follow-up — attempt-aware + NO-ORIENTED nudge phrasing (2026-06-16).

Bug: the proactive nudge repeated verbatim — Ahmed (66855544860898@lid) got the
identical "Just checking in..." on Sun 06-14 AND Tue 06-16. Root cause: the phrase
was keyed on the silence WINDOW (soft_checkin <72h / last_shot >72h), and every
nudge SEND resets the silence clock (last_operator_reply_at), so a repeat-nudged
lead stays in soft_checkin and never escalates to the last_shot line.

Fix (flag FOLLOWUP_ATTEMPT_PHRASING_ENABLED, default OFF = legacy byte-for-byte):
key the phrase on ATTEMPT NUMBER (followup_count = nudges the customer has already
received). 1st nudge -> soft check-in; 2nd+ -> a NO-ORIENTED (Chris Voss) question
where a "No" re-opens the chat ("Have you given up on booking a yacht?"), rotated
per lead+attempt so repeat outreach never reuses a line.

Run: python3 hermes-bridge/test_followup_attempt_phrasing.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402

NO = server.GHOST_RECOVERY_NO_ORIENTED
SOFT = server.GHOST_RECOVERY_PHRASES["soft_checkin"]


def test_flag_off_is_legacy_by_window():
    os.environ.pop("FOLLOWUP_ATTEMPT_PHRASING_ENABLED", None)
    t, w = server._ghost_recovery_phrase("soft_checkin", 9, "x@lid")  # fc ignored
    assert t == SOFT and w == "soft_checkin", (t, w)
    t2, w2 = server._ghost_recovery_phrase("last_shot", 0, "x@lid")
    assert t2 == server.GHOST_RECOVERY_PHRASES["last_shot"] and w2 == "last_shot"


def test_flag_on_first_nudge_is_soft():
    os.environ["FOLLOWUP_ATTEMPT_PHRASING_ENABLED"] = "1"
    try:
        t, w = server._ghost_recovery_phrase("soft_checkin", 0, "x@lid")
    finally:
        os.environ.pop("FOLLOWUP_ATTEMPT_PHRASING_ENABLED", None)
    assert t == SOFT and w == "soft_checkin", (t, w)


def test_flag_on_second_nudge_is_no_oriented_even_in_soft_window():
    # Ahmed's exact case: silence_window=soft_checkin but 2nd attempt (fc=1) ->
    # must NOT repeat the soft line; must escalate to a no-oriented question.
    os.environ["FOLLOWUP_ATTEMPT_PHRASING_ENABLED"] = "1"
    try:
        t, w = server._ghost_recovery_phrase("soft_checkin", 1,
                                             "66855544860898@lid")
    finally:
        os.environ.pop("FOLLOWUP_ATTEMPT_PHRASING_ENABLED", None)
    assert t in NO, t
    assert t != SOFT
    assert w == "last_shot"
    assert "{service}" in t


def test_no_oriented_pool_has_voss_line_and_variety():
    assert "Have you given up on booking {service}?" in NO  # operator's exact line
    assert len(NO) >= 3, "operator asked for MORE no-oriented questions"
    for p in NO:
        assert "{service}" in p


def test_variety_across_leads():
    os.environ["FOLLOWUP_ATTEMPT_PHRASING_ENABLED"] = "1"
    try:
        picks = {server._ghost_recovery_phrase("soft_checkin", 1, c)[0]
                 for c in ("111@lid", "222@lid", "333@lid",
                           "444@lid", "555@lid", "666@lid")}
    finally:
        os.environ.pop("FOLLOWUP_ATTEMPT_PHRASING_ENABLED", None)
    assert len(picks) >= 2, "no-oriented questions should vary across leads"


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} attempt-phrasing tests passed")
