#!/usr/bin/env python3
"""Payment match-buttons (2026-06-02) — foundation.

When a Nomod charge is TRULY unmatched (no link / no phone / no amount+time
hit), the operator today must read the Nomod dashboard and type
`/label <name> CONFIRMED` by hand (routes.py ~5671). These buttons replace
that typing: the bridge offers the current WAITING_FOR_PAYMENT customers as
tap-to-confirm candidates. The operator still does the real identification
(payer name/phone is shown) — the button just saves the manual /label.

This locks the two PURE pieces every button design needs, so they can't
drift and so the DB/n8n wiring can build on a verified base:

  build_paymatch_keyboard(charge_id, candidates) -> inline_keyboard rows
  parse_paymatch_callback("paymatch:<charge8>:<cid>") -> {charge, cid}

Telegram hard limit: callback_data must be <= 64 BYTES. A wrong button that
mis-promotes a real payment is a money/customer error, so the parser must be
strict (reject anything that isn't a paymatch callback) and the keyboard must
never emit an over-long callback_data.

Run: python3 hermes-bridge/test_paymatch.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from labels import build_paymatch_keyboard, parse_paymatch_callback  # noqa: E402


# ── keyboard builder ───────────────────────────────────────────────────────

def test_one_button_per_candidate_plus_dismissal():
    kb = build_paymatch_keyboard("abcd1234ef", [
        {"customer_id": "971500000001", "name": "Sara K", "phone": "971500000001"},
        {"customer_id": "971500000002", "name": "Omar",   "phone": "971500000002"},
    ])
    # 2 candidate rows + 1 dismissal row
    assert len(kb) == 3, kb
    # every row is a list of exactly one button (phones+names are long)
    assert all(len(row) == 1 for row in kb), kb


def test_callback_data_encodes_charge_and_cid():
    kb = build_paymatch_keyboard("abcd1234ef", [
        {"customer_id": "971500000001", "name": "Sara K"},
    ])
    cb = kb[0][0]["callback_data"]
    # charge is truncated to 8 chars (matches charge_id[:8] used everywhere)
    assert cb == "paymatch:abcd1234:971500000001", cb


def test_button_text_prefers_name_then_phone():
    kb = build_paymatch_keyboard("c", [
        {"customer_id": "971500000001", "name": "Sara K", "phone": "971500000001"},
        {"customer_id": "971500000002", "phone": "971500000002"},  # no name
    ])
    assert "Sara K" in kb[0][0]["text"]
    assert "971500000002" in kb[1][0]["text"]


def test_dismissal_button_present_and_routes_to_none():
    kb = build_paymatch_keyboard("abcd1234ef", [
        {"customer_id": "971500000001", "name": "Sara K"},
    ])
    dismissal = kb[-1][0]
    assert dismissal["callback_data"] == "paymatch:abcd1234:none", dismissal
    # parsing the dismissal yields cid None (operator declined to match)
    assert parse_paymatch_callback(dismissal["callback_data"]) == {
        "charge": "abcd1234", "cid": None}


def test_empty_candidates_still_offers_dismissal_only():
    kb = build_paymatch_keyboard("abcd1234ef", [])
    assert len(kb) == 1
    assert kb[0][0]["callback_data"] == "paymatch:abcd1234:none"


def test_callback_data_never_exceeds_64_bytes():
    # A pathological cid must not produce an over-limit callback_data.
    kb = build_paymatch_keyboard("abcd1234ef", [
        {"customer_id": "9715" + "0" * 80, "name": "X" * 200},
    ])
    for row in kb:
        for btn in row:
            assert len(btn["callback_data"].encode("utf-8")) <= 64, btn


def test_candidates_are_capped():
    many = [{"customer_id": f"9715000000{i:02d}", "name": f"C{i}"}
            for i in range(20)]
    kb = build_paymatch_keyboard("abcd1234ef", many)
    # capped candidate rows + 1 dismissal row; never an unbounded keyboard
    assert len(kb) <= 7, len(kb)


# ── callback parser ────────────────────────────────────────────────────────

def test_parse_valid_match():
    assert parse_paymatch_callback("paymatch:abcd1234:971500000001") == {
        "charge": "abcd1234", "cid": "971500000001"}


def test_parse_rejects_other_callbacks():
    for other in ("send:xyz", "disregard:971500000001", "nudge:9715",
                  "", "paymatch", "paymatch:", "randomtext"):
        assert parse_paymatch_callback(other) is None, other


def test_parse_dismissal_is_none_cid():
    assert parse_paymatch_callback("paymatch:abcd1234:none") == {
        "charge": "abcd1234", "cid": None}


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} paymatch tests passed")
