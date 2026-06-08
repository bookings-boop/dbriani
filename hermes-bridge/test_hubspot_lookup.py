#!/usr/bin/env python3
"""Tests for hubspot_lookup.py — lock the fail-open safety contract.

Self-contained: stubs `hermes_exclusion_guards`, monkeypatches `_http_search` /
`urlopen`, so it runs anywhere with stdlib python3 (no box modules, no network).
Run:  python3 test_hubspot_lookup.py    (also pytest-compatible)
"""
import os
import sys
import types
import socket

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


# ── stub the exclusion guard BEFORE importing the module under test ───────────
def _install_guard(blocked=False, boom=False):
    m = types.ModuleType("hermes_exclusion_guards")

    def is_excluded(identity, lid_resolver=None, block_if_unresolved=False):
        if boom:
            raise RuntimeError("guard exploded")  # context_block must swallow -> ""
        return bool(blocked)

    m.is_excluded = is_excluded
    sys.modules["hermes_exclusion_guards"] = m


_install_guard(blocked=False)
import hubspot_lookup as H  # noqa: E402

_ORIG_SEARCH = H._http_search  # keep the real one so timeout test can restore it


# ── fixtures ──────────────────────────────────────────────────────────────────
MIR = {
    "firstname": "Mir", "lastname": "Aashaq", "phone": "+971502350420",
    "customer_type": "repeat-client",
    "hermes_data_source": "chat-reliant", "hermes_data_quality_flag": "false",
    "last_quote_amount_aed": "6426",
    "hermes_summary": (
        "Type: price-sensitive, repeat client\n"
        "Wanted: F1 trackside yacht with full track view; 2022: Berth 177 for 430k AED; "
        "2024: 88ft trackside yacht for 300k AED cash\n"
        "Objection: price-too-high (wanted 300k, asked for 350k); "
        "tickets-separate-cost (1500 each) — overcame, booked\n"
        "Last quote: AED 6,426 (gateway)\n"
        "Re-engage: Proactive NYE offer or spring F1 2025 pre-booking with "
        "guaranteed berth + ticket delivery timeline"
    ),
}
MIR_CID = "971502350420@c.us"


def _reset(enabled=True, token="test-token", blocked=False, boom=False):
    H.cache_clear()
    H._http_search = _ORIG_SEARCH  # restore real search; tests re-patch as needed
    _install_guard(blocked=blocked, boom=boom)
    if enabled:
        os.environ["HUBSPOT_LOOKUP_ENABLED"] = "1"
    else:
        os.environ.pop("HUBSPOT_LOOKUP_ENABLED", None)
    if token:
        os.environ["HUBSPOT_TOKEN"] = token
    else:
        os.environ.pop("HUBSPOT_TOKEN", None)


def _patch_search(fn):
    H._http_search = fn  # type: ignore[attr-defined]


# ── tests ───────────────────────────────────────────────────────────────────
def test_disabled_is_a_pure_noop():
    _reset(enabled=False)
    called = {"n": 0}
    _patch_search(lambda k: called.__setitem__("n", called["n"] + 1) or MIR)
    assert H.context_block(MIR_CID) == ""
    assert called["n"] == 0, "OFF must not touch HubSpot at all"


def test_excluded_number_blocked():
    _reset(blocked=True)
    called = {"n": 0}
    _patch_search(lambda k: called.__setitem__("n", called["n"] + 1) or MIR)
    assert H.context_block(MIR_CID) == ""
    assert called["n"] == 0, "excluded numbers must not be looked up"


def test_guard_error_fails_open():
    _reset(boom=True)
    _patch_search(lambda k: MIR)
    assert H.context_block(MIR_CID) == ""  # guard raised -> swallowed -> no block


def test_no_contact_returns_empty():
    _reset()
    _patch_search(lambda k: None)
    assert H.context_block(MIR_CID) == ""


def test_dq_flagged_contact_skipped():
    _reset()
    bad = dict(MIR, hermes_data_quality_flag="true")
    _patch_search(lambda k: bad)
    assert H.context_block(MIR_CID) == ""


def test_renders_block_and_scrubs_money():
    _reset()
    _patch_search(lambda k: MIR)
    out = H.context_block(MIR_CID)
    # structure present
    assert "RETURNING CUSTOMER" in out
    assert "Mir Aashaq" in out
    assert "repeat-client" in out
    assert "Context only" in out
    # behavioral nuance kept
    assert "price-sensitive" in out
    assert "F1 trackside" in out
    assert "Re-engage" in out
    # NO money/figures leak through
    for leak in ("430k", "300k", "350k", "6,426", "6426", "1500 each", "Last quote"):
        assert leak not in out, f"money leaked into block: {leak!r}\n---\n{out}"


