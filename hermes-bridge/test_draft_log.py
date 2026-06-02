#!/usr/bin/env python3
"""Draft-log foundation (Part 1 of draft-logging + shadow-mode). ADDITIVE
observability — draft_log (migration 007) RECORDS what was drafted/scored/done;
it NEVER changes a send. This locks the pure column whitelist + coercion that
feeds the fail-safe UPSERT (`_draft_log_write`): only known columns, correctly
typed, with None dropped (so a partial lifecycle UPSERT never NULLs a prior
value).

Run: python3 hermes-bridge/test_draft_log.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from labels import _draft_log_columns  # noqa: E402


def test_whitelist_drops_unknown_keys():
    out = _draft_log_columns({"customer_id": "x@c.us", "evil; DROP": "1",
                              "nonsense": 5})
    assert out == {"customer_id": "x@c.us"}


def test_none_values_dropped():
    # UPSERT must not overwrite a prior value with NULL when a field is absent.
    out = _draft_log_columns({"draft_text": "hi", "score": None,
                              "outcome": None})
    assert out == {"draft_text": "hi"}


def test_score_coerced_to_int():
    assert _draft_log_columns({"score": "8"})["score"] == 8
    assert _draft_log_columns({"score": 8.0})["score"] == 8
    assert _draft_log_columns({"score": 8})["score"] == 8


def test_score_bool_not_coerced():
    # True must never become score 1.
    assert "score" not in _draft_log_columns({"score": True})


def test_score_uncoercible_dropped():
    assert "score" not in _draft_log_columns({"score": "n/a"})


def test_is_shadow_coerced_to_bool():
    assert _draft_log_columns({"is_shadow": True})["is_shadow"] is True
    assert _draft_log_columns({"is_shadow": False})["is_shadow"] is False


def test_text_coerced_to_str():
    out = _draft_log_columns({"customer_name": 12345, "draft_text": "hello"})
    assert out["customer_name"] == "12345"
    assert out["draft_text"] == "hello"


def test_empty_returns_empty():
    assert _draft_log_columns({}) == {}
    assert _draft_log_columns({"score": None}) == {}


def test_outcome_and_mode_passthrough():
    out = _draft_log_columns({"mode": "shadow", "outcome": "would_send",
                              "trigger_kind": "inbound"})
    assert out == {"mode": "shadow", "outcome": "would_send",
                   "trigger_kind": "inbound"}


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn(); print("PASS", fn.__name__)
        except AssertionError as e:
            failed += 1; print("FAIL", fn.__name__, "-", e or "assert")
        except Exception as e:
            failed += 1; print("ERROR", fn.__name__, "-", repr(e))
    print(f"{len(fns)-failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
