#!/usr/bin/env python3
"""_is_reengage_enquiry — refines audit #3 (2026-06-07). A previously-LOST/
DISREGARDED customer who sends a GENUINE fresh booking enquiry must REOPEN
(returning customers were being buried); a stray inbound (bare number, 'thanks',
emoji) must NOT reopen a terminal lead.

Run: python3 hermes-bridge/test_reengage_intent.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from labels import _is_reengage_enquiry  # noqa: E402


def test_genuine_enquiries_reopen():
    for m in ("Hi Dubriani, I'd like to check availability for Saturday",
              "do you have anything this weekend for 8 guests?",
              "what's the price for the Bliss 55 tomorrow?",
              "still interested — can we book for my birthday?",
              "I'd like to pay the deposit now"):
        assert _is_reengage_enquiry(m) is True, m


def test_stray_inbound_does_not_reopen():
    for m in ("reach me on 0501234567",   # the audit #3 stray bare-number case
              "okay thank you",
              "thanks",
              "ok",
              "👍",
              "0509876543"):
        assert _is_reengage_enquiry(m) is False, m


def test_none_and_empty_safe():
    assert _is_reengage_enquiry(None) is False
    assert _is_reengage_enquiry("") is False


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} reengage-intent tests passed")
