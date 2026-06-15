#!/usr/bin/env python3
"""Fix #2 (2026-06-14, Humdan +971501848003): a bare "yes" answering the
"Have you given up on booking ...?" nudge must classify the lead as terminal
LOST (signal 'declined_ghost_recovery'), NOT HOT 'multi_yacht_engaged'. The
terminal label is what stops the re-nudge and the sweep's stale-yacht re-warm.

Exercises compute_label (the live inbound path, routes.py:1751). The only DB
touch is _last_outbound_is_givenup_nudge, stubbed per case (repo idiom).

Run: python3 hermes-bridge/test_givenup_label.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402
from server import compute_label  # noqa: E402

# Stub the unrelated DB helpers in compute_label's cascade so this runs locally
# without docker/psql (same approach as test_reject_to_lost.py).
server._has_recent_payment_intent = lambda *a, **k: False
server._has_recent_payment_link_sent = lambda *a, **k: False
server._recent_outbound_quote = lambda *a, **k: False


def _nudge(val):
    """Force whether our last outbound was the 'have you given up?' nudge."""
    server._last_outbound_is_givenup_nudge = lambda *a, **k: val


def humdan(**ov):
    # Humdan's facts at the 'yes': WARM, re-warmed off the stale quoted yachts.
    d = {"customer_id": "8761951404156@lid", "message_count": 62,
         "label": "WARM", "yachts": "Élan 44, Bliss 55, Zenith 64", "dates": ""}
    d.update(ov)
    return d


# --- THE BUG: 'yes' answering the nudge must terminalize, not pitch ---------
def test_humdan_yes_after_nudge_goes_lost():
    _nudge(True)
    lab, sig, _ = compute_label("yes", humdan())
    assert lab == "LOST" and sig == "declined_ghost_recovery", (lab, sig)


def test_clarifying_explicit_given_up_goes_lost():
    _nudge(False)  # context-independent — explicit 'given up'
    assert compute_label("I said yes I've given up", humdan())[0] == "LOST"


# --- SAFETY: the same 'yes' in other contexts must NOT terminalize ----------
def test_yes_without_nudge_not_lost():
    _nudge(False)
    lab, _sig, _ = compute_label("yes", humdan())
    assert lab != "LOST", lab            # stays active (multi_yacht_engaged)


def test_positive_reengage_after_nudge_not_lost():
    _nudge(True)
    assert compute_label("yes please let's book it", humdan())[0] != "LOST"


def test_no_after_nudge_not_lost():
    _nudge(True)
    assert compute_label("no", humdan())[0] != "LOST"


def test_question_after_nudge_not_lost():
    _nudge(True)
    assert compute_label("yes what's the price for the 20th?", humdan())[0] != "LOST"


def test_confirmed_paid_lead_never_lost():
    _nudge(True)  # never demote a paid booking, even on a 'yes' to a nudge
    assert compute_label("yes", humdan(label="CONFIRMED"))[0] != "LOST"


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} givenup_label tests passed")
