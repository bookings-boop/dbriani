#!/usr/bin/env python3
"""Deterministic price validator — catches fabricated prices a soft prompt rule
can't (the NO_INVENT_DIRECTIVE existed yet the LLM still invented '375 AED'
fine-dining 2026-06-01 AND '600 AED/hr' Von Dutch 2026-06-06).

validate_draft_prices(text) returns a list of human-readable mismatch strings
(empty = clean). CONSERVATIVE allowlist: only validates yachts/items it knows
authoritatively (operator-confirmed 2026-06-06) — unknown yachts/items are NOT
flagged, so it cannot false-positive a legit quote on the live send path.

Run: python3 hermes-bridge/test_price_validator.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from server import validate_draft_prices  # noqa: E402


# --- yacht hourly rates ------------------------------------------------------
def test_von_dutch_600_is_flagged():
    m = validate_draft_prices("Von Dutch 40 — up to 8 guests\n600 AED/hr special offer")
    assert m, m
    assert any("600" in x and "Von Dutch" in x for x in m), m


def test_von_dutch_1400_is_clean():
    assert validate_draft_prices("Von Dutch 40 — 8 guests\nAED 1,400/hr") == []


def test_bliss_anchor_1100_and_list_1400_clean():
    assert validate_draft_prices("Bliss 55 — up to 17 guests\n~AED 1,400/hr~ "
                                 "AED 1,100/hr special offer") == []


def test_bliss_wrong_rate_flagged():
    m = validate_draft_prices("Bliss 55\nAED 999/hr")
    assert any("999" in x for x in m), m


def test_unknown_yacht_not_flagged():
    # Zenith 64 (1,300/hr) is not in the conservative allowlist -> never flagged.
    assert validate_draft_prices("Zenith 64 — up to 22 guests\nAED 1,300/hr") == []


def test_multi_yacht_card_all_correct_is_clean():
    draft = ("Zenith 64 — up to 22 guests\nAED 1,300/hr\n\n"
             "Bliss 55 — up to 17 guests\n~AED 1,400/hr~ AED 1,100/hr\n\n"
             "Sunseeker Satoshi 70 — up to 15 guests\nAED 3,000/hr")
    assert validate_draft_prices(draft) == []


def test_satoshi_morning_floor_1500_clean():
    assert validate_draft_prices("Sunseeker Satoshi 70\nAED 1,500/hr morning") == []


def test_yacht_then_unrelated_hourly_rate_not_false_positive():
    # a jet-ski /hr rate well after the yacht card must NOT be attributed to the
    # yacht (proximity guard) — else a legit draft would be wrongly flagged.
    draft = ("Bliss 55 — up to 17 guests\nAED 1,100/hr\n\n"
             "you can also add 2 jet skis at AED 600/hr each")
    assert validate_draft_prices(draft) == []


# --- catering ----------------------------------------------------------------
def test_fine_dining_375_flagged():
    m = validate_draft_prices("the fine dining menu for 2 starts from 375 AED per person")
    assert any("375" in x and "Fine Dining" in x for x in m), m


def test_fine_dining_2500_clean():
    assert validate_draft_prices("fine dining for 2 — from AED 2,500 (chef included)") == []


def test_bbq_2500_price_and_1500_min_clean_wrong_flagged():
    # Audit #2 (2026-06-07): Premium BBQ = AED 2,500 incl. chef; 1,500 is the
    # MIN-SPEND floor. BOTH are valid catalog numbers; a fabricated value is
    # flagged. (Previously the test wrongly expected 2,500 — the real price — to
    # be flagged, steering regen to under-quote by 1,000.)
    assert validate_draft_prices("premium bbq for 2 — AED 2,500 (chef incl.)") == []
    assert validate_draft_prices("premium bbq — min spend AED 1,500") == []
    m = validate_draft_prices("premium bbq menu for 2 — AED 600")
    assert any("600" in x for x in m), m


def test_catering_term_near_yacht_hourly_rate_no_false_positive():
    # 'fine dining' mentioned, but the nearby AED figure is a yacht /hr rate,
    # not a catering price -> must NOT be flagged as a catering mismatch.
    assert validate_draft_prices(
        "fine dining available — the Satoshi is AED 3,000/hr") == []


# --- add-ons (balloon decor / birthday cake = AED 300) ----------------------
def test_addon_balloon_wrong_price_flagged():
    m = validate_draft_prices("romantic balloon decor inside cabin — AED 500")
    assert any("300" in x for x in m), m


def test_addon_balloon_correct_300_clean():
    assert validate_draft_prices("romantic balloon decor — from AED 300") == []


def test_addon_birthday_cake_wrong_flagged():
    m = validate_draft_prices("birthday cake — AED 150")
    assert any("Birthday Cake" in x for x in m), m


def test_addon_birthday_cake_300_clean():
    assert validate_draft_prices("birthday cake — from AED 300") == []


def test_addon_birthday_cake_500_2kg_clean():
    # Audit #2: 2kg cake = AED 500 is a real catalog value — must NOT be flagged
    # (the old {300}-only set steered the regen to under-quote a faithful 2kg cake).
    assert validate_draft_prices("2kg birthday cake — AED 500") == []


def test_addon_term_near_yacht_rate_no_false_positive():
    # 'balloon' mentioned but the nearby AED figure is a yacht /hr rate.
    assert validate_draft_prices("balloon decor available — the Satoshi is AED 3,000/hr") == []


def test_empty_safe():
    assert validate_draft_prices("") == []
    assert validate_draft_prices(None) == []


# --- Satoshi RANGE (operator 2026-06-11: list 3,000, discount floor 2,000 — a
# point rate would flag legitimate quotes at the other end of the band) -------
def test_satoshi_range_endpoints_and_midpoint_clean():
    assert validate_draft_prices("Satoshi — AED 3,000/hr") == []
    assert validate_draft_prices("Satoshi — AED 2,000/hr for residents") == []
    assert validate_draft_prices("Satoshi — special at AED 2,400/hr") == []


def test_satoshi_below_floor_flagged_with_range_label():
    m = validate_draft_prices("Satoshi — only AED 1,800/hr today")
    assert any("1,800" in x for x in m), m
    # the regen hint must hand the drafter the RANGE as ground truth
    assert any("2,000-3,000" in x for x in m), m


def test_satoshi_above_list_flagged():
    m = validate_draft_prices("Satoshi — AED 3,200/hr")
    assert any("3,200" in x for x in m), m


def test_satoshi_bare_name_matches_key():
    # drafts often write just "Satoshi" — the key must catch it without the
    # full "Sunseeker Satoshi 70" form.
    m = validate_draft_prices("the Satoshi is AED 1,200/hr")
    assert any("1,200" in x for x in m), m


# --- Phase-1 expansion (quote-routing experiment surface, 2026-06-11) --------
def test_phase1_push_boats_catalog_rates_clean():
    draft = ("Ferretti 780 — up to 20 guests\nAED 5,500/hr\n\n"
             "Haigan — up to 25 guests\nAED 4,500/hr\n\n"
             "Galeon 780 — AED 5,000/hr\n\n"
             "Zeta 100 — AED 7,000/hr\n\n"
             "Diana 50 — AED 1,100/hr")
    assert validate_draft_prices(draft) == []


def test_phase1_legacy_prices_flagged():
    # "(was X)" catalog prices are deprecated on purpose — they must flag.
    assert any("4,900" in x for x in
               validate_draft_prices("Zirve 72 — AED 4,900/hr")), "zirve legacy"
    assert any("1,100" in x for x in
               validate_draft_prices("Elise 50 — AED 1,100/hr")), "elise legacy"
    assert any("5,000" in x for x in
               validate_draft_prices("Eclipse 90 — AED 5,000/hr")), "eclipse legacy"


def test_returning_fleet_princess_60_and_bella():
    # operator 2026-06-11: both returning to the fleet at 1,400/hr.
    assert validate_draft_prices("Princess 60 — AED 1,400/hr") == []
    assert validate_draft_prices("Bella — AED 1,400/hr") == []
    m = validate_draft_prices("Princess 60 — AED 1,800/hr")
    assert any("1,800" in x and "Princess 60" in x for x in m), m


def test_tatti_spelling_variants():
    # classifier corpus spells it "Tattii"; catalog says "Tatti 110" — the
    # substring key must catch both.
    assert validate_draft_prices("Tatti 110 — AED 9,000/hr") == []
    m = validate_draft_prices("Tattii — AED 8,500/hr")
    assert any("8,500" in x for x in m), m


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} price_validator tests passed")
