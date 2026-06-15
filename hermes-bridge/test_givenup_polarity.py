#!/usr/bin/env python3
"""Polarity-safety proof for the "have you given up?" fix (2026-06-14, Humdan
+971501848003 / 8761951404156@lid).

A bare "yes" answering the negative-polarity ghost-recovery nudge ("Have you
given up on booking ...?") means DECLINE. The SAME "yes" in ANY other context
stays positive. These tests pin the exact boundary so fix #2 (auto-LOST) can
NEVER terminally close an interested customer.

Pure logic — no DB. Run: python3 hermes-bridge/test_givenup_polarity.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402
from server import (  # noqa: E402
    GHOST_RECOVERY_PHRASES,
    _is_givenup_decline,
    _is_givenup_nudge_text,
)


# --- the nudge detector fires ONLY on the 'given up' (last_shot) nudge, NOT
#     the soft check-in (where 'yes' = still considering = POSITIVE) ---------
def test_nudge_text_detector():
    assert _is_givenup_nudge_text("Have you given up on booking a private yacht?")
    assert _is_givenup_nudge_text("have you  given   up on booking a yacht?")
    # ties the detector to the LIVE template — edit the phrase, this fails loud
    assert _is_givenup_nudge_text(
        GHOST_RECOVERY_PHRASES["last_shot"].format(service="a yacht"))
    # the soft check-in is NOT polarity-inverting — 'yes' there is positive
    assert not _is_givenup_nudge_text(
        GHOST_RECOVERY_PHRASES["soft_checkin"].format(service="a yacht"))
    assert not _is_givenup_nudge_text("glad you're still interested! Élan 44 or bigger?")
    assert not _is_givenup_nudge_text("")


# --- WITH the given-up nudge as our last outbound: a bare yes-family = decline
GIVENUP_WITH_NUDGE = [
    "yes", "Yes", "yes.", "yeah", "yep", "yup", "ya", "yes 🙏",
    "sadly yes", "unfortunately yes", "yes sadly", "yes i have", "i have",
]


def test_bare_yes_after_givenup_nudge_is_decline():
    for m in GIVENUP_WITH_NUDGE:
        assert _is_givenup_decline(m, True), repr(m)


# --- explicit 'given up' / 'gave up' is a decline in ANY context ------------
GIVENUP_EXPLICIT = [
    "I've given up", "i have given up", "I said yes I've given up",
    "we gave up", "given up sorry", "i give up",
]


def test_explicit_given_up_is_decline_any_context():
    for m in GIVENUP_EXPLICIT:
        assert _is_givenup_decline(m, False), repr(m)   # no nudge needed
        assert _is_givenup_decline(m, True), repr(m)


# --- SAFETY (a): a bare yes-family WITHOUT the given-up nudge stays positive -
NOT_DECLINE_NO_NUDGE = [
    "yes", "yeah", "yep", "ok", "sure", "sounds good", "let's do it",
    "yes please", "👍", "i have",
]


def test_bare_yes_without_nudge_is_not_decline():
    for m in NOT_DECLINE_NO_NUDGE:
        assert not _is_givenup_decline(m, False), repr(m)


# --- SAFETY (b): even WITH the given-up nudge, these must NOT be a decline ---
NOT_DECLINE_WITH_NUDGE = [
    "no", "no still keen", "not yet", "no i still want to book",
    "yes please let's book it", "yes i'm still keen",
    "yes let's book the Élan 44", "ok", "sure", "book it", "let's do it",
    "go ahead", "deal", "yes but when can you do the 20th?",
    "not really, can you send options?", "yes can you call me?", "👍 let's book",
    "yes what's the price for the 20th?",
]


def test_positive_or_ambiguous_after_nudge_is_not_decline():
    for m in NOT_DECLINE_WITH_NUDGE:
        assert not _is_givenup_decline(m, True), repr(m)


# --- invariant fix #2 relies on: a LOST target survives confidence dampening
def test_lost_never_demoted_by_dampening():
    assert server._TIER_BELOW.get("LOST", "LOST") == "LOST"


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} givenup_polarity tests passed")
