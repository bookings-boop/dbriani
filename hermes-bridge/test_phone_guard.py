#!/usr/bin/env python3
"""4-A (2026-06-09): lid_phone_map phone-disagreement guard for the identity
reconcile. WhatsApp recycles/re-points @lid handles, so the live WAHA lid->@c.us
resolution can point an @lid at a DIFFERENT person's number than the durable
lid_phone_map recorded. When the map's clean (conflict_phone IS NULL) phone for
the @lid DISAGREES with the @c.us phone WAHA resolved, BLOCK the merge + pin.

ADD-ONLY: the guard can only ever BLOCK a merge, never enable one. It fails OPEN
(a missing/blank phone never blocks) so it defers to the existing name-conflict
guard rather than wedging legitimate folds.

This locks the pure predicate. Run: python3 hermes-bridge/test_phone_guard.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from labels import _phone_disagreement_block as B  # noqa: E402


def test_differing_phones_block_the_over_merge_pairs():
    # The two reverted over-merges — distinct phones MUST block a re-merge.
    assert B("971507177877", "971555633317") is True   # Mohammed (…7877 vs …3317)
    assert B("971503367561", "971547366777") is True   # Émilie  (…3361 vs …6777)


def test_same_phone_allows():
    # Youssra @lid + @c.us = one person, same number -> never block.
    assert B("971544236263", "971544236263") is False


def test_blank_or_missing_fails_open():
    assert B("", "971555633317") is False
    assert B("971507177877", None) is False
    assert B(None, None) is False


def test_digit_normalization():
    # Formatting differences are NOT a conflict (digits-only compare)...
    assert B("+971 50 717 7877", "971507177877") is False
    # ...but a genuinely different number still blocks despite formatting.
    assert B("+971507177877", "971555633317") is True


def test_guard_is_wired_into_reconcile():
    import routes
    assert ("_phone_disagreement_block"
            in routes.handle_reconcile_identities.__code__.co_names), (
        "4-A guard not referenced in handle_reconcile_identities")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
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
