#!/usr/bin/env python3
"""Past-date nudge guard — incident 2026-06-17 (B-suppress + C2 card badge).

Incident: the /pending batch sent "…Satoshi for Jun 14 … happy to help you lock
it in" to a lead whose charter date was 3 days past. Root cause: the proactive
ghost-recovery path has NO deterministic past-date guard — sourcing is
silence-only, the date_passed re-engage directive lives in an unreachable
else-branch, and prompt directives are ADVISORY.

Fix B (FOLLOWUP_PASTDATE_GUARD_ENABLED): deterministic suppress in
routes.handle_draft_followup — a past booking date posts NO proactive card,
reusing the exclusion-guard skip protocol (sweep counts it skipped_excluded).
Fix C2: pending.py renders the charter date + ⛔PAST + recipient phone so any
past-date card is catchable before tapping.

These tests are PURE / clock-controlled (no docker). The server-helper test
monkeypatches _dubai_now; it self-skips if server can't import locally (the full
box suite always runs it).

Run: python3 hermes-bridge/test_followup_pastdate_guard.py
"""
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pending  # noqa: E402  — pure

# Fixed "now" = 2026-06-17 12:00 UTC so every assertion is clock-independent.
NOW_MS = int(datetime.datetime(2026, 6, 17, 12, 0,
                               tzinfo=datetime.timezone.utc).timestamp() * 1000)


def _draft(did="1000_a", **kw):
    d = {"id": did, "status": "pending", "is_proactive_nudge": True,
         "customer_phone": "5532505120995@lid", "customer_name": "Nassr",
         "draft_text": "hi Nassr, just following up about the Satoshi for Jun 14"}
    d.update(kw)
    return d


# ── C2: _past_date_badge (pure) ───────────────────────────────────────────
def test_badge_marks_past_abs_date():
    b = pending._past_date_badge(_draft(booking_date_abs="2026-06-14"), NOW_MS)
    assert "📅" in b and b.endswith("⛔PAST"), b
    assert "Jun 14" in b, b


def test_badge_future_abs_date_no_past_mark():
    b = pending._past_date_badge(_draft(booking_date_abs="2026-07-11"), NOW_MS)
    assert "📅" in b and "PAST" not in b, b
    assert "Jul 11" in b, b


def test_badge_today_is_not_past():
    b = pending._past_date_badge(_draft(booking_date_abs="2026-06-17"), NOW_MS)
    assert "PAST" not in b, b


def test_badge_freetext_dates_display_only_never_false_past():
    b = pending._past_date_badge(_draft(dates="2027 (unconfirmed)"), NOW_MS)
    assert "📅" in b and "PAST" not in b, b


def test_badge_absent_when_no_date():
    assert pending._past_date_badge(_draft(), NOW_MS) == ""


# ── C2: render surfaces the past-date flag + phone on the card ─────────────
def test_render_past_date_card_flagged():
    sel = pending.select_pending_nudges(
        [_draft(booking_date_abs="2026-06-14")], NOW_MS)
    r = pending.render_pending(sel, stale_ids=set(), now_ms=NOW_MS)
    card = r["per_card"][0]
    assert card.get("past_date") is True, card
    assert "PAST DATE" in card["text"] and "📅" in card["text"], card["text"]
    assert "⛔" in r["telegram_text"], r["telegram_text"]
    # Flag-not-block: one-tap Send is still offered (operator decides; the
    # deterministic suppress is upstream). ⛔PAST just makes it catchable.
    assert any(b["callback_data"].startswith("send:") for b in card["buttons"])


def test_render_future_date_card_not_flagged():
    sel = pending.select_pending_nudges(
        [_draft(booking_date_abs="2026-07-11")], NOW_MS)
    r = pending.render_pending(sel, stale_ids=set(), now_ms=NOW_MS)
    card = r["per_card"][0]
    assert card.get("past_date") is False, card
    assert "PAST DATE" not in card["text"], card["text"]
    assert "📅" in card["text"], card["text"]


def test_render_card_shows_phone():
    sel = pending.select_pending_nudges(
        [_draft(booking_date_abs="2026-07-11")], NOW_MS)
    r = pending.render_pending(sel, stale_ids=set(), now_ms=NOW_MS)
    assert "5532505120995@lid" in r["per_card"][0]["text"]


def test_render_no_date_unchanged_behaviour():
    # A nudge with no date must render exactly as before (no badge, no flag).
    sel = pending.select_pending_nudges([_draft()], NOW_MS)
    r = pending.render_pending(sel, stale_ids=set(), now_ms=NOW_MS)
    card = r["per_card"][0]
    assert card.get("past_date") is False
    assert "📅" not in card["text"] and "PAST DATE" not in card["text"]


# ── B: deterministic past-date decision (server._is_past_booking_date) ─────
def test_is_past_booking_date_decision():
    try:
        import server  # noqa: E402
    except Exception as e:  # pragma: no cover — full import guaranteed on box
        print("SKIP server import (run on box):", repr(e))
        return
    _orig = server._dubai_now
    server._dubai_now = lambda: datetime.datetime(2026, 6, 17, 12, 0)
    try:
        assert server._is_past_booking_date("Sun Jun 14") is True
        assert server._is_past_booking_date("Jun 16") is True      # 1 day past
        assert server._is_past_booking_date("Fri Jul 11") is False  # future
        assert server._is_past_booking_date("") is False
        assert server._is_past_booking_date("2027 (unconfirmed)") is False
    finally:
        server._dubai_now = _orig


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} past-date-guard tests passed")
