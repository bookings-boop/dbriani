#!/usr/bin/env python3
"""Profile-lookup gating patch — tests for the tightened inject gate (Option B)
and the AED price-strip. Written test-first (RED→GREEN), stdlib only, same
convention/harness as test_profile_lookup.py.

Contract under test:
  Change 1 (gate): profile_block injects ONLY when ALL of:
      returning=True  AND  real name (not blank/"Unknown")  AND
      profile_prefs contains >=1 CONCRETE preference
      (yacht NAME[booked] / dietary / crew / beverage / occasion).
    Contact-only or all-"not mentioned" prefs => zero concrete prefs => no inject.
  Change 2 (strip): price amounts adjacent to an AED/aed/dirham token are removed
    from the prefs before rendering; yacht model numbers (Sunseeker 88, SX88,
    Princess 74, Anna 50) and dates are NEVER touched.

Run (no pytest on the box): python3 hermes-bridge/test_profile_gate.py
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import profile_lookup as P  # noqa: E402
import hermes_exclusion_guards as EG  # noqa: E402

# real-index cases (the operator's explicit expectations)
INCLUDE = {"Ramona": "7064648227", "Nitin Jain": "971585388732",
           "Michele Oliveira": "19176761666", "Armin": "41793808495",
           "Giuseppe Luongo": "971586513671"}
MOHAMED = "421918588588"   # substantive AND carries "75,000 AED" -> must strip
ROBERT = "447769326783"    # prawn food-poisoning INCIDENT (event-phrased, no allergy term) -> must inject
EXCLUDE = {"Alexis": "244922818585", "Shamsheer Vayalil": "971555875497"}

_AED_RESIDUE = re.compile(r"(?i)(?:aed|dhs?|dirhams?|درهم)\s*\.?\s*\d|\d[\d,]*\s*(?:aed|dhs?|dirhams?|درهم)")


def _on():  os.environ["PROFILE_LOOKUP_ENABLED"] = "1"
def _off(): os.environ.pop("PROFILE_LOOKUP_ENABLED", None)


# ---------- Change 1a: real-name guard ----------
def test_real_name_accepts_normal():
    assert P._is_real_name("Aamir") is True
    assert P._is_real_name("Giuseppe Luongo") is True


def test_real_name_rejects_unknown_and_blank():
    assert P._is_real_name("Unknown") is False
    assert P._is_real_name("unknown") is False
    assert P._is_real_name("") is False
    assert P._is_real_name("   ") is False
    assert P._is_real_name(None) is False


# ---------- Change 1b: concrete-preference detector ----------
def test_concrete_true_for_substantive():
    assert P._has_concrete_pref("**Preferred yacht(s):**\n- Booked: Bliss 55\n"
                                "**Dietary notes:** VEGAN — strong flag") is True
    assert P._has_concrete_pref("Côtes de Provence Rosé x4 bottles; Prosecco x2") is True
    assert P._has_concrete_pref("the crew and the chef were very professional") is True
    assert P._has_concrete_pref("booking for his wife's birthday celebration") is True


def test_concrete_false_for_empty_and_contact_only():
    assert P._has_concrete_pref("") is False
    assert P._has_concrete_pref(None) is False
    # contact-only / all "not stated" (Robert-shaped)
    assert P._has_concrete_pref(
        "| Full name | Robert | | Email | r@x.com |\n"
        "| Preferred yacht | Not stated |\n| Preferred crew | Not stated |\n"
        "| Dietary notes | None stated |") is False


def test_concrete_false_for_yacht_interest_without_booking():
    # Shamsheer-shaped: names yachts "of interest" but never booked one
    assert P._has_concrete_pref(
        "Preferred yacht size: 85ft and above\n"
        "Yachts of interest: Ace 142, Azimut 88, Galeon 80\n"
        "Preferred crew | None named") is False


def test_concrete_false_for_unnamed_yacht_and_negated_crew():
    # Alexis-shaped: unnamed yacht, crew "None named or requested", BBQ interest
    assert P._has_concrete_pref(
        "Preferred yacht(s): The unnamed luxury yacht shown via Matterport 360 tour. "
        "Only one booking in record; no stated repeat preference yet.\n"
        "Preferred crew: None named or requested.\n"
        "Dietary notes: None stated. Interest in grilling/BBQ.") is False


# ---------- Change 2: AED price-strip ----------
def test_strip_removes_aed_amounts():
    assert "75,000" not in P._strip_aed("fixed price 75,000 AED for 5 hours")
    assert "4,000" not in P._strip_aed("deposit of AED 4,000 paid")
    assert "2800" not in P._strip_aed("quoted 2800 AED")
    assert _AED_RESIDUE.search(P._strip_aed("rate AED 7,500 / 75,000 AED total")) is None


def test_strip_preserves_yacht_numbers_and_dates():
    s = P._strip_aed("booked Sunseeker 88; also SX88 and Princess 74 and Anna 50 on 2024-04-24")
    for keep in ("Sunseeker 88", "SX88", "Princess 74", "Anna 50", "2024-04-24"):
        assert keep in s, (keep, s)


# ---------- integration on the REAL index ----------
def test_gate_includes_substantive_real_cases():
    _on()
    try:
        for name, cid in INCLUDE.items():
            b = P.profile_block(cid + "@c.us")
            assert b and "RETURNING" in b, (name, b[:120])
            assert _AED_RESIDUE.search(b) is None, (name, "AED amount leaked")
    finally:
        _off()


def test_gate_excludes_thin_real_cases():
    _on()
    try:
        for name, cid in EXCLUDE.items():
            assert P.profile_block(cid + "@c.us") == "", (name, "should be empty")
    finally:
        _off()


def test_mohamed_stripped_but_profile_survives():
    _on()
    try:
        b = P.profile_block(MOHAMED + "@c.us")
        assert b, "Mohamed should still inject (substantive)"
        assert "75,000" not in b and _AED_RESIDUE.search(b) is None, b[:300]
        assert "Royal" in b, "rest of profile must survive the strip"
    finally:
        _off()


# ---------- Part 1: food-safety incident detection (broadened dietary) ----------
def test_foodsafety_incident_counts_as_concrete():
    rob = P.lookup(ROBERT + "@c.us")
    assert P._has_concrete_pref(rob.get("profile_prefs") or "") is True, "Robert food-safety note"
    # event-phrased incidents (no clinical allergy keyword) must count
    assert P._has_concrete_pref("One guest had confirmed food poisoning from a prawn") is True
    assert P._has_concrete_pref("she had a bad reaction to shellfish last time") is True
    assert P._has_concrete_pref("client got sick from the lamb on the last charter") is True
    assert P._has_concrete_pref("he cannot eat dairy") is True
    assert P._has_concrete_pref("please avoid all nuts for this group") is True
    # casual / positive mentions must NOT match
    assert P._has_concrete_pref("we ate prawns and loved them") is False
    assert P._has_concrete_pref("the group enjoyed the seafood platter") is False
    assert P._has_concrete_pref("just got home and got my credit card") is False


def test_robert_injects_with_foodsafety_note_intact():
    _on()
    try:
        b = P.profile_block(ROBERT + "@c.us")
        assert b and "RETURNING" in b, "Robert should now inject"
        assert "food poisoning" in b.lower(), b[-400:]
    finally:
        _off()


# ---------- Part 1: suppress conflicting body name lines ----------
RAMONA = "7064648227"      # header 'Ramona' vs body 'Melanie Kirkwood Ruiz' -> conflict, suppress body line
GIUSEPPE = "971586513671"  # no name line -> must be untouched
NITIN = "971585388732"     # body 'Nitin Jain' == header -> untouched
JORGEN = "971566961627"    # header 'Jörgen Larsson' vs body 'Joergen' -> transliteration, KEEP
MIHAILS = "37120036580"    # header 'Mihails Grebenuks' vs body 'Michael' -> anglicization, KEEP


def _prefs(cid):
    return P.lookup(cid + "@c.us").get("profile_prefs") or ""


def test_drop_name_noop_when_no_name_line():
    g = _prefs(GIUSEPPE)
    assert P._drop_conflicting_name(g, "Giuseppe Luongo") == g  # byte-identical


def test_drop_name_noop_when_name_matches():
    n = _prefs(NITIN)
    assert P._drop_conflicting_name(n, "Nitin Jain") == n       # byte-identical


def test_drop_name_keeps_variants_and_transliterations():
    # ö->oe and anglicized form are the SAME person — must NOT be suppressed
    j = _prefs(JORGEN)
    assert P._drop_conflicting_name(j, "Jörgen Larsson") == j
    m = _prefs(MIHAILS)
    assert P._drop_conflicting_name(m, "Mihails Grebenuks") == m


def test_drop_name_removes_only_conflicting_line():
    r = _prefs(RAMONA)
    out = P._drop_conflicting_name(r, "Ramona")
    assert "Melanie Kirkwood Ruiz" not in out          # conflicting body name gone
    assert len(out) < len(r)                            # something was removed
    assert "Royalty 136" in out and "NUT ALLERGY" in out  # rest of prefs intact


def test_ramona_block_greets_header_not_body():
    _on()
    try:
        b = P.profile_block(RAMONA + "@c.us")
        assert "Name: Ramona" in b
        assert "Melanie Kirkwood Ruiz" not in b
        assert "Royalty 136" in b                       # rest still rendered
    finally:
        _off()


def test_giuseppe_block_unchanged_by_suppression():
    _on()
    try:
        b = P.profile_block(GIUSEPPE + "@c.us")
        assert "Giuseppe" in b and "SX88" in b and ("Prosecco" in b or "Provence" in b)
    finally:
        _off()


# ---------- exclusion-list gate (respect the same block-list as the nudge path) ----------
BLOCKED_PHONE = "971509767187"   # real internal_staff entry — must never be profiled


def test_exclusion_gate_blocks_listed_phone():
    assert EG.is_excluded(BLOCKED_PHONE + "@c.us") is True       # precondition: on the list
    _on()
    ix = P._index()
    had = BLOCKED_PHONE in ix
    backup = ix.get(BLOCKED_PHONE)
    ix[BLOCKED_PHONE] = {"returning": True, "name": "Staff Member", "country": "AE",
                         "n_booking_blocks": 2,
                         "profile_prefs": "**Dietary notes:** VEGAN\n**Preferred crew:** same crew"}
    try:
        assert P.profile_block(BLOCKED_PHONE + "@c.us") == "", "excluded phone must inject nothing"
    finally:
        if had:
            ix[BLOCKED_PHONE] = backup
        else:
            ix.pop(BLOCKED_PHONE, None)
        _off()


def test_exclusion_gate_allows_unlisted_phone():
    unlisted = "990000000007"
    assert EG.is_excluded(unlisted + "@c.us") is False           # control: not on the list
    _on()
    ix = P._index()
    ix[unlisted] = {"returning": True, "name": "Real Customer", "country": "AE",
                    "n_booking_blocks": 1, "profile_prefs": "**Dietary notes:** VEGAN"}
    try:
        assert P.profile_block(unlisted + "@c.us") != "", "non-excluded substantive must still inject"
    finally:
        ix.pop(unlisted, None)
        _off()


def _run():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    p = f = 0
    for t in tests:
        try:
            t(); p += 1; print(f"  PASS {t.__name__}")
        except Exception as e:  # noqa: BLE001
            f += 1; print(f"  FAIL {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{p} passed, {f} failed, {len(tests)} total")
    return 1 if f else 0


if __name__ == "__main__":
    sys.exit(_run())
