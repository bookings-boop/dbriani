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


def test_confirmed_owed_shows_paid_detail_in_awaiting():
    # Antonio 2026-06-06: a CONFIRMED + owed booking surfaced in AWAITING_REPLY
    # must STILL show the booked & paid detail (status, yacht, date, amount) —
    # not the bare generic 'Hermes: N/100' line. The detail was gated on the
    # SECTION (label_key) instead of the lead's real label.
    row = _row("ant@lid", label="CONFIRMED", name="Antonio",
               booked_yacht="Bliss 55", dates="Dec 31 2099", party_size="12 guests",
               paid_amount="AED 3534.3", importance_score=88,
               last_customer_message_at_seconds=600,     # customer after us -> owed
               last_operator_reply_at_seconds=7200)
    res = render_review([(5000, row)], {}, "on-demand")
    text = "\n".join(m.get("text", "") for m in (res.get("per_lead_messages") or []))
    assert "AWAITING_REPLY" in text, text          # routed to AWAITING (owed)
    assert "booked & paid" in text, text           # but shows CONFIRMED detail
    assert "AED 3534.3" in text, text              # and the paid amount


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


def test_stale_relative_confirmed_renders_won_not_logistics():
    # H1 Émilie 2026-06-07: a CONFIRMED lead with a STALE relative date
    # ('tomorrow 4–7 PM' frozen ~10d ago) + score 0 must render as a won /
    # reconfirm-date booking, NOT the active-sale 'confirm logistics or upsell'
    # framing, and must NOT be pulled into the AWAITING_REPLY section.
    emi = _row("emi@lid", label="CONFIRMED", name="Emilie",
               booked_yacht="Bliss 55", dates="tomorrow 4–7 PM",
               importance_score=0,
               last_customer_message_at_seconds=10 * 86400,
               last_operator_reply_at_seconds=11 * 86400)
    res = render_review([(5000, emi)], {}, "on-demand")
    msgs = res.get("per_lead_messages") or []
    card = [m for m in msgs if m.get("customer_id") == "emi@lid"]
    assert card, "Émilie must still be visible (CONFIRMED tier)"
    text = card[0]["text"]
    assert "AWAITING_REPLY" not in text, text          # not the top section
    assert "won" in text.lower(), text                 # won / reconfirm framing
    assert "confirm logistics" not in text, text        # no active-sale guidance


def test_engaged_hot_lead_surfaces_above_cap():
    # H2 Zayn 2026-06-07: an engaged, near-ready HOT lead (43 messages, a
    # concrete future date, importance 48) on a MID-rate yacht was buried at
    # ~rank 15 of 21 — below REVIEW_CAP_HOT=10 — by low-engagement 'whale' leads
    # on pricey yachts (EV ranks raw rate above engagement). Engagement/
    # importance must co-weight so he surfaces in the DEFAULT /review.
    whales = [(800, _row(f"whale{i}@lid", label="HOT", name=f"Whale{i}",
                         yachts="Mila 141",            # AED 18,000/hr
                         importance_score=35,           # low intent
                         message_count=4))              # low engagement
              for i in range(20)]
    zayn = _row("zayn@lid", label="HOT", name="Zayn",
                yachts="Azimut 62",                     # mid rate (1,500)
                importance_score=48, message_count=43,
                dates="Dec 31 2099")                    # concrete future date
    res = render_review(whales + [(800, zayn)], {}, "on-demand")
    assert "zayn@lid" in _shown_cids(res), \
        "engaged near-ready HOT lead must surface in default /review, not overflow"


def test_lost_renders_in_lost_section_not_no_active_sale():
    # H4: a LOST lead renders in the 💔 LOST section, not the 💤 NO ACTIVE SALE
    # ("not a customer") bucket which is for vendor/spam non-customers.
    lost = _row("lost@lid", label="LOST", name="Yogi", dates="Jan 1 2020",
                importance_score=0,
                importance_reasoning="found a better price and booked elsewhere",
                last_customer_message_at_seconds=900000,
                last_operator_reply_at_seconds=3600)
    res = render_review([(1, lost)], {}, "on-demand")
    msgs = res.get("per_lead_messages") or []
    card = [m for m in msgs if m.get("customer_id") == "lost@lid"]
    assert card, "LOST lead must be visible"
    assert "*LOST*" in card[0]["text"], card[0]["text"]
    assert "NOT_A_CUSTOMER" not in card[0]["text"], card[0]["text"]


def test_we_replied_last_shows_awaiting_not_needs_reply():
    # H7: when the OPERATOR replied last (our reply more recent → smaller
    # seconds-ago), the badge must read '📨 awaiting reply', never '🔴 needs
    # your reply'. seconds-ago: smaller = more recent.
    row = _row("repl@lid", label="HOT", name="WeReplied", importance_score=50,
               last_customer_message_at_seconds=7200,   # customer 2h ago
               last_operator_reply_at_seconds=600)        # we replied 10m ago
    res = render_review([(800, row)], {}, "on-demand")
    text = (res.get("per_lead_messages") or [{}])[0].get("text", "")
    assert "awaiting reply" in text, text
    assert "needs your reply" not in text, text


