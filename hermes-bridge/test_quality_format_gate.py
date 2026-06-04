#!/usr/bin/env python3
"""The quality scorer must ENFORCE multi-option formatting (2026-06-04).

Root cause the operator's format feedback never stuck (QC investigation): the
scorer (build_quality_query) had NO spacing flag and its `too_verbose` flag
penalised vertical lists, so a crammed multi-option reply scored fine and a
well-spaced one got compressed back by the improver. This locks the formatting
gate into the scorer rubric so a wall-of-text multi-option draft scores low and
routes to regen — the enforcement leg that makes the layout rule stick.

Run: python3 hermes-bridge/test_quality_format_gate.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402


def _query():
    orig = server._psql
    server._psql = lambda *a, **k: ("", None)  # offline: no DB rules/analysis
    try:
        return server.build_quality_query({
            "customer_id": "971500000000@c.us",
            "incoming_message": "which yachts do you have?",
            "current_draft": "Bliss 55 1400/hr, Lana 62 2000/hr",
        })
    finally:
        server._psql = orig


def test_scorer_has_wall_of_text_flag():
    assert "wall_of_text" in _query().lower()


def test_scorer_gates_unspaced_options_low():
    low = _query().lower()
    assert "own line" in low or "own message bubble" in low
    assert "5 or lower" in low or "score it 5" in low


def test_scorer_protects_spacing_from_verbosity():
    low = _query().lower()
    assert "penalised as too_verbose" in low or "penalized as too_verbose" in low


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} quality-format-gate tests passed")
