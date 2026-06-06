#!/usr/bin/env python3
"""handle_quality_check must HARD-fail a draft that quotes a price not in the
catalog (operator 2026-06-06): cap the score below the regen threshold so the
n8n loop regenerates, and surface a 'price mismatch' flag so the operator sees
it (approval-first). A clean draft keeps the LLM score untouched.

Run: python3 hermes-bridge/test_quality_price_gate.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402
import routes  # noqa: E402


def _run(draft):
    captured = {}

    def fake_send(status, body):
        captured["status"] = status
        captured["body"] = body

    def fake_run_hermes(query, priority=None):
        # LLM scorer says 7/10, clean — the deterministic gate must override.
        return (0, '{"score": 7, "flags": ["weak opener"], "summary": "ok"}', "", 50)

    orig = server.run_hermes
    try:
        server.run_hermes = fake_run_hermes
        # customer_id empty -> the score-telemetry/DB block is skipped (no DB).
        routes.handle_quality_check(
            {"current_draft": draft, "customer_name": "Test", "customer_id": ""},
            fake_send)
    finally:
        server.run_hermes = orig
    return captured.get("body") or {}


def test_wrong_price_draft_is_score_capped_and_flagged():
    body = _run("Von Dutch 40 — up to 8 guests\n600 AED/hr special offer")
    assert body.get("ok") is True, body
    assert body.get("score", 99) <= 3, body          # forced below regen threshold (8)
    assert any("price mismatch" in str(f).lower()
               for f in body.get("flags", [])), body


def test_clean_draft_keeps_llm_score():
    body = _run("Von Dutch 40 — up to 8 guests\nAED 1,400/hr")
    assert body.get("score") == 7, body              # untouched
    assert not any("price mismatch" in str(f).lower()
                   for f in body.get("flags", [])), body


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} quality_price_gate tests passed")
