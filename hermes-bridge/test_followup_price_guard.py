#!/usr/bin/env python3
"""Proactive-draft price-invention guard (Eva/Thunder, 2026-06-10).

PREMISE CORRECTION from the RCA: the sweep draft was NOT history-starved
(journald: waha_count=13 — the thread incl. the operator's "15,000 AED/hr"
rate was fed). The model did PRICE ARITHMETIC: 15,000 x 6 = 90,000 + VAT
4,500 = 94,500 — a TOTAL the operator never quoted (the 87,731 discount
came 29 minutes later). Core rule violated: never emit a price the thread
does not contain.

FIX (deterministic, no extra LLM calls): in handle_draft_followup — the
single chokepoint for ALL proactive drafts (owe-sweep, reengage sweep,
[Draft nudge], /assist draft_nudge) — strip any sentence whose money-like
amount (500..5,000,000, years/phones excluded, format-normalized) does not
literally appear in the conversation history. If everything is stripped,
the existing R4 fact-anchored fallback (price-free) takes over.

Run: python3 hermes-bridge/test_followup_price_guard.py  (or pytest)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import routes  # noqa: E402

EVA_HISTORY = (
    "[2026-06-10 14:57] DUBRIANI: a few of our yachts have jacuzzis...\n"
    "[2026-06-10 15:00] CUSTOMER: Is there any discounts available for the "
    "thunder superyacht\n"
    "[2026-06-10 15:04] DUBRIANI: the Thunder is currently showing a special "
    "rate — 15,000 AED/hr (was 18,000)\n"
    "[2026-06-10 15:04] DUBRIANI: for June 19th, how many hours were you "
    "thinking?\n"
    "[2026-06-10 15:05] CUSTOMER: 6\n")

EVA_DRAFT = [
    "the Thunder Superyacht for 6 hours on June 19th comes to 15,000 AED/hr "
    "× 6 = 90,000 AED + VAT 4,500 AED = 94,500 AED total.",
    "shall i hold the slot for you?",
]


def test_eva_replay_invented_total_stripped():
    """The exact incident: in-thread rate, INVENTED total — must not survive."""
    clean, offenders = routes._strip_invented_prices(EVA_DRAFT, EVA_HISTORY)
    joined = " ".join(clean)
    for bad in ("94,500", "94500", "90,000", "90000", "4,500"):
        assert bad not in joined, f"invented amount {bad} survived: {clean}"
    assert clean == ["shall i hold the slot for you?"], clean
    assert set(offenders) == {4500, 90000, 94500}, offenders


def test_amount_present_in_history_passes():
    hist = EVA_HISTORY + "[15:44] DUBRIANI: after discount we can do 87,731 " \
                         "AED for 6 hours\n"
    draft = ["just a reminder — 87,731 AED for the 6 hours, shall i hold it?"]
    clean, offenders = routes._strip_invented_prices(draft, hist)
    assert clean == draft, clean
    assert offenders == [], offenders


def test_format_normalization_commas_vs_plain():
    hist = "DUBRIANI: the rate is 15,000 AED per hour"
    draft = ["it stays at 15000 AED per hour — want me to check the date?"]
    clean, offenders = routes._strip_invented_prices(draft, hist)
    assert clean == draft and offenders == []


def test_years_hours_guests_phones_ignored():
    draft = ["for June 19 2026, 6 hours with 10-20 guests — reach us on "
             "+971 58 990 1996 anytime."]
    clean, offenders = routes._strip_invented_prices(draft, "no numbers here")
    assert clean == draft, (clean, offenders)
    assert offenders == []


def test_all_invented_returns_empty_for_fallback():
    """Everything stripped -> empty list -> caller's R4 fallback engages."""
    clean, offenders = routes._strip_invented_prices(
        ["the package is 25,000 AED all-in."], "hi, do you have yachts?")
    assert clean == [], clean
    assert offenders == [25000]


def test_in_thread_rate_alone_is_allowed():
    """Repeating the operator's own quoted rate is fine — only NEW numbers die."""
    draft = ["the special 15,000 AED/hr rate is still live for June 19th.",
             "want me to pencil you in?"]
    clean, offenders = routes._strip_invented_prices(draft, EVA_HISTORY)
    assert clean == draft and offenders == []


def test_guard_wired_into_followup_chokepoint():
    """Wiring lock: handle_draft_followup must invoke the guard on BOTH draft
    shapes (messages list + single text) before finalizing draft_text."""
    import inspect
    src = inspect.getsource(routes.handle_draft_followup)
    assert src.count("_strip_invented_prices(") >= 2, \
        "guard not wired into both draft shapes of handle_draft_followup"


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn(); print("PASS", fn.__name__)
        except AssertionError as e:
            failed += 1; print("FAIL", fn.__name__, "-", e or "assert")
        except Exception as e:  # noqa: BLE001
            failed += 1; print("ERROR", fn.__name__, "-", repr(e))
    print(f"{len(fns)-failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
