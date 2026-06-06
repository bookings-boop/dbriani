#!/usr/bin/env python3
"""Bug F: the bot greeted but then dumped a generic 3-yacht options list without
ever asking the customer's name (and addressed them by the WhatsApp profile
name). _first_contact_line(message_count) gives the drafter an early-conversation
directive (greet + ask the name + don't lead with a name-less options dump),
riding _lead_state_block -> behavioral_context -> the drafter. '' once the
conversation is past first contact (safe to concatenate).

Run: python3 hermes-bridge/test_first_contact.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from server import _first_contact_line, FIRST_CONTACT_DIRECTIVE  # noqa: E402


def test_first_contact_emits_directive():
    line = _first_contact_line(1).lower()
    assert "name" in line, line
    assert "greet" in line, line
    # don't dump a generic options list before getting the name/context
    assert "option" in line or "dump" in line or "pitch" in line, line


def test_msg_two_still_first_contact():
    assert _first_contact_line(2) == FIRST_CONTACT_DIRECTIVE


def test_past_first_contact_is_empty():
    assert _first_contact_line(5) == ""
    assert _first_contact_line(10) == ""


def test_non_numeric_or_missing_is_empty():
    assert _first_contact_line("(unknown)") == ""
    assert _first_contact_line("") == ""
    assert _first_contact_line(None) == ""


def test_directive_is_substantial():
    assert len(FIRST_CONTACT_DIRECTIVE.strip()) > 100


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} first_contact tests passed")
