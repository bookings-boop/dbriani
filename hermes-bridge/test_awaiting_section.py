#!/usr/bin/env python3
"""Unit tests for _awaiting_section_for() — the /review section router.

Decides whether a (non-paused, valid-label) lead belongs in:
  - 'AWAITING_REPLY'  : we owe a reply to a GENUINE active prospect
  - 'NOT_A_CUSTOMER'  : analyzer score 0 (supplier/spam/done) OR a FRESH
                        terminal verdict on an owed-reply lead (#2 fix)
  - ''                : render in its own label section (default)

#2 fix (2026-06-01): an owed-reply lead the analyzer has FRESHLY judged
terminal (confirmed_terminal / date_passed) must leave AWAITING for the
💤 NO ACTIVE SALE bucket — UNLESS the customer messaged after that verdict
(then they're re-engaging and stay owed). cold_decay is dormant, NOT terminal.

Plain-assert style. Run: python3 hermes-bridge/test_awaiting_section.py

Seconds fields are AGE-in-seconds: smaller = more recent.
  owe  = customer msg more recent than our last reply/nudge  (_cs < _out)
  fresh terminal = analysis at least as recent as the customer msg (_anz <= _cs)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from review import _awaiting_section_for, _no_sale_reason  # noqa: E402


def _r(label="HOT", **overrides):
    base = {
        "label": label,
        "importance_score": 50,
        "last_customer_message_at_seconds": None,
        "last_operator_reply_at_seconds": None,
        "last_nudge_drafted_at_seconds": None,
        "last_analysis_signal": "",
        "last_analyzed_at_seconds": None,
    }
    base.update(overrides)
    return base


# --- the #2 fix: fresh terminal verdict on an owed lead -> NO ACTIVE SALE ---
def test_fresh_date_passed_owe_goes_to_not_a_customer():
    row = _r("HOT", importance_score=57,
             last_customer_message_at_seconds=3600,   # customer msg 1h ago
             last_operator_reply_at_seconds=7200,      # we replied 2h ago -> owe
             last_analysis_signal="date_passed",
             last_analyzed_at_seconds=1800)            # analysed 0.5h ago -> fresh
    assert _awaiting_section_for(row, 800) == "NOT_A_CUSTOMER"


def test_fresh_confirmed_terminal_owe_goes_to_not_a_customer():
    row = _r("HOT", importance_score=57,
             last_customer_message_at_seconds=3600,
             last_operator_reply_at_seconds=7200,
             last_analysis_signal="confirmed_terminal",
             last_analyzed_at_seconds=1800)
    assert _awaiting_section_for(row, 800) == "NOT_A_CUSTOMER"


# --- freshness guard: customer messaged AFTER the terminal verdict ----------
def test_stale_terminal_verdict_stays_in_awaiting():
    # analysed 2h ago, customer messaged 0.5h ago -> they re-engaged -> AWAITING
    row = _r("HOT", importance_score=57,
             last_customer_message_at_seconds=1800,
             last_operator_reply_at_seconds=7200,
             last_analysis_signal="confirmed_terminal",
             last_analyzed_at_seconds=7200)
    assert _awaiting_section_for(row, 800) == ""  # operator 2026-06-02: owed leads render in their OWN tier (with a 🔴 needs-your-reply badge), no separate AWAITING_REPLY section


def test_terminal_signal_without_analysis_time_stays_in_awaiting():
    # no last_analyzed_at -> cannot trust the verdict -> AWAITING
    row = _r("HOT", importance_score=57,
             last_customer_message_at_seconds=3600,
             last_operator_reply_at_seconds=7200,
             last_analysis_signal="date_passed",
             last_analyzed_at_seconds=None)
    assert _awaiting_section_for(row, 800) == ""  # operator 2026-06-02: owed leads render in their OWN tier (with a 🔴 needs-your-reply badge), no separate AWAITING_REPLY section


# --- unchanged behaviour (regression guards) --------------------------------
def test_active_owed_lead_goes_to_awaiting():
    row = _r("HOT", importance_score=80,
             last_customer_message_at_seconds=3600,
             last_operator_reply_at_seconds=7200,
             last_analysis_signal="sticky_hot",
             last_analyzed_at_seconds=1800)
    assert _awaiting_section_for(row, 800) == ""  # operator 2026-06-02: owed leads render in their OWN tier (with a 🔴 needs-your-reply badge), no separate AWAITING_REPLY section


def test_cold_decay_is_not_terminal_owe_stays_awaiting():
    row = _r("COLD", importance_score=40,
             last_customer_message_at_seconds=3600,
             last_operator_reply_at_seconds=7200,
             last_analysis_signal="cold_decay",
             last_analyzed_at_seconds=1800)
    assert _awaiting_section_for(row, 200) == ""  # operator 2026-06-02: owed leads render in their OWN tier (with a 🔴 needs-your-reply badge), no separate AWAITING_REPLY section


def test_score_zero_owe_goes_to_not_a_customer():
    row = _r("NEW", importance_score=0,
             last_customer_message_at_seconds=3600,
             last_operator_reply_at_seconds=7200,
             last_analysis_signal="new_window",
             last_analyzed_at_seconds=1800)
    assert _awaiting_section_for(row, 300) == "NOT_A_CUSTOMER"


def test_score_zero_not_owe_still_not_a_customer():
    # score-0 always bucketed, owe or not (matches pre-fix behaviour)
    row = _r("COLD", importance_score=0,
             last_customer_message_at_seconds=7200,
             last_operator_reply_at_seconds=3600)
    assert _awaiting_section_for(row, 200) == "NOT_A_CUSTOMER"


def test_confirmed_owed_goes_to_awaiting():
    # Antonio 2026-06-06: a PAID/CONFIRMED customer with an unanswered new
    # message (active post-booking thread — viewing logistics) must surface in
    # AWAITING_REPLY, not sit buried at the bottom CONFIRMED tier.
    row = _r("CONFIRMED", importance_score=90,
             last_customer_message_at_seconds=3600,   # customer msg 1h ago
             last_operator_reply_at_seconds=7200,      # we replied 2h ago -> owe
             last_analysis_signal="confirmed_terminal",
             last_analyzed_at_seconds=1800)
    assert _awaiting_section_for(row, 5000) == "AWAITING_REPLY"


def test_confirmed_not_owed_stays_in_own_section():
    # A CONFIRMED booking we've already replied to (no owed reply) stays in the
    # CONFIRMED tier — unchanged (a post-booking "thanks" we already handled).
    row = _r("CONFIRMED", importance_score=90,
             last_customer_message_at_seconds=7200,    # customer msg 2h ago
             last_operator_reply_at_seconds=3600,       # we replied 1h ago -> not owe
             last_analysis_signal="confirmed_terminal",
             last_analyzed_at_seconds=1800)
    assert _awaiting_section_for(row, 5000) == ""


def test_confirmed_passed_event_owed_leaves_awaiting():
    # Emilie 2026-06-06: a CONFIRMED booking whose event date has PASSED is a
    # WON, completed deal. A stale pre-trip message must NOT resurrect it into
    # the top AWAITING section (she was #1 in /review off a May-27 message for a
    # May-28 event, score 0). Passed-event CONFIRMED stays in its CONFIRMED tier.
    row = _r("CONFIRMED", importance_score=0, dates="Jan 1 2020",
             last_customer_message_at_seconds=3600,    # customer msg 1h ago
             last_operator_reply_at_seconds=7200,       # we replied 2h ago -> owe
             last_analysis_signal="confirmed_terminal",
             last_analyzed_at_seconds=1800)
    assert _awaiting_section_for(row, 5000) == ""


def test_confirmed_future_event_owed_stays_awaiting():
    # Antonio regression: an UPCOMING CONFIRMED booking with an unanswered
    # message still surfaces in AWAITING_REPLY (active post-booking thread).
    row = _r("CONFIRMED", importance_score=90, dates="Dec 31 2099",
             last_customer_message_at_seconds=3600,
             last_operator_reply_at_seconds=7200,
             last_analysis_signal="confirmed_terminal",
             last_analyzed_at_seconds=1800)
    assert _awaiting_section_for(row, 5000) == "AWAITING_REPLY"


# --- H1: stale RELATIVE-date CONFIRMED must not sit at the top of /review -----
# Émilie 2026-06-07: dates='tomorrow 4–7 PM' frozen ~10 days ago never parses to
# a calendar date, so event_passed() can't see it and the old guard let her win
# the AWAITING_REPLY top slot at score 0. The broadened guard (stale-relative OR
# score-0 'no open sale') keeps her in the CONFIRMED tier, while a FRESH relative
# date (Antonio's 'tomorrow', set an hour ago) stays in AWAITING.
def test_confirmed_stale_relative_date_leaves_awaiting():
    row = _r("CONFIRMED", importance_score=0, dates="tomorrow 4–7 PM",
             last_customer_message_at_seconds=10 * 86400,  # frozen ~10d ago
             last_operator_reply_at_seconds=11 * 86400,     # owed
             last_analysis_signal="confirmed_terminal",
             last_analyzed_at_seconds=5 * 86400)
    assert _awaiting_section_for(row, 5000) == ""


def test_confirmed_fresh_relative_date_stays_awaiting():
    # Antonio: fresh relative date (<1 day old), high score, owed → AWAITING.
    row = _r("CONFIRMED", importance_score=90, dates="tomorrow",
             last_customer_message_at_seconds=3600,     # fresh (<1 day)
             last_operator_reply_at_seconds=7200,        # owed
             last_analysis_signal="confirmed_terminal",
             last_analyzed_at_seconds=1800)
    assert _awaiting_section_for(row, 5000) == "AWAITING_REPLY"


def test_confirmed_score_zero_leaves_awaiting():
    # 'no open sale' (score 0) CONFIRMED with NO concrete future date → not a
    # live AWAITING lead even when owed (won/closed booking). A concrete FUTURE
    # date still overrides this (see test_confirmed_future_event_owed_stays_awaiting).
    row = _r("CONFIRMED", importance_score=0, dates="",
             last_customer_message_at_seconds=3600,
             last_operator_reply_at_seconds=7200,
             last_analysis_signal="confirmed_terminal",
             last_analyzed_at_seconds=1800)
    assert _awaiting_section_for(row, 5000) == ""


# --- H4: LOST is a VISIBLE terminal label, never "not a customer" -------------
def test_lost_label_renders_own_section():
    # A LOST lead (legit prospect we didn't win — price/competitor/timing/ghost)
    # has importance_score 0 and usually a passed date, which previously routed
    # it into the 💤 NO ACTIVE SALE / "not a customer" bucket (for vendor/spam).
    # It must render in its OWN 💔 LOST section → return '' (own label section).
    row = _r("LOST", importance_score=0, dates="Jan 1 2020",
             last_customer_message_at_seconds=900000,
             last_operator_reply_at_seconds=3600)
    assert _awaiting_section_for(row, 1) == ""


def test_scam_label_renders_own_section():
    # H5: a SCAM lead (crypto/fraud) is terminal-VISIBLE — it renders in its OWN
    # 🚫 SCAM section, never diverted into the 💤 NO ACTIVE SALE / "not a
    # customer" bucket (its score-0 + passed date would otherwise route it
    # there). Returning '' falls through to sections['SCAM'] in render_review.
    row = _r("SCAM", importance_score=0, dates="Jan 1 2020",
             last_customer_message_at_seconds=900000,
             last_operator_reply_at_seconds=None)
    assert _awaiting_section_for(row, 1) == ""


def test_unreliable_zero_score_not_routed_to_no_active_sale():
    # H8: a lead with REAL history (157 msgs) that the analyzer scored 0 while
    # claiming "first contact / no prior messages" ran on incomplete history —
    # the 0-score must NOT bury it in NO ACTIVE SALE. The unreliable verdict is
    # not trusted for routing → render in its own (HOT) tier ('').
    row = _r("HOT", importance_score=0, message_count=157,
             importance_reasoning="First contact, no prior messages — date passed",
             last_customer_message_at_seconds=1000,
             last_operator_reply_at_seconds=None)
    assert _awaiting_section_for(row, 800) == ""


def test_reliable_zero_score_still_routes_to_no_active_sale():
    # Guard: a genuine score-0 supplier (reasoning does NOT claim empty history)
    # is STILL routed to NO ACTIVE SALE — the unreliable carve-out is narrow.
    row = _r("NEW", importance_score=0, message_count=20,
             importance_reasoning="Alma is a supplier (Fruitful Day), no booking intent",
             last_customer_message_at_seconds=1000,
             last_operator_reply_at_seconds=None)
    assert _awaiting_section_for(row, 300) == "NOT_A_CUSTOMER"


def test_non_owe_terminal_lead_is_untouched():
    # not owed (we replied more recently than the customer) + terminal -> stays
    # in its own tier; the fix only diverts OWED leads (scope guard).
    row = _r("HOT", importance_score=57,
             last_customer_message_at_seconds=7200,
             last_operator_reply_at_seconds=3600,
             last_analysis_signal="date_passed",
             last_analyzed_at_seconds=1800)
    assert _awaiting_section_for(row, 800) == ""


# --- 💤 NO ACTIVE SALE bucket reason line -----------------------------------
def test_no_sale_reason_date_passed():
    row = _r(last_analysis_signal="date_passed",
             importance_reasoning="HOT — wants AK Royalty next month")
    # terminal signal wins over stale positive reasoning
    assert _no_sale_reason(row) == "booking date has already passed"


def test_no_sale_reason_confirmed_terminal():
    row = _r(last_analysis_signal="confirmed_terminal", importance_reasoning="")
    assert _no_sale_reason(row) == "booking completed — no open sale"


def test_no_sale_reason_falls_back_to_analyzer_reasoning():
    # a score-0 supplier (non-terminal signal) keeps its analyzer reasoning
    row = _r(last_analysis_signal="new_window",
             importance_reasoning="Alma is a supplier (Fruitful Day), no booking intent")
    assert _no_sale_reason(row) == "Alma is a supplier (Fruitful Day), no booking intent"


def test_no_sale_reason_default_when_no_reasoning():
    row = _r(last_analysis_signal="", importance_reasoning="")
    assert _no_sale_reason(row) == "analyzer scored 0 — no open sale"


def test_no_sale_reason_date_passed_reengaged():
    # #5 graceful close: we SENT a re-engage (replied more recently than the
    # customer's last message) → show the re-engaged / gracefully-closed state.
    row = _r(last_analysis_signal="date_passed",
             last_customer_message_at_seconds=900000,   # customer ~10d silent
             last_operator_reply_at_seconds=3600)       # we replied 1h ago
    r = _no_sale_reason(row).lower()
    assert "re-engaged" in r and "graceful" in r


def test_no_sale_reason_date_passed_not_yet_reengaged():
    # date passed but we haven't reached out yet → plain "date passed"
    row = _r(last_analysis_signal="date_passed",
             last_customer_message_at_seconds=900000,
             last_operator_reply_at_seconds=None)
    assert _no_sale_reason(row) == "booking date has already passed"


# --- parse-based passed-date routing (signal-independent, 2026-06-01) --------
# The analyzer signal goes stale (sticky_hot/sticky_cold) so passed-date leads
# vanish into the COLD overflow. Route off the PARSED booking date instead.
def test_route_passed_date_not_owe_to_lost():
    # COLD passed-date lead we've already replied to (graceful exit sent) →
    # 💔 LOST (a real prospect we didn't win), NOT "not a customer", despite the
    # stale sticky_hot signal + nonzero score (operator 2026-06-08: P2-4).
    row = _r("COLD", importance_score=52, dates="Jan 1 2020",
             last_customer_message_at_seconds=900000,
             last_operator_reply_at_seconds=3600,
             last_analysis_signal="sticky_hot")
    assert _awaiting_section_for(row, 800) == "LOST"


def test_route_passed_date_but_owed_stays_awaiting():
    # passed date BUT the customer just messaged (active) → still AWAITING
    row = _r("HOT", importance_score=60, dates="Jan 1 2020",
             last_customer_message_at_seconds=1800,
             last_operator_reply_at_seconds=7200,
             last_analysis_signal="sticky_hot")
    assert _awaiting_section_for(row, 800) == ""  # operator 2026-06-02: owed leads render in their OWN tier (with a 🔴 needs-your-reply badge), no separate AWAITING_REPLY section


def test_route_future_date_unaffected():
    # future date, not owed → render in its own tier (unchanged)
    row = _r("HOT", importance_score=60, dates="Dec 31 2099",
             last_customer_message_at_seconds=7200,
             last_operator_reply_at_seconds=1800,
             last_analysis_signal="sticky_hot")
    assert _awaiting_section_for(row, 800) == ""


def test_no_sale_reason_parsed_passed_date_reengaged():
    # parsed past date + we replied → graceful-exit reason even with no
    # date_passed signal (the Marimuthu case)
    row = _r("COLD", dates="Jan 1 2020",
             last_customer_message_at_seconds=900000,
             last_operator_reply_at_seconds=3600,
             last_analysis_signal="sticky_hot")
    assert "graceful" in _no_sale_reason(row).lower()


# --- P2-4 (operator 2026-06-08): a real lead whose booking DATE PASSED is a
# LOST prospect, NOT "not a customer" (vendor/spam). The passed-date route must
# file it in the 💔 LOST section. The no-date supplier score-0 case
# (test_reliable_zero_score_still_routes) is UNAFFECTED — it has no booking date,
# so it stays in NOT_A_CUSTOMER. The discriminator is a real, passed booking date.
def test_passed_date_not_owe_routes_to_lost():
    # John Winter / Abdulla: booking date passed, graceful exit sent (we replied
    # last -> not owed). A genuine prospect we didn't win -> 💔 LOST, never
    # "not a customer". (Was wrongly routed to NOT_A_CUSTOMER.)
    row = _r("COLD", importance_score=30, dates="Jan 1 2020",
             last_customer_message_at_seconds=900000,   # customer ~10d silent
             last_operator_reply_at_seconds=3600)        # we replied 1h ago -> not owe
    assert _awaiting_section_for(row, 100) == "LOST"


def test_no_date_supplier_score_zero_still_not_a_customer():
    # Guard: a genuine no-booking-date supplier (score 0, no dates) must STILL
    # route to NOT_A_CUSTOMER — the passed-date -> LOST change must not catch it.
    row = _r("NEW", importance_score=0, dates="",
             last_customer_message_at_seconds=1000,
             last_operator_reply_at_seconds=None,
             importance_reasoning="Alma is a supplier (Fruitful Day), no booking intent")
    assert _awaiting_section_for(row, 300) == "NOT_A_CUSTOMER"


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} _awaiting_section_for tests passed")
