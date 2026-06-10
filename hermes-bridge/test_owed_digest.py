#!/usr/bin/env python3
"""F3 (2026-06-10): the "🔴 NEEDS YOUR REPLY" owed-digest at the top of /review —
a flat, priority-ranked index of EVERY lead we owe a reply to, independent of the
(possibly mis-routed / unanalyzed) tier it landed in.

Locks the PURE build_owed_digest(scored, cap) behaviour:
  • population == the owe-reply sweep (reuses review._owe_reply_candidates, so
    terminal/paused labels are excluded), and
  • ordering == _digest_sort_key: B2B/role-anchored leads sink to the BOTTOM
    regardless of score; then expected booking value (review._expected_value)
    desc; then composite score desc.
Render-only, pure, None/junk-safe.

Run: python3 hermes-bridge/test_owed_digest.py
"""
import sys

import review


def _row(**kw):
    """A v_lead_summary-shaped row. Defaults make the lead OWED (customer spoke
    more recently than our last outbound) and a real customer (benign reasoning,
    so _has_role_anchor is False). Override via kwargs."""
    r = {
        "name": kw.get("name", "Test Lead"),
        "customer_id": kw.get("customer_id", "100@c.us"),
        "label": kw.get("label", "HOT"),
        "importance_score": kw.get("importance_score", 50),
        "importance_reasoning": kw.get(
            "importance_reasoning", "asking about availability and pricing"),
        "yachts": kw.get("yachts", ""),
        "dates": kw.get("dates", ""),
        "message_count": kw.get("message_count", 5),
        # seconds-AGO (smaller = more recent). cs < orep => we owe a reply.
        "last_customer_message_at_seconds": kw.get("cs", 3600),
        "last_operator_reply_at_seconds": kw.get("orep", 7200),
        "last_nudge_drafted_at_seconds": kw.get("nudge", None),
    }
    return r


def _bullets(digest):
    """The ' • ' lead lines of a digest block, in order."""
    return [ln for ln in digest.splitlines() if ln.strip().startswith("•")]


def test_b2b_role_sinks_to_bottom_regardless_of_score():
    # Vendor has a HUGE score + high importance, real customer is a weak COLD —
    # the vendor must STILL sort below the real customer (role is a leading key).
    vendor = _row(name="VendorCo", label="WARM", importance_score=90,
                  importance_reasoning="they are a yacht supplier reselling to us")
    real = _row(name="RealCustomer", label="COLD", importance_score=10)
    digest = review.build_owed_digest([(9999, vendor), (100, real)])
    assert "RealCustomer" in digest and "VendorCo" in digest, \
        "vendor should be shown (at the bottom), not excluded"
    assert digest.index("RealCustomer") < digest.index("VendorCo"), \
        "B2B/role lead must sink below the real customer regardless of score"


def test_high_value_hot_above_low_value_owed():
    # Same composite score on both, so expected-value must break the tie:
    # a HOT high-importance lead outranks a COLD low-importance one.
    hot = _row(name="HotWhale", label="HOT", importance_score=80)
    cold = _row(name="ColdLow", label="COLD", importance_score=15)
    digest = review.build_owed_digest([(500, cold), (500, hot)])
    assert digest.index("HotWhale") < digest.index("ColdLow"), \
        "higher expected-value lead must sort above the low-value one"


def test_empty_set_returns_no_block():
    assert review.build_owed_digest([]) == ""
    assert review.build_owed_digest(None) == ""
    # A lead we already answered (we replied more recently) is NOT owed.
    answered = _row(cs=7200, orep=3600)
    assert review.build_owed_digest([(100, answered)]) == ""


def test_cap_and_overflow():
    scored = [(100 + i, _row(name=f"Lead{i:02d}", customer_id=f"{i}@c.us"))
              for i in range(22)]
    digest = review.build_owed_digest(scored, cap=20)
    assert digest.splitlines()[0].count("22 owed") == 1, \
        "title must report the FULL owed count (22), not the capped count"
    assert len(_bullets(digest)) == 20, "must show exactly cap=20 bullet lines"
    assert "_+2 more owed_" in digest, "overflow line must report the remainder"


def test_none_and_junk_safe():
    # Malformed scored entries must never raise — the digest is fail-open.
    junk = [
        None,
        ("x",),                       # too short
        (1, 2, 3),                    # row is not a dict
        (5, None),                    # None row
        (5, {}),                      # empty row (not owed)
        (5, {"label": None, "last_customer_message_at_seconds": None}),
        # owed, but name is None and no waited field is well-formed:
        (7, {"name": None, "label": "HOT",
             "last_customer_message_at_seconds": 1,
             "last_operator_reply_at_seconds": 5}),
    ]
    out = review.build_owed_digest(junk)
    assert isinstance(out, str), "must return a string, never raise"


def test_worked_example_maria_top_vendor_bottom():
    maria = _row(name="Maria Fernanda", label="HOT", importance_score=70,
                 cs=66 * 3600, orep=None)
    dean = _row(name="Dean", label="HOT", importance_score=50, cs=56 * 3600,
                orep=None)
    vendor = _row(name="SeaLux supply", label="WARM", importance_score=95,
                  importance_reasoning="charter operator / supplier",
                  cs=10 * 3600, orep=None)
    digest = review.build_owed_digest([(700, dean), (5000, vendor), (800, maria)])
    i_maria = digest.index("Maria Fernanda")
    i_dean = digest.index("Dean")
    i_vendor = digest.index("SeaLux supply")
    assert i_maria < i_dean < i_vendor, \
        "Maria (top value) first, Dean next, vendor (role) last"
    # Spot-check the locked line shape on Maria.
    assert "Maria Fernanda — HOT · waited 66h" in digest


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
