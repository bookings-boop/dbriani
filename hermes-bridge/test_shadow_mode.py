#!/usr/bin/env python3
"""Shadow mode (Part 2 of draft-logging + shadow-mode). 'shadow' is a new
conversation mode that SENDS exactly like 'approval' (never auto-sends — it is
not 'autonomous', so the n8n Auto path never fires) but the bridge LOGS, per
draft, what autonomy WOULD have done (would_send if score>=floor, else
would_hold) into draft_log. This locks the mode whitelist + the would-send
verdict (which reuses the already-tested _quality_floor_ok).

Run: python3 hermes-bridge/test_shadow_mode.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from labels import is_valid_mode, VALID_MODES, _quality_floor_ok  # noqa: E402


def test_shadow_is_a_valid_mode():
    assert is_valid_mode("shadow") is True


def test_existing_modes_still_valid():
    for m in ("approval", "autonomous", "paused"):
        assert is_valid_mode(m) is True, m


def test_invalid_modes_rejected():
    for m in ("", "bogus", "SHADOW", None, "shadow ", " shadow", "auto"):
        assert is_valid_mode(m) is False, repr(m)


def test_valid_modes_membership():
    assert "shadow" in VALID_MODES
    assert set(VALID_MODES) == {"approval", "autonomous", "paused", "shadow"}


def test_would_send_verdict_reuses_floor():
    # would_send iff a real int score meets the floor (fail-closed otherwise).
    floor = 8
    assert _quality_floor_ok(9, floor) is True      # would_send
    assert _quality_floor_ok(8, floor) is True       # would_send
    assert _quality_floor_ok(7, floor) is False      # would_hold
    assert _quality_floor_ok(None, floor) is False   # would_hold (fail-closed)
    assert _quality_floor_ok(True, floor) is False   # would_hold (bool != score)


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn(); print("PASS", fn.__name__)
        except AssertionError as e:
            failed += 1; print("FAIL", fn.__name__, "-", e or "assert")
        except Exception as e:
            failed += 1; print("ERROR", fn.__name__, "-", repr(e))
    print(f"{len(fns)-failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
