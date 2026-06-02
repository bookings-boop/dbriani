#!/usr/bin/env python3
"""2026-06-02 incident: a literal "false" was sent to a real customer as a
second WhatsApp bubble. Root cause: routes.py:4838 (handle_draft_gated) assigned
the (list, bool) tuple from sanitize_draft_messages WITHOUT unpacking, so the
draft's messages became (["good morning..."], False); n8n's Prepare Send then did
String(False) -> a "false" bubble. The sibling _gate_loop (routes.py:4766)
unpacks correctly — 4838 was the lone defect.

These tests lock the DEFENSE-IN-DEPTH so a junk bubble can never reach a customer
again from ANY path (LLM, refine, future regression):
  (1) _clean_message_bubbles — the canonical guard: a bubble must be a real,
      non-empty string; everything else is dropped.
  (2) sanitize_draft_messages drops non-string elements instead of passing them.
The one-line unpack at routes.py:4838 is the root fix; these are the nets.

Run: python3 hermes-bridge/test_false_bubble.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from labels import _clean_message_bubbles            # noqa: E402
from server import sanitize_draft_messages           # noqa: E402


# ── _clean_message_bubbles — canonical send/persist guard ───────────────────

def test_drops_false_the_exact_incident():
    assert _clean_message_bubbles(
        ["good morning Mr Saif, glad to hear it", False]) == \
        ["good morning Mr Saif, glad to hear it"]


def test_drops_all_non_strings():
    assert _clean_message_bubbles(
        ["hello there friend", False, None, 5, ["nested"], True]) == \
        ["hello there friend"]


def test_drops_empty_and_whitespace():
    assert _clean_message_bubbles(
        ["  ", "", "real message here", "\n"]) == ["real message here"]


def test_drops_literal_poison_words():
    assert _clean_message_bubbles(
        ["false", "true", "none", "null", "FALSE", "the real one"]) == \
        ["the real one"]


def test_trims_and_keeps_real_strings():
    assert _clean_message_bubbles(
        ["  hi there friend  ", "second bubble"]) == \
        ["hi there friend", "second bubble"]


def test_unpacked_tuple_never_leaks_false():
    # If the un-unpacked (list, False) tuple ever reaches the guard, NO "false"
    # bubble can survive (the bool is dropped). The primary unpack fix prevents
    # the tuple; this proves the net holds even if it didn't.
    out = _clean_message_bubbles((["good morning"], False))
    assert all(isinstance(x, str) for x in out)
    assert "false" not in [x.lower() for x in out]


def test_non_list_yields_empty():
    assert _clean_message_bubbles("just a string") == []
    assert _clean_message_bubbles(None) == []
    assert _clean_message_bubbles(False) == []


# ── sanitize_draft_messages — now drops non-strings (was: appended verbatim) ─

def test_sanitize_drops_non_string_elements():
    cleaned, stripped = sanitize_draft_messages(
        ["good morning Mr Saif", False, None])
    assert cleaned == ["good morning Mr Saif"]
    assert stripped is True


def test_sanitize_keeps_clean_strings_untouched():
    cleaned, _ = sanitize_draft_messages(
        ["here is a normal reply about your booking"])
    assert cleaned == ["here is a normal reply about your booking"]


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} false-bubble tests passed")
