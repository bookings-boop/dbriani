#!/usr/bin/env python3
"""Unit tests for _is_dormancy_eligible() — #5-auto graceful-close gate.

A passed-date lead (signal date_passed) is auto-marked DORMANT (DISREGARDED,
reversible via /label) only AFTER >= N re-engage attempts AND >= D days of
continued customer silence — never before a re-engage attempt, never for a
won/closed lead. Operator chose N=2 attempts, D=7 days (2026-06-01).

Plain-assert style. Run: python3 hermes-bridge/test_dormancy.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from labels import _is_dormancy_eligible  # noqa: E402


def test_eligible_at_threshold():
    assert _is_dormancy_eligible("COLD", "date_passed", 2, 7) is True


def test_eligible_above_threshold():
    assert _is_dormancy_eligible("COLD", "date_passed", 4, 30) is True


def test_not_enough_attempts():
    # 1 re-engage attempt is not enough — never close after a single nudge
    assert _is_dormancy_eligible("COLD", "date_passed", 1, 30) is False


def test_zero_attempts_never():
    # the "never silent-disregard" rule: 0 attempts → never auto-close
    assert _is_dormancy_eligible("COLD", "date_passed", 0, 365) is False


def test_not_silent_long_enough():
    assert _is_dormancy_eligible("COLD", "date_passed", 5, 6) is False


def test_only_date_passed_signal():
    # cold_decay / other signals are out of scope for #5-auto
    assert _is_dormancy_eligible("COLD", "cold_decay", 5, 30) is False
    assert _is_dormancy_eligible("COLD", "", 5, 30) is False
    assert _is_dormancy_eligible("COLD", None, 5, 30) is False


def test_excludes_terminal_and_won_labels():
    for lab in ("DISREGARDED", "CONFIRMED", "PAUSED_SPAM",
                "PAUSED_B2B", "PAUSED_PERSONAL"):
        assert _is_dormancy_eligible(lab, "date_passed", 5, 30) is False


def test_active_labels_eligible():
    # date_passed leads usually carry COLD, but guard NEW/WARM too
    for lab in ("COLD", "NEW", "WARM"):
        assert _is_dormancy_eligible(lab, "date_passed", 2, 7) is True


def test_none_numeric_inputs_safe():
    assert _is_dormancy_eligible("COLD", "date_passed", None, 30) is False
    assert _is_dormancy_eligible("COLD", "date_passed", 5, None) is False


def test_custom_thresholds():
    # thresholds are tunable (env-overridable in the caller)
    assert _is_dormancy_eligible("COLD", "date_passed", 3, 14,
                                 min_attempts=3, min_silent_days=14) is True
    assert _is_dormancy_eligible("COLD", "date_passed", 2, 14,
                                 min_attempts=3, min_silent_days=14) is False


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} dormancy tests passed")