def test_customer_messaged_last_shows_needs_reply():
    # H7 inverse: customer messaged last (more recent than our reply) → we owe
    # them → '🔴 needs your reply'.
    row = _row("need@lid", label="HOT", name="CustLast", importance_score=50,
               last_customer_message_at_seconds=600,     # customer 10m ago
               last_operator_reply_at_seconds=7200)        # we replied 2h ago
    res = render_review([(800, row)], {}, "on-demand")
    text = (res.get("per_lead_messages") or [{}])[0].get("text", "")
    assert "needs your reply" in text, text


def _card_for(res, cid):
    for m in (res.get("per_lead_messages") or []):
        if m.get("customer_id") == cid:
            return m
    return None


def test_scam_renders_own_section_info_only_keyboard():
    # H5: a SCAM lead (Mike) renders in its OWN 🚫 SCAM section (operator audit),
    # never the catch-all → HOT. Its card has an Info-only keyboard so the
    # operator can't accidentally draft / snooze / disregard-reply a scammer.
    scam = _row("scam@lid", label="SCAM", name="Mike", importance_score=0,
                importance_reasoning="Classic crypto USDT refund scam — fraud")
    res = render_review([(1, scam)], {}, "on-demand")
    assert "🚫 SCAM" in res["telegram_text"], res["telegram_text"]
    card = _card_for(res, "scam@lid")
    assert card, "SCAM lead must be visible"
    assert "*SCAM*" in card["text"], card["text"]
    btns = [b["text"] for kbrow in card["inline_keyboard"] for b in kbrow]
    assert any("Info" in b for b in btns), btns
    assert not any(("Draft" in b or "Snooze" in b or "Disregard" in b)
                   for b in btns), btns
    # No "render_review: unknown label" catch-all → SCAM is a known section.
    assert "🔥 SCAM" not in res["telegram_text"]


def test_scam_excluded_from_active_total():
    # H5: SCAM (like LOST / NOT_A_CUSTOMER) is terminal — excluded from the
    # "*N active*" header count so a scammer doesn't inflate the live pipeline.
    hot = _row("hot@lid", label="HOT", name="RealLead", importance_score=60,
               importance_reasoning="hot — push to book", message_count=10)
    scam = _row("scam@lid", label="SCAM", name="Mike", importance_score=0,
                importance_reasoning="crypto scam fraud")
    res = render_review([(800, hot), (1, scam)], {}, "on-demand")
    assert "1 active" in res["header_text"], res["header_text"]


def test_unreliable_zero_score_flagged_not_trusted():
    # H8: a HOT lead with REAL history (157 msgs) that the analyzer scored 0
    # while claiming "first contact, no prior messages" must be FLAGGED as
    # unreliable, not silently rendered as a dead 0/100 lead.
    row = _row("unrel@lid", label="HOT", name="Emilie", importance_score=0,
               message_count=157,
               importance_reasoning="First contact, no prior messages — date passed",
               last_customer_message_at_seconds=1000,
               last_operator_reply_at_seconds=None)
    res = render_review([(800, row)], {}, "on-demand")
    card = _card_for(res, "unrel@lid")
    assert card, "unreliable-analysis lead must stay visible"
    assert "analysis unreliable" in card["text"], card["text"]


def test_reliable_lead_not_flagged_unreliable():
    # Guard: a normally-analyzed lead (history present, reasoning doesn't claim
    # empty history) is NOT flagged unreliable.
    row = _row("ok@lid", label="HOT", name="Fine", importance_score=72,
               message_count=22,
               importance_reasoning="Customer chose Bliss 55, ready to book",
               last_customer_message_at_seconds=600,
               last_operator_reply_at_seconds=7200)
    res = render_review([(800, row)], {}, "on-demand")
    card = _card_for(res, "ok@lid")
    assert card and "analysis unreliable" not in card["text"], card["text"]


def test_confirmed_card_never_shows_likely_lost_or_unreliable():
    # H8 / FIX-GROUP 1: a won, PAID booking must NEVER surface the analyzer's
    # convertibility "likely LOST" verdict or an "analysis unreliable" flag on
    # its /review card — even when scored 0 off empty history. Shows booked&paid.
    row = _row("conf@lid", label="CONFIRMED", name="Antonio", importance_score=0,
               message_count=157, booked_yacht="Bliss 55", dates="Dec 31 2099",
               importance_reasoning="First contact, no prior messages — date passed",
               last_customer_message_at_seconds=7200,
               last_operator_reply_at_seconds=600)
    res = render_review([(5000, row)], {}, "on-demand")
    card = _card_for(res, "conf@lid")
    assert card, "CONFIRMED lead must be visible"
    assert "likely LOST" not in card["text"], card["text"]
    assert "analysis unreliable" not in card["text"], card["text"]
    assert "booked & paid" in card["text"], card["text"]


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} render_review tests passed")
