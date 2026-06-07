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
from intake import intake_gaps  # noqa: E402

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


def test_old_chat_not_flagged():
    assert intake_gaps([_chat("971000@c.us", 30.0)], set(), NOW) == []


def test_ms_timestamp_normalized():
    c = {"id": "971000@c.us", "conversationTimestamp": (NOW - 3600) * 1000,
         "lastMessage": {"fromMe": False, "body": "hi"}}
    assert len(intake_gaps([c], set(), NOW)) == 1


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} intake_gap tests passed")