def test_failopen_on_timeout():
    # exercise the REAL _http_search with a urlopen that times out
    _reset()
    import hubspot_lookup as HH
    orig = HH.urllib.request.urlopen

    def boom(*a, **k):
        raise socket.timeout("timed out")

    HH.urllib.request.urlopen = boom
    try:
        assert HH._http_search("971502350420") is None
        assert HH.context_block(MIR_CID) == ""   # fail-open all the way through
    finally:
        HH.urllib.request.urlopen = orig


def test_cache_hit_avoids_second_call():
    _reset()
    calls = {"n": 0}

    def counting(k):
        calls["n"] += 1
        return MIR

    _patch_search(counting)
    a = H.context_block(MIR_CID)
    b = H.context_block(MIR_CID)
    assert a == b and a != ""
    assert calls["n"] == 1, f"expected 1 HubSpot call, got {calls['n']}"


def test_cache_remembers_misses():
    _reset()
    calls = {"n": 0}

    def counting(k):
        calls["n"] += 1
        return None

    _patch_search(counting)
    assert H.context_block(MIR_CID) == ""
    assert H.context_block(MIR_CID) == ""
    assert calls["n"] == 1, "a miss should be cached, not re-queried"


def test_normalize_phone_parity():
    assert H.normalize_phone("+971502350420") == "971502350420"
    assert H.normalize_phone("0502350420") == "971502350420"
    assert H.normalize_phone("502350420") == "971502350420"
    assert H.normalize_phone("00971502350420") == "971502350420"
    assert H.normalize_phone("") == ""


def test_cid_resolution():
    assert H._cid_to_phone("971502350420@c.us", None) == "971502350420"
    assert H._cid_to_phone("abc123@lid", lambda c: "971502350420@c.us") == "971502350420@c.us"
    assert H._cid_to_phone("abc123@lid", None) == ""   # no resolver -> no phone


def test_scrub_unit_keeps_context_drops_figures():
    s = H._scrub_money(MIR["hermes_summary"])
    assert "Berth 177" in s and "88ft" in s and "F1 2025" in s
    for leak in ("430k", "300k", "350k", "AED 6,426", "1500 each", "Last quote"):
        assert leak not in s, f"{leak!r} survived scrub:\n{s}"


def test_scrub_exotic_forms_leave_no_figure():
    import re
    leak = re.compile(
        r"(?i)(?:aed|dhs|dirham|usdt|usd|btc|eth|eur|gbp)\s*\.?\s*\d"
        r"|\d[\d.,]*\s*k\b|\b\d{1,3}(?:,\d{3})+\b|[$€£]\s?\d"
        r"|(?<![\d.])(?!(?:19|20)\d\d\b)\d{4,7}\b|\d[\d,]*\s+each\b")
    for s in [
        "quoted AED 5k for sunset cruise",
        "negotiated 5,000 dirhams down from 6,000 dirhams",
        "wanted the 8000 Sunseeker",
        "budget was 50k for NYE",
        "paid 5000 USDT deposit",
        "tipped 0.5 BTC; pay 2.5 ETH",
        "quote was $5k, pushed for $4,000",
        "range 4-5k for half-day",
        "AED 5,000-6,000 depending on yacht",
        "5k to 10k budget",
        "deposit 600 then balance 750",
        "negotiate 4500 vs 5000",
        "£300 corkage; €1,200 catering",
        "12,500 total for Princess 60",
        "1500 each for tickets",
    ]:
        out = H._scrub_money(s)
        assert not leak.search(out), f"figure survived: {out!r}  (from {s!r})"


def test_scrub_preserves_nonfinancial_context():
    s = ("Berth 177 in 2022; 88ft yacht for 2025 F1; Princess 60; 30 guests; "
         "4-hour minimum; 21 pax; 2026 campaign; Feretti 780")
    out = H._scrub_money(s)
    for keep in ("Berth 177", "2022", "88ft", "2025", "Princess 60",
                 "30 guests", "21 pax", "2026", "4-hour", "Feretti 780"):
        assert keep in out, f"over-stripped {keep!r}: {out!r}"


# ── runner (no pytest needed) ─────────────────────────────────────────────────
if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL  {t.__name__}: {e}")
            failed += 1
        except Exception as e:  # noqa: BLE001
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    # show the live-shaped render for eyeballing
    _reset()
    _patch_search(lambda k: MIR)
    print("\n===== context_block(Mir Aashaq) — actual output =====")
    print(H.context_block(MIR_CID))
    sys.exit(1 if failed else 0)
