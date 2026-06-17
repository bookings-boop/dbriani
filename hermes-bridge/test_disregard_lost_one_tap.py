#!/usr/bin/env python3
"""Bug 1 (2026-06-17): pressing 🛑 Disregard on a LOST lead was a LOST→LOST
no-op. The default path re-ran Hermes, which re-derived LOST from the
"customer never replied" reasoning (analysis_guard.reclassify_close_label →
labels._LOST_GHOST_RE → LOST, never DISREGARDED), so the 💔 LOST card returned
on every /review and the operator pressed 🛑 Disregard 9× on one lead (Digital
Fivver), each press burning a live Hermes call. When
DISREGARD_TERMINAL_LOST_ONE_TAP_ENABLED is on, a LOST lead closes in ONE tap →
force path → LOST→DISREGARDED (hidden), idempotent via the resurrection guard.
Flag OFF = byte-identical old behavior.

Reuses the mocked harness from test_disregard_second_press (server.* +
routes._psql patched; no DB / no Hermes).

Run: python3 hermes-bridge/test_disregard_lost_one_tap.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_disregard_second_press import _Harness, _call  # noqa: E402

_FLAG = "DISREGARD_TERMINAL_LOST_ONE_TAP_ENABLED"


class _flag:
    """Set the one-tap flag for the duration of a block, then restore — so the
    env var never leaks into sibling tests when the suite runs in one process."""

    def __init__(self, on):
        self.on = on
        self._prev = None

    def __enter__(self):
        self._prev = os.environ.get(_FLAG)
        os.environ[_FLAG] = "1" if self.on else "0"
        return self

    def __exit__(self, *a):
        if self._prev is None:
            os.environ.pop(_FLAG, None)
        else:
            os.environ[_FLAG] = self._prev


# --- flag ON: LOST closes in one tap, Hermes NOT consulted (the fix) ---------
def test_lost_one_tap_hides_when_flag_on():
    with _flag(True):
        h = _Harness(label="LOST", prior_veto=False)
        out = _call(h, {"customer_id": "240991168635118@lid"})
    assert h.hermes_called is False, "one-tap LOST close must SKIP Hermes"
    assert out.get("label_after") == "DISREGARDED"
    assert out.get("verdict") == "close"
    assert len(h.transitions) == 1
    frm, to, signal, created_by = h.transitions[0]
    assert (frm, to) == ("LOST", "DISREGARDED")
    assert created_by == "operator:disregard_force"


# --- flag OFF: LOST still defers to Hermes (byte-identical old behavior) ------
def test_lost_consults_hermes_when_flag_off():
    with _flag(False):
        h = _Harness(label="LOST", prior_veto=False, hermes_verdict="keep_open")
        out = _call(h, {"customer_id": "x@lid"})
    assert h.hermes_called is True, "flag OFF must preserve the Hermes path"
    assert not h.transitions, "keep_open verdict must not change the label"


# --- flag ON: a re-press on the now-DISREGARDED lead is idempotent -----------
def test_repress_on_disregarded_is_idempotent_when_flag_on():
    with _flag(True):
        h = _Harness(label="DISREGARDED", prior_veto=False)
        out = _call(h, {"customer_id": "x@lid"})
    assert h.hermes_called is False
    assert out.get("label_after") == "DISREGARDED"
    assert not h.transitions, "resurrection guard must not re-write the label"


# --- flag ON: active leads (HOT/WARM/NEW) still get Hermes (scope guard) -----
def test_active_lead_still_consults_hermes_when_flag_on():
    with _flag(True):
        h = _Harness(label="HOT", prior_veto=False, hermes_verdict="keep_open")
        out = _call(h, {"customer_id": "x@lid"})
    assert h.hermes_called is True, "flag must only affect LOST, not active leads"
    assert not h.transitions


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} disregard_lost_one_tap tests passed")
