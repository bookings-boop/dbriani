#!/usr/bin/env python3
"""Kill-switch for the 2026-06-14 'have you given up?' decline handling.
GIVENUP_DECLINE_ENABLED (default ON) gates BOTH fix #1 (draft directive) and
fix #2 (LOST classification). Flip to 0 + restart to instantly revert the
draft/label behavior to pre-fix, without a redeploy.

Run: python3 hermes-bridge/test_givenup_killswitch.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402
from server import compute_label  # noqa: E402

server._has_recent_payment_intent = lambda *a, **k: False
server._has_recent_payment_link_sent = lambda *a, **k: False
server._recent_outbound_quote = lambda *a, **k: False
server._last_outbound_is_givenup_nudge = lambda *a, **k: True
server._latest_inbound_body = lambda *a, **k: "yes"


def humdan(**ov):
    d = {"customer_id": "8761951404156@lid", "message_count": 62,
         "label": "WARM", "yachts": "Élan 44, Bliss 55, Zenith 64", "dates": ""}
    d.update(ov)
    return d


def test_default_on_classifies_and_directs():
    os.environ.pop("GIVENUP_DECLINE_ENABLED", None)            # default = ON
    assert compute_label("yes", humdan())[0] == "LOST"
    assert server._ghost_recovery_decline_line("8761951404156@lid") != ""


def test_killswitch_off_reverts_both_fixes():
    os.environ["GIVENUP_DECLINE_ENABLED"] = "0"
    try:
        assert compute_label("yes", humdan())[0] != "LOST"    # fix #2 disabled
        assert server._ghost_recovery_decline_line("x@lid") == ""  # fix #1 disabled
    finally:
        os.environ.pop("GIVENUP_DECLINE_ENABLED", None)


def test_explicit_true_keeps_on():
    os.environ["GIVENUP_DECLINE_ENABLED"] = "true"
    try:
        assert compute_label("yes", humdan())[0] == "LOST"
    finally:
        os.environ.pop("GIVENUP_DECLINE_ENABLED", None)


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} givenup_killswitch tests passed")
