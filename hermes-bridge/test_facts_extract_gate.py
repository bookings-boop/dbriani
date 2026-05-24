#!/usr/bin/env python3
"""Unit tests for _facts_extract_gate() — the extraction-cost guard.

The gate decides whether a NON-FIRST customer message warrants a fresh
Hermes call to extract {name, dates, yachts, party_size}. Each Hermes
call is ~5-15s + cost; the gate skips messages that clearly carry no
new fact ('thanks', 'ok'), saving budget.

False-negatives are bad (we miss a fact). False-positives are wasteful
(spend Hermes on 'thanks'). Tests cover both directions.

Plain-assert style. Run: python3 hermes-bridge/test_facts_extract_gate.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from server import _facts_extract_gate  # noqa: E402


def test_digits_trigger():
    # Any digit suggests party size / date / phone / amount — worth
    # extracting.
    assert _facts_extract_gate("we are 6") is True
    assert _facts_extract_gate("call me back on 555-1234") is True
    assert _facts_extract_gate("AED 3000 ok?") is True


def test_yacht_keyword_triggers():
    # Curated YACHT_NAMES from system-prompt §7. Hits should always
    # trigger extraction (yacht switch may have happened).
    assert _facts_extract_gate("interested in the Satoshi") is True
    assert _facts_extract_gate("can we see the Aurora?") is True
    assert _facts_extract_gate("the Pershing looks great") is True


def test_date_words_trigger():
    assert _facts_extract_gate("can we do Saturday?") is True
    assert _facts_extract_gate("how about tomorrow morning") is True


def test_name_intro_triggers():
    assert _facts_extract_gate("hi, I'm Mark") is True
    assert _facts_extract_gate("this is Sarah") is True


def test_booking_words_trigger():
    assert _facts_extract_gate("we want to book") is True
    assert _facts_extract_gate("let's reserve it") is True


def test_filler_does_not_trigger():
    # 'thanks' / 'ok' / 'sounds great' carry no facts — skip Hermes.
    assert _facts_extract_gate("ok thanks!") is False
    assert _facts_extract_gate("sounds great") is False
    assert _facts_extract_gate("👍") is False


def test_empty_inputs():
    # Empty / None must not crash.
    assert _facts_extract_gate("") is False
    assert _facts_extract_gate(None) is False


def test_jetcar_regression():
    # Today's regression: +491742083756 messaged 'I'm interested to
    # check availability of the following jetcar: MODEL R #008'.
    # Gate MUST return True (contains digits + 'interested' booking
    # intent). If this regresses, jetcar prospects will silently miss
    # extraction again.
    msg = "Hi, I'm interested to check availability of the following " \
          "jetcar: MODEL R #008"
    assert _facts_extract_gate(msg) is True, \
        "Jetcar-prospect message must trigger extraction — operator " \
        "lost customer +491742083756 to this gap before"


def test_mixed_case_yacht():
    # Yacht-name match must be case-insensitive (operator types
    # informally).
    assert _facts_extract_gate("SATOSHI when free?") is True
    assert _facts_extract_gate("aurora ok") is True


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} facts_extract_gate tests passed")
