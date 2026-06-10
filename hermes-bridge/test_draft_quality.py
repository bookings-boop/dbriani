#!/usr/bin/env python3
"""Unit tests for draft-quality fixes 2026-06-06 (bundle: 2A/3A).

Pure pieces only (no DB / no network):
  - 2A: _format_draft_to_score() — bubble-aware scorer input so the
    wall_of_text rule judges real WhatsApp message structure, not a blob.
  - 3A: validate_draft_prices() returns the CORRECT catalog price in the
    mismatch string + _price_correction_hint() builds the regen ground-truth.

Plain-assert style. Run: python3 hermes-bridge/test_draft_quality.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from server import (  # noqa: E402
    _format_draft_to_score,
    validate_draft_prices,
    _price_correction_hint,
)


# --- 2A: _format_draft_to_score ---------------------------------------------
def test_format_draft_list_uses_bubble_markers():
    out = _format_draft_to_score(
        ["Hi there", "Here's the Bliss 55", "Want me to hold it?"])
    assert "[bubble 1/3]" in out
    assert "[bubble 2/3]" in out
    assert "[bubble 3/3]" in out
    assert "3 WhatsApp message bubbles" in out


def test_format_draft_single_bubble_is_singular():
    out = _format_draft_to_score(["Just one message"])
    assert "[bubble 1/1]" in out
    assert "1 WhatsApp message bubble;" in out  # singular — no trailing 's'


def test_format_draft_string_is_plain_no_markers():
    out = _format_draft_to_score("a plain joined string")
    assert "--- DRAFT TO SCORE ---" in out
    assert "a plain joined string" in out
    assert "[bubble" not in out


def test_format_draft_filters_blank_bubbles():
    out = _format_draft_to_score(["real", "  ", "", "second"])
    assert "[bubble 1/2]" in out
    assert "[bubble 2/2]" in out
    assert "[bubble 3" not in out


def test_format_draft_none_and_empty_safe():
    assert "DRAFT TO SCORE" in _format_draft_to_score(None)
    assert "DRAFT TO SCORE" in _format_draft_to_score([])


# --- 3A: price correction ground-truth --------------------------------------
def test_validate_prices_reports_correct_catalog_value():
    # A wrong Bliss 55 rate must name the CORRECT catalog rate so regen can fix it.
    msgs = validate_draft_prices("The Bliss 55 is AED 600/hr for today.")
    assert msgs, "expected a price mismatch to be flagged"
    joined = " ".join(msgs).lower()
    assert "600" in joined            # the wrong quote
    assert "catalog" in joined        # and the correct catalog value


def test_validate_prices_clean_draft_no_flags():
    assert validate_draft_prices("Happy to help — when would you like to sail?") == []


def test_price_correction_hint_injects_ground_truth():
    mismatches = ["Bliss 55 quoted AED 600/hr (catalog: 1,400/hr)"]
    hint = _price_correction_hint(mismatches)
    assert "1,400" in hint            # the CORRECT price must be in the regen hint
    assert "catalog" in hint.lower()


def test_price_correction_hint_empty_when_no_mismatch():
    assert _price_correction_hint([]) == ""


# --- 1B: /draft-gated re-scores the original with the SAME scorer -----------
# Network/DB boundary stubbed (build_quality_query hits the DB; _anthropic_*
# hit the network) — the contract under test is "original_score is returned,
# computed by the same scorer as newScore", so n8n's swap is like-for-like.
def test_draft_gated_returns_original_score_same_scorer():
    import routes
    import server
    saved = (server.build_quality_query_parts, routes._anthropic_score,
             routes._anthropic_draft, server.sanitize_draft_messages)
    try:
        # Lever 1a: the gate now calls build_quality_query_parts -> (prefix, body)
        # and _anthropic_score(body, system_prefix=prefix). Stub the split form.
        server.build_quality_query_parts = lambda p: (
            "PFX", "Q::" + str(p.get("current_draft")))
        routes._anthropic_draft = lambda *a, **k: (["regen bubble"], "notes", None)
        # original ("old draft") scores 5; regenerated ("regen bubble") scores 9
        routes._anthropic_score = lambda q, system_prefix=None: (
            (9, [], "") if "regen bubble" in q else (5, [], ""))
        server.sanitize_draft_messages = lambda m: (m, False)
        captured = {}
        routes.handle_draft_gated(
            {"system_prompt": "sys", "customer_id": "c", "customer_name": "X",
             "user_message": "hi", "original_draft": "old draft",
             "threshold": 8, "max_attempts": 1},
            lambda code, body: captured.update(body=body))
        b = captured["body"]
        assert b["ok"] is True
        assert b["score"] == 9             # regenerated draft (Anthropic)
        assert b["original_score"] == 5    # original, SAME scorer -> like-for-like
    finally:
        (server.build_quality_query_parts, routes._anthropic_score,
         routes._anthropic_draft, server.sanitize_draft_messages) = saved


def test_draft_gated_original_score_none_when_absent():
    import routes
    import server
    saved = (server.build_quality_query_parts, routes._anthropic_score,
             routes._anthropic_draft, server.sanitize_draft_messages)
    try:
        server.build_quality_query_parts = lambda p: (
            "PFX", "Q::" + str(p.get("current_draft")))
        routes._anthropic_draft = lambda *a, **k: (["regen bubble"], "n", None)
        routes._anthropic_score = lambda q, system_prefix=None: (9, [], "")
        server.sanitize_draft_messages = lambda m: (m, False)
        captured = {}
        routes.handle_draft_gated(
            {"system_prompt": "sys", "customer_id": "", "user_message": "hi",
             "threshold": 8, "max_attempts": 1},
            lambda code, body: captured.update(body=body))
        assert captured["body"]["original_score"] is None   # backward-compatible
    finally:
        (server.build_quality_query_parts, routes._anthropic_score,
         routes._anthropic_draft, server.sanitize_draft_messages) = saved


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} draft-quality tests passed")
