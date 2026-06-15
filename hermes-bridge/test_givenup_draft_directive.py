#!/usr/bin/env python3
"""Fix #1 (2026-06-14, Humdan +971501848003): the REAL-TIME no-push directive.
When our last outbound was the "Have you given up on booking ...?" nudge and the
customer's latest inbound signals they HAVE given up, behavioral_context must
inject a directive telling the drafter to close gracefully — NOT pitch. This is
the only fix that lands BEFORE the synchronous draft, so it pre-empts the
"glad you're still interested!" misread even while the label is still WARM.

The two conversation-store reads are stubbed per case. Run:
  python3 hermes-bridge/test_givenup_draft_directive.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402


def _ctx(last_out_is_nudge, latest_inbound):
    server._last_outbound_is_givenup_nudge = lambda *a, **k: last_out_is_nudge
    server._latest_inbound_body = lambda *a, **k: latest_inbound


# --- the directive fires when 'yes' answers the given-up nudge --------------
def test_directive_fires_on_yes_after_nudge():
    _ctx(True, "yes")
    line = server._ghost_recovery_decline_line("8761951404156@lid")
    low = line.lower()
    assert "given up" in low and "do not" in low and "still interested" in low, line


def test_directive_fires_on_explicit_given_up():
    _ctx(True, "I said yes I've given up")
    assert server._ghost_recovery_decline_line("8761951404156@lid") != ""


# --- SAFETY: must stay silent (no goodbye directive) when not a decline -----
def test_directive_silent_when_customer_said_no():
    _ctx(True, "no")                       # 'no' to 'have you given up?' = keen
    assert server._ghost_recovery_decline_line("x@lid") == ""


def test_directive_silent_when_reengaging():
    _ctx(True, "yes please let's book it")
    assert server._ghost_recovery_decline_line("x@lid") == ""


def test_directive_silent_when_last_outbound_not_givenup_nudge():
    _ctx(False, "yes")                     # bare 'yes' with no nudge context
    assert server._ghost_recovery_decline_line("x@lid") == ""


# --- integration: the directive reaches the drafter via _lead_state_block ---
def test_lead_state_block_surfaces_directive():
    _ctx(True, "yes")
    # facts row: label~~isc~~irea~~dates~~psize~~yachts~~mct (see _lead_state_block)
    server._psql = lambda *a, **k: (
        "WARM~~40~~engaged on multiple yachts~~~~~~Élan 44, Bliss 55~~62", None)
    block = server._lead_state_block("8761951404156@lid")
    assert "given up" in block.lower(), block


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} givenup_draft_directive tests passed")
