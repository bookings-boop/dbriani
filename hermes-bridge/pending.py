#!/usr/bin/env python3
"""pending.py — pure selection + render for the /pending approval-throughput
digest (Lever ②, Tier 0, 2026-06-16).

PURE (no Redis/PG) so it unit-tests with no live infra. routes.handle_pending
does the Redis reads (SMEMBERS drafts:active + GET each, read-time GHOST SKIP —
never SREM) and the /draft-freshness calls, then hands the drafts in here.

Every send the digest offers reuses the EXISTING per-card callbacks
(send:/edit:/skip:) -> n8n -> /draft-freshness -> /queue claim-send. This module
NEVER sends; it only selects + renders. See test_pending_digest.py.
"""
import datetime


def _past_date_badge(d, now_ms):
    """Pure. Short charter-date badge for a card, e.g. ' · 📅 Jun 14 ⛔PAST'.

    Incident 2026-06-17: a stale charter date ("Satoshi for Jun 14", 3 days
    past) was buried in the 60-char snippet so the operator couldn't catch it
    before tapping Send. This surfaces the date explicitly and marks it ⛔PAST
    when the resolved booking_date_abs (YYYY-MM-DD) is >= 1 day before today.
    Today is derived from now_ms in Asia/Dubai (UTC+4) to match
    server._is_past_booking_date. Falls back to the free-text `dates` (display
    only, no past-calc — never a false ⛔PAST). None-safe; returns '' when no
    date is known. Backstop only — FOLLOWUP_PASTDATE_GUARD_ENABLED is the
    primary, deterministic stop."""
    abs_raw = str(d.get("booking_date_abs") or "").strip()
    bd = None
    if abs_raw:
        try:
            bd = datetime.date.fromisoformat(abs_raw[:10])
        except ValueError:
            bd = None
    if bd is not None:
        today = (datetime.datetime.utcfromtimestamp(now_ms / 1000.0)
                 + datetime.timedelta(hours=4)).date()  # Asia/Dubai
        disp = bd.strftime("%b ") + str(bd.day)  # "Jun 14" (no zero-pad)
        return " · 📅 " + disp + (" ⛔PAST" if (today - bd).days >= 1 else "")
    free = str(d.get("dates") or "").strip().replace("\n", " ")
    if free:
        return " · 📅 " + free[:24]
    return ""


def _created_ms(d):
    """Creation epoch-ms parsed from the draft id ("<ms>_<rand>", the format
    server._draft_save mints). Always present; the soonest-to-expire sort key."""
    try:
        return int(str(d.get("id", "")).split("_")[0])
    except (ValueError, AttributeError, IndexError):
        return 0


def select_pending_nudges(items, now_ms):
    """Pure. items = drafts (dict) or None (TTL-expired ghost / junk). Returns
    the renderable pending PROACTIVE nudges, oldest-first (soonest to hit the 24h
    TTL = highest approval priority). Skips ghosts, non-proactive, non-pending."""
    out = []
    for d in items:
        if not isinstance(d, dict):
            continue
        if not d.get("is_proactive_nudge"):
            continue
        if str(d.get("status") or "").strip().lower() != "pending":
            continue
        out.append(d)
    out.sort(key=_created_ms)
    return out


def _fmt_left(ms_left):
    """Human time-to-24h-expiry for a card."""
    h = ms_left / 3600000.0
    if h <= 0:
        return "expiring"
    if h < 1:
        return str(int(h * 60)) + "m left"
    return str(round(h, 1)) + "h left"


def _first_line(d, limit=60):
    msgs = d.get("messages")
    if isinstance(msgs, list):
        for m in msgs:
            if isinstance(m, str) and m.strip():
                return m.strip().replace("\n", " ")[:limit]
    return str(d.get("draft_text") or "").strip().replace("\n", " ")[:limit]


def _card_buttons(did, stale):
    """Reuse the EXISTING per-card callbacks. A stale (replied-since) card gets
    NO ✅ Send — it must not be one-tap sendable as a cold nudge."""
    btns = []
    if not stale:
        btns.append({"text": "✅ Send", "callback_data": "send:" + did})
    btns.append({"text": "✏️ Edit", "callback_data": "edit:" + did})
    btns.append({"text": "❌ Skip", "callback_data": "skip:" + did})
    return btns


def render_pending(selected, stale_ids, now_ms, ttl_ms=86400000):
    """Pure render of the pending-nudge digest.

    selected   : oldest-first pending nudges (from select_pending_nudges).
    stale_ids  : ids flagged stale by /draft-freshness (customer OR operator
                 messaged since the draft) -> shown ⚠️, no one-tap Send.
    Returns {count, fresh_ids, stale_ids, per_card, inline_keyboards,
    telegram_text}. fresh_ids/stale_ids partition the pile (Tier-1 batch +
    lifecycle instrumentation read these)."""
    stale = set(stale_ids or [])
    if not selected:
        return {"count": 0, "fresh_ids": [], "stale_ids": [],
                "per_card": [], "inline_keyboards": [],
                "telegram_text": "✅ No proactive nudges pending approval."}
    fresh_ids, st_ids, per_card, kbs, lines = [], [], [], [], []
    for d in selected:
        did = d.get("id", "")
        is_stale = did in stale
        name = (d.get("customer_name") or d.get("customer_phone") or "?")
        phone = str(d.get("customer_phone") or "")
        left = _fmt_left((_created_ms(d) + ttl_ms) - now_ms)
        date_badge = _past_date_badge(d, now_ms)
        is_past = date_badge.endswith("⛔PAST")
        flag = "⚠️ replied-since · " if is_stale else ""
        pmark = "⛔ *PAST DATE* · " if is_past else ""
        # Surface recipient phone + the charter date (⛔PAST when the resolved
        # booking date is before today) so a stale-date nudge is catchable
        # before tapping — the 60-char snippet alone hid it (incident 2026-06-17).
        text = (pmark + flag + "*" + str(name) + "*"
                + ((" · " + phone) if phone else "")
                + " · ⏳" + left + date_badge + "\n" + _first_line(d))
        btns = _card_buttons(did, is_stale)
        per_card.append({"id": did, "text": text, "buttons": btns,
                         "stale": is_stale, "past_date": is_past})
        kbs.append([btns])
        lines.append((("⛔ " if is_past else "") + ("⚠️ " if is_stale else "• "))
                     + str(name) + " · ⏳" + left + date_badge)
        (st_ids if is_stale else fresh_ids).append(did)
    header = ("🔔 *" + str(len(selected)) + "* proactive nudge(s) awaiting "
              "approval — " + str(len(fresh_ids)) + " sendable"
              + ((", " + str(len(st_ids)) + " ⚠️ replied-since (no one-tap)")
                 if st_ids else "") + ".")
    return {"count": len(selected), "fresh_ids": fresh_ids,
            "stale_ids": st_ids, "per_card": per_card,
            "inline_keyboards": kbs,
            "telegram_text": header + "\n\n" + "\n".join(lines)}
