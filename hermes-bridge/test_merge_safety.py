#!/usr/bin/env python3
"""Task #11 — reconcile-cron merge-safety, two fixes (2026-06-02 PM, Opus):

(A) Stress-finding #3: `_merge_blocked` was name-EQUALITY only (`na != nb`) →
    it refused the MOST COMMON legit @lid/@c.us split (first-name vs full-name,
    transliteration/typo) → persistent split → duplicate draft cards. Loosen it
    to treat plausibly-same-person names as mergeable, while STILL blocking
    truly-different names (Antonio vs Qurbani — recycled-LID false merge).

(B) Stress-finding #4: a name-independent DURABLE pin so the hourly reconcile
    can't re-bury an un-merged lead the instant a name-refresh erases the name
    difference. `_do_not_merge_pinned` reads customer_label_history 'do_not_merge'
    rows for the pair.

Run: python3 hermes-bridge/test_merge_safety.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from labels import (  # noqa: E402
    _merge_blocked,
    _name_edit_distance,
    _names_likely_same_person,
    _do_not_merge_pinned,
)


# ---- (A) loosened _merge_blocked: NEW allow cases (the bug) ----
def test_first_name_vs_full_name_allows_merge():
    # The dominant real split: pushName 'Antonio' vs extracted 'Antonio Rossi'.
    assert _merge_blocked("Antonio", "Antonio Rossi") is False
    assert _merge_blocked("Antonio Rossi", "Antonio") is False
    assert _merge_blocked("sara", "Sara Khan") is False


def test_transliteration_typo_allows_merge():
    # Common in this customer base; same human, 1-char variant.
    assert _merge_blocked("Mohammad", "Mohammed") is False
    assert _merge_blocked("Sara", "Sarah") is False
    assert _merge_blocked("Jon Smith", "John Smith") is False


# ---- (A) preserved BLOCK cases (the safety net must hold) ----
def test_distinct_real_names_still_block():
    assert _merge_blocked("Zayn", "Antonio") is True
    assert _merge_blocked("Qurbani", "Antonio") is True
    assert _merge_blocked("John Smith", "Jane Doe") is True
    assert _merge_blocked("Tony", "Antonio") is True   # nickname: conservative block


# ---- (A) preserved ALLOW cases (existing behavior) ----
def test_existing_allow_cases_preserved():
    assert _merge_blocked("Antonio", "Antonio") is False
    assert _merge_blocked("  antonio ", "Antonio") is False
    assert _merge_blocked("", "Antonio") is False
    assert _merge_blocked("unknown", "Antonio") is False
    assert _merge_blocked("+971509767187", "Antonio") is False


# ---- _name_edit_distance helper ----
def test_name_edit_distance():
    assert _name_edit_distance("abc", "abc") == 0
    assert _name_edit_distance("abc", "abd") == 1          # substitution
    assert _name_edit_distance("abc", "abcd") == 1         # insertion
    assert _name_edit_distance("abcd", "abc") == 1         # deletion
    assert _name_edit_distance("mohammad", "mohammed") == 1
    assert _name_edit_distance("", "abc") == 3
    assert _name_edit_distance("antonio", "qurbani") >= 5  # truly different


def test_names_likely_same_person():
    assert _names_likely_same_person("Antonio", "Antonio Rossi") is True
    assert _names_likely_same_person("Mohammad", "Mohammed") is True
    assert _names_likely_same_person("Antonio", "Qurbani") is False
    assert _names_likely_same_person("", "Antonio") is False


# ---- (B) durable do-not-merge pin ----
LID = "274942918680787@lid"
CUS = "971509767187@c.us"
OTHER = "999@lid"


def test_pin_on_lid_referencing_cus_blocks():
    rows = [(LID, "do_not_merge", "recycled-LID name conflict vs " + CUS)]
    assert _do_not_merge_pinned(LID, CUS, rows) is True


def test_pin_on_cus_referencing_lid_blocks():
    rows = [(CUS, "do_not_merge", "do-not-merge-with:" + LID)]
    assert _do_not_merge_pinned(LID, CUS, rows) is True


def test_no_pin_rows_not_blocked():
    assert _do_not_merge_pinned(LID, CUS, []) is False


def test_pin_referencing_third_party_not_blocked():
    # A do_not_merge pin linking LID to a DIFFERENT cid must not block THIS pair.
    rows = [(LID, "do_not_merge", "do-not-merge-with:" + OTHER)]
    assert _do_not_merge_pinned(LID, CUS, rows) is False


def test_non_pin_signal_ignored():
    rows = [(LID, "auto:identity_merge", "WAHA-resolved dup -> " + CUS)]
    assert _do_not_merge_pinned(LID, CUS, rows) is False


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print("PASS", fn.__name__)
        except AssertionError as e:
            failed += 1
            print("FAIL", fn.__name__, "-", e or "assert")
        except Exception as e:
            failed += 1
            print("ERROR", fn.__name__, "-", repr(e))
    print(f"{len(fns)-failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
