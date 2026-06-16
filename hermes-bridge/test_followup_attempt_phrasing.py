#!/usr/bin/env python3
"""Lever ③ follow-up — attempt-aware variant pools + per-variant tagging (2026-06-16).

Touch 1 (24-72h) = a SOFT follow-up (operator's templates); touch 2 (2nd attempt) =
a NO-ORIENTED (Voss) question. Both pools are rotated PER LEAD (vary-by-lead) so an
A/B spread accrues, and each pick returns a `variant_tag` stamped onto
draft_log.trigger_kind so conversion-per-variant becomes measurable later
("see what works best" instead of a guess). Flag FOLLOWUP_ATTEMPT_PHRASING_ENABLED
(default OFF = legacy: phrase by silence-window).

Run: python3 hermes-bridge/test_followup_attempt_phrasing.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402

SOFT = server.GHOST_RECOVERY_SOFT          # touch-1 pool (operator soft templates)
NO = server.GHOST_RECOVERY_NO_ORIENTED     # touch-2 pool (no-oriented)


def test_flag_off_is_legacy_by_window():
    os.environ.pop("FOLLOWUP_ATTEMPT_PHRASING_ENABLED", None)
    t, w, tag = server._ghost_recovery_phrase("soft_checkin", 9, "x@lid")
    assert t == server.GHOST_RECOVERY_PHRASES["soft_checkin"] and w == "soft_checkin"
    t2, w2, _tag2 = server._ghost_recovery_phrase("last_shot", 0, "x@lid")
    assert t2 == server.GHOST_RECOVERY_PHRASES["last_shot"] and w2 == "last_shot"


def test_flag_on_first_nudge_is_a_soft_variant():
    os.environ["FOLLOWUP_ATTEMPT_PHRASING_ENABLED"] = "1"
    try:
        t, w, tag = server._ghost_recovery_phrase("soft_checkin", 0, "x@lid")
    finally:
        os.environ.pop("FOLLOWUP_ATTEMPT_PHRASING_ENABLED", None)
    assert t in SOFT, t
    assert w == "soft_checkin"
    assert tag.startswith("reengage:soft_"), tag


def test_flag_on_second_nudge_is_no_oriented():
    os.environ["FOLLOWUP_ATTEMPT_PHRASING_ENABLED"] = "1"
    try:
        t, w, tag = server._ghost_recovery_phrase("soft_checkin", 1,
                                                  "66855544860898@lid")
    finally:
        os.environ.pop("FOLLOWUP_ATTEMPT_PHRASING_ENABLED", None)
    assert t in NO and t not in SOFT
    assert w == "last_shot"
    assert tag.startswith("reengage:noorient_"), tag


def test_soft_pool_is_operator_templates_generic():
    assert len(SOFT) >= 2
    assert any("suitable options" in s for s in SOFT)   # operator's wording
    assert any("How are you" in s for s in SOFT)
    for s in SOFT:                                       # generic — no {service}
        assert "{service}" not in s


def test_no_oriented_pool_has_voss_line():
    assert any("given up on booking" in p for p in NO)
    assert len(NO) >= 3
    for p in NO:
        assert "{service}" in p


def test_variant_tags_spread_for_ab_testing():
    # use REAL production @lid cids (long digit strings) — a weak hash clusters
    # them all onto one variant, which would make the A/B data meaningless.
    cids = ("259691439468674@lid", "66855544860898@lid", "73770995818585@lid",
            "243451799019653@lid", "247150420213939@lid", "160133577494562@lid",
            "278305425088707@lid", "275767518830614@lid", "56586697465991@lid")
    os.environ["FOLLOWUP_ATTEMPT_PHRASING_ENABLED"] = "1"
    try:
        soft_tags = [server._ghost_recovery_phrase("soft_checkin", 0, c)[2]
                     for c in cids]
        no_tags = [server._ghost_recovery_phrase("soft_checkin", 1, c)[2]
                   for c in cids]
    finally:
        os.environ.pop("FOLLOWUP_ATTEMPT_PHRASING_ENABLED", None)
    # both SOFT variants must appear across 9 real leads (not all on soft_a)
    assert len(set(soft_tags)) == len(SOFT), (set(soft_tags), "expected all soft variants")
    # no-oriented should hit at least half the pool across 9 leads
    assert len(set(no_tags)) >= max(2, len(NO) // 2), set(no_tags)


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} attempt-phrasing tests passed")
