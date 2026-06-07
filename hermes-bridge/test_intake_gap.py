#!/usr/bin/env python3
"""Unit tests for intake_gaps() — the silent-intake-drop detector.

A lead can reach WAHA but never get ingested into customer_facts if the
WAHA->n8n webhook misses it (outage / IP-drift window). 2026-06-06: Ayaan
Nadeem sat invisible for ~4 days. intake_gaps() cross-references WAHA chats
against the set of ingested customer_ids and returns recent unanswered inbound
1:1 chats we have NO record of — so a daily cron can alert the operator to
/refresh them. Pure (no DB / no network) so it is unit-tested.

Contract intake_gaps(chats, cf_ids, now_ts, max_age_days=7):
  - flag a 1:1 chat whose cid is NOT in cf_ids, last message is inbound
    (not fromMe), and age <= max_age_days.
  - never flag: already-ingested cids, fromMe-last (we replied), groups /
    status / broadcast, or chats older than max_age_days.
  - tolerate ms or s timestamps.

Plain-assert style. Run: python3 hermes-bridge/test_intake_gap.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from intake import canon_phone, intake_gaps  # noqa: E402

NOW = 1_700_000_000


def _chat(cid, ago_days, from_me=False, name="Lead", body="hi"):
    return {"id": cid,
            "conversationTimestamp": NOW - int(ago_days * 86400),
            "lastMessage": {"fromMe": from_me, "body": body,
                            "_data": {"notifyName": name}}}


def test_new_recent_inbound_is_flagged():
    g = intake_gaps([_chat("971000@c.us", 1.0, name="New Lead")], set(), NOW)
    assert len(g) == 1 and g[0]["cid"] == "971000@c.us", g
    assert g[0]["name"] == "New Lead", g


def test_already_ingested_not_flagged():
    assert intake_gaps([_chat("971000@c.us", 1.0)], {"971000@c.us"}, NOW) == []


def test_fromme_last_not_flagged():
    assert intake_gaps([_chat("971000@c.us", 1.0, from_me=True)], set(), NOW) == []


def test_group_chat_excluded():
    assert intake_gaps([_chat("12-34@g.us", 1.0)], set(), NOW) == []
    assert intake_gaps([_chat("status@broadcast", 1.0)], set(), NOW) == []


def test_junk_nonphone_cid_excluded():
    # 2026-06-07: the never-miss net auto-ingested a synthetic 'cid=0' chat as a
    # junk lead. A real lead cid is a phone/lid (all digits, plausible length);
    # '0' / all-zeros / non-numeric must never be flagged or auto-ingested.
    assert intake_gaps([_chat("0@c.us", 1.0)], set(), NOW) == []
    assert intake_gaps([_chat("0", 1.0)], set(), NOW) == []
    assert intake_gaps([_chat("000000@c.us", 1.0)], set(), NOW) == []
    # a real phone / lid is still flagged
    assert len(intake_gaps([_chat("971544855029@c.us", 1.0)], set(), NOW)) == 1
    assert len(intake_gaps([_chat("137813169274972@lid", 1.0)], set(), NOW)) == 1


def test_old_chat_not_flagged():
    assert intake_gaps([_chat("971000@c.us", 30.0)], set(), NOW) == []


def test_ms_timestamp_normalized():
    c = {"id": "971000@c.us", "conversationTimestamp": (NOW - 3600) * 1000,
         "lastMessage": {"fromMe": False, "body": "hi"}}
    assert len(intake_gaps([c], set(), NOW)) == 1


# ── NEVER-MISS hardening (2026-06-07) ──────────────────────────────────

def test_no_age_cap_flags_old_never_ingested():
    """max_age_days=None — a never-ingested inbound lead must NEVER age out of
    detection (the old 7-day cap let a buried drop go permanently invisible)."""
    g = intake_gaps([_chat("971000@c.us", 90.0, name="Old Lead")], set(), NOW,
                    max_age_days=None)
    assert len(g) == 1 and g[0]["cid"] == "971000@c.us", g
    assert g[0]["age_days"] >= 89, g  # age still reported on the card


def test_age_cap_still_applies_when_set():
    """A numeric cap still windows (legacy alert path) — old chat excluded."""
    assert intake_gaps([_chat("971000@c.us", 30.0)], set(), NOW,
                       max_age_days=7) == []


def test_canon_folds_lid_to_cus_no_false_gap():
    """An @lid chat already ingested under its @c.us identity is NOT a gap once
    a lid-aware canon resolver folds the two identity domains together."""
    lid_map = {"63977933553823@lid": "971568241103"}
    canon = lambda c: canon_phone(c, lid_map)  # noqa: E731
    chats = [_chat("63977933553823@lid", 1.0, name="Antonio")]
    cf = {"971568241103@c.us"}
    assert intake_gaps(chats, cf, NOW, max_age_days=None, canon=canon) == []


def test_canon_unresolved_lid_still_flagged():
    """A truly-dropped @lid lead with no lid-map entry stays DISTINCT and is
    still flagged (fail-toward-flagging — never silently miss a lead)."""
    canon = lambda c: canon_phone(c, {})  # noqa: E731
    chats = [_chat("99999999999999@lid", 1.0, name="Ghost")]
    g = intake_gaps(chats, {"971568241103@c.us"}, NOW,
                    max_age_days=None, canon=canon)
    assert len(g) == 1 and g[0]["cid"] == "99999999999999@lid", g


def test_canon_phone_normalizes_uae_variants():
    """Identity normalize: 0xxxxxxxxx / 5xxxxxxxx / 00-prefixed / +-spaced all
    canonicalise to the same 971 key so cf-set membership matches regardless of
    how the number was stored."""
    assert canon_phone("971568241103@c.us") == "971568241103"
    assert canon_phone("0568241103@c.us") == "971568241103"
    assert canon_phone("568241103@c.us") == "971568241103"
    assert canon_phone("00971568241103@c.us") == "971568241103"
    assert canon_phone("+971 56 824 1103@c.us") == "971568241103"
    # lid resolves through the map to the same key
    assert canon_phone("63977933553823@lid",
                       {"63977933553823@lid": "971568241103"}) == "971568241103"


def test_canon_phone_none_safe():
    assert canon_phone(None) == ""
    assert canon_phone("") == ""


def test_cf_phone_variant_match_suppresses_gap():
    """cf stored as 0-prefixed / lid; the same chat under @c.us is suppressed by
    the default (phone-only) canon — no false gap from format drift."""
    chats = [_chat("971568241103@c.us", 1.0)]
    assert intake_gaps(chats, {"0568241103@c.us"}, NOW,
                       max_age_days=None) == []


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} intake_gap tests passed")
