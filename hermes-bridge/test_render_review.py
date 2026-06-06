#!/usr/bin/env python3
"""Integration tests for render_review passed-date visibility (R1, 2026-06-01).

Marimuthu incident: a COLD passed-date lead the operator had re-engaged was
INVISIBLE because (a) score_lead damped it to a negative score, and the
`score < 0` guard shunted it to the hidden pause-tail BEFORE the NO-ACTIVE-SALE
routing ran; and (b) even if routed, the 55-lead bucket (cap 12) buried it.

Fix: the pause-tail guard exempts passed-date leads, and the NOT_A_CUSTOMER
bucket floats re-engaged graceful-exits (passed date + a recent operator
reply) to the top so they're visible.

Run: python3 hermes-bridge/test_render_review.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from review import render_review  # noqa: E402


def _row(cid, label="COLD", **ov):
    base = {
        "customer_id": cid, "name": "", "label": label,
        "label_updated_at": "", "label_locked_until": "",
        "label_locked_active": False, "message_count": 2, "yachts": "",
        "dates": "", "party_size": "", "last_customer_message_at": "",
        "last_customer_message_at_seconds": 1000,
        "last_operator_reply_at_seconds": None,
        "last_review_seen_at_seconds": None,
        "last_nudge_drafted_at_seconds": None,
        "last_payment_link_at_seconds": None,
        "last_payment_promised_at_seconds": None,
        "last_booking_intent_at": "", "last_rejection_at_seconds": None,
        "last_rejection_kind": "", "recent_notes": "",
        "importance_score": 0, "importance_reasoning": "",
        "suggested_action": "", "importance_analyzed_at_seconds": None,
        "last_analysis_signal": "", "last_analyzed_at_seconds": None,
    }
    base.update(ov)
    return base


def _shown_cids(res):
    return [m.get("customer_id") for m in (res.get("per_lead_messages") or [])]


def test_reengaged_passed_date_visible_despite_negative_score_and_overflow():
    # 20 score-0 supplier/spam fillers fill the NO ACTIVE SALE bucket (cap 12),
    # ordered before Marimuthu (caller sorts score-desc) so without the sort he
    # overflows. Marimuthu: COLD, passed date, we re-engaged (recent reply),
    # score -100 (damped).
    fillers = [(0, _row(f"sup{i}@lid", name=f"Sup{i}",
                        last_analysis_signal="cold_decay")) for i in range(20)]
    mari = _row("mari@lid", name="Marimuthu", dates="Jan 1 2020",
                last_customer_message_at_seconds=900000,
                last_operator_reply_at_seconds=3600)
    scored = fillers + [(-100, mari)]
    res = render_review(scored, {}, "on-demand")
    assert "mari@lid" in _shown_cids(res), \
        "re-engaged passed-date lead must be visible in NO ACTIVE SALE"


def test_future_date_negative_score_still_hidden():
    # regression: a non-passed-date lead with score < 0 stays in the hidden
    # pause-tail (unchanged behaviour).
    fut = _row("fut@lid", dates="Dec 31 2099",
               last_customer_message_at_seconds=900000,
               last_operator_reply_at_seconds=3600)
    res = render_review([(-100, fut)], {}, "on-demand")
    assert "fut@lid" not in _shown_cids(res)


def test_main_list_ranks_by_expected_value_not_raw_rate():
    # Operator 2026-06-06: rank by POTENTIAL VALUE (importance-weighted), not raw
    # hourly rate. A high-intent lead on a cheaper yacht must outrank a low-intent
    # lead on a pricier yacht (Milenski/Emma were buried under low-intent whales).
    # Bliss 55 imp88 -> EV 1792 (rate 1400); Satoshi 70 imp10 -> EV 1500 (rate 3000).
    hi = _row("hi@lid", label="HOT", name="HighIntent",
              yachts="Bliss 55", importance_score=88)
    lo = _row("lo@lid", label="HOT", name="LowIntent",
              yachts="Sunseeker Satoshi 70", importance_score=10)
    res = render_review([(0, hi), (0, lo)], {}, "on-demand")
    cids = _shown_cids(res)
    assert cids.index("hi@lid") < cids.index("lo@lid"), cids


def test_owed_reply_still_floats_above_higher_value():
    # owed-reply (revenue at risk) must still beat a higher-value non-owed lead.
    owed = _row("owed@lid", label="HOT", name="Owed", yachts="Bliss 55",
                importance_score=40, last_customer_message_at_seconds=600,
                last_operator_reply_at_seconds=7200)   # customer after us -> owe
    rich = _row("rich@lid", label="HOT", name="Rich", yachts="Sunseeker Satoshi 70",
                importance_score=95,                    # higher EV but NOT owed
                last_customer_message_at_seconds=7200,  # we replied after them
                last_operator_reply_at_seconds=600)
    res = render_review([(0, owed), (0, rich)], {}, "on-demand")
    cids = _shown_cids(res)
    assert cids.index("owed@lid") < cids.index("rich@lid"), cids


def test_review_hot_uncap_shows_every_lead_in_tier():
    # Operator 2026-06-06: '+N more — /review hot to see all' was BROKEN — the
    # tier-filtered view ALSO capped at 10, so buried high-intent leads
    # (Milenski/Emma) were unreachable in ANY /review view. With uncap=True the
    # filtered tier shows every lead.
    leads = [(50 - i, _row(f"h{i}@lid", label="HOT", name=f"H{i}",
                           importance_score=50 - i)) for i in range(14)]
    capped = _shown_cids(render_review(leads, {}, "on-demand"))
    uncapped = _shown_cids(render_review(leads, {}, "on-demand", uncap=True))
    assert len(capped) <= 10, len(capped)
    assert len(uncapped) == 14, len(uncapped)


def test_unknown_label_is_surfaced_not_dropped():
    # CATCH-ALL (2026-06-06): a lead whose label has no section (e.g. a new or
    # typo'd label like SUPPLIER_B2B, or LOST before it was wired) must be
    # surfaced for triage, never silently dropped. Operator hit leads vanishing
    # from /review entirely.
    row = _row("unk@lid", label="SUPPLIER_B2B", name="Mystery",
               importance_score=50)
    res = render_review([(50, row)], {}, "on-demand")
    assert "unk@lid" in _shown_cids(res), \
        "unknown-label lead must not vanish from /review"


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} render_review tests passed")
