#!/usr/bin/env python3
"""reengage_quote.py — proactive follow-up CARD builder (pure).

ARCHITECTURE (2026-06-07): the proactive quote follow-up reuses Hermes' NATIVE
engine end-to-end — targeting (server.scan_followup_eligibility: we-replied-last
+ customer silent 24h-14d, 48h cooldown, FOLLOWUP_CAP, terminal/paid-label +
paylink suppression), intelligent drafting with FULL WAHA context and the
verified Chris-Voss ghost-recovery phrasing (routes.handle_draft_followup +
server.GHOST_RECOVERY_PHRASES, e.g. "Have you given up on booking a private
yacht?"), the Layer-3 exclusion guard, and the cooldown bump
(upsert_conversation_state "nudge_drafted"). routes.handle_followup_sweep
orchestrates: scan -> draft -> _draft_save -> self-post this card via _tg_post.

This module holds ONLY the pure card composition so it is unit-testable without
the bridge running. The Send/Edit/Regen/Skip callbacks carry the persisted
draft_id, so tapping ✅ Send drives the existing n8n -> /queue claim-send -> WAHA
path and delivers from the BUSINESS WhatsApp number (approval-first: nothing
reaches the customer until the operator taps).

Stdlib only.
"""


def build_followup_card(header, name, customer_id, label, silence_hours,
                        badge, draft_text, draft_id, phone=None):
    """Build the operator Telegram card (text + reply_markup) for a one-tap
    proactive follow-up. Pure; None-safe.

    The recipient (name + resolved phone) is shown so the operator verifies the
    target before a one-tap send — mirrors the /assist nudge card. `phone`
    should be the waha-resolved number; falls back to the cid's digits when
    unresolved. reply_markup mirrors the /assist nudge card so the existing
    callback router handles it."""
    who = name.strip() if (name and name.strip()) else \
        ("WhatsApp lead ••" + (customer_id or "").split("@")[0][-4:])
    recipient = (phone or "").strip() or (customer_id or "").split("@")[0]
    hrs = ""
    try:
        if silence_hours is not None:
            hrs = f", silent {float(silence_hours):.0f}h"
    except (TypeError, ValueError):
        hrs = ""
    lines = [header, f"*{who}*  ·  {recipient}  ({label}{hrs})"]
    if badge:
        lines.append(badge)
    lines.append("⚠️ _confirm the recipient above before sending_")
    lines.append("")
    lines.append(draft_text or "")
    text = "\n".join(lines)
    reply_markup = {"inline_keyboard": [
        [{"text": "✅ Send", "callback_data": "send:" + draft_id},
         {"text": "✏️ Edit", "callback_data": "edit:" + draft_id}],
        [{"text": "🔁 Regen", "callback_data": "regen:" + draft_id},
         {"text": "❌ Skip", "callback_data": "skip:" + draft_id}],
    ]}
    return {"text": text, "reply_markup": reply_markup}
