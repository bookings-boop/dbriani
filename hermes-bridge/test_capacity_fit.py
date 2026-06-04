#!/usr/bin/env python3
"""Capacity bug (2026-06-02): a 50-guest request got a draft recommending a
yacht that seats <=40. Nothing fed the drafter the party size as an enforceable
constraint. This line rides _lead_state_block -> behavioral_context().formatted
-> the live n8n drafter (no n8n change needed), telling it to only recommend
yachts that fit the party.

Locks the pure line-builder: a clear capacity-fit instruction for a known party
size, and '' when unknown (so it's safe to concatenate).

Run: python3 hermes-bridge/test_capacity_fit.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from labels import _party_size_fit_line  # noqa: E402


def test_known_party_emits_constraint():
    line = _party_size_fit_line(50)
    assert "50" in line
    assert "at least 50" in line.lower() or "seat at least 50" in line.lower()
    # must be a hard "only/never" constraint, not a soft suggestion
    assert "only" in line.lower() and "never" in line.lower()


def test_string_party_parsed():
    assert "50" in _party_size_fit_line("50")


def test_unknown_party_is_empty():
    assert _party_size_fit_line("(unknown)") == ""
    assert _party_size_fit_line("") == ""
    assert _party_size_fit_line(None) == ""


def test_zero_or_negative_is_empty():
    assert _party_size_fit_line(0) == ""
    assert _party_size_fit_line("-3") == ""


def test_handles_messy_numeric():
    # e.g. "50 guests" should still surface 50 if leading-int parses; if not
    # parseable, returns '' (never crashes).
    out = _party_size_fit_line("50 guests")
    assert out == "" or "50" in out


# --- Rule 3 (2026-06-04): capacity-NUMBER suppression for small parties, while
#     the capacity-FIT constraint MUST still hold (don't re-break the fit fix) --
def test_small_party_suppresses_capacity_number_but_keeps_fit():
    line = _party_size_fit_line(4).lower()
    # fit constraint preserved (still only recommend a yacht that seats 4)
    assert "at least 4" in line
    assert "only" in line and "never" in line
    # but the capacity NUMBER must not be stated to a small party
    assert "not mention" in line or "do not state" in line
    assert "capacity" in line
    assert "experience" in line  # describe by experience instead


def test_large_party_does_not_suppress_capacity():
    line = _party_size_fit_line(25).lower()
    assert "at least 25" in line          # fit still enforced
    assert "not mention" not in line      # capacity may be stated for 10+


def test_capacity_suppression_boundary_is_under_10():
    assert "not mention" in _party_size_fit_line(9).lower()       # small
    assert "not mention" not in _party_size_fit_line(10).lower()  # 10+


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} capacity-fit tests passed")
