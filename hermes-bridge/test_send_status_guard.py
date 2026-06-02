#!/usr/bin/env python3
"""2026-06-02 double-send: claim-send never read draft.status, so tapping ✅ Send
on an already-sent card (or a stale/superseded sibling card for the same
customer — e.g. an unmerged @lid vs @c.us split) re-sent the customer. This
locks the pure predicate that decides when a send must be REFUSED because the
draft is already terminal. The claim-send caller fails OPEN (allows the send)
when status can't be read, so a transient lookup error never blocks a real first
send — only an unambiguous terminal status blocks.

Run: python3 hermes-bridge/test_send_status_guard.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from labels import _send_blocked_by_status  # noqa: E402


def test_sent_is_blocked():
    assert _send_blocked_by_status("sent") is True


def test_superseded_is_blocked():
    assert _send_blocked_by_status("superseded") is True


def test_disregarded_is_blocked():
    assert _send_blocked_by_status("disregarded") is True


def test_pending_sends():
    assert _send_blocked_by_status("pending") is False


def test_awaiting_states_send():
    # awaiting_edit / awaiting_amount are mid-flow, still sendable.
    assert _send_blocked_by_status("awaiting_edit") is False
    assert _send_blocked_by_status("awaiting_amount") is False


def test_unknown_status_fails_open():
    # Fail-open: an empty/None/garbage status must NOT block (a transient
    # lookup miss can never wedge a legitimate first send).
    assert _send_blocked_by_status("") is False
    assert _send_blocked_by_status(None) is False
    assert _send_blocked_by_status("weird") is False


def test_case_insensitive():
    assert _send_blocked_by_status("SENT") is True
    assert _send_blocked_by_status("  Superseded  ") is True


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} send-status-guard tests passed")
