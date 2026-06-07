#!/usr/bin/env python3
"""fix-group 2 (a)+(d) — handle_label_eval reopen guards (2026-06-07).

(a) An OPERATOR-closed LOST/DISREGARDED lead must STAY closed — a stray inbound
    that matches the re-engage intent must NOT auto-reopen a deliberate operator
    close (the spammer-pitch-reopens-DISREGARDED hole). Only an auto/analyzer
    close (created_by='system'/cron) is auto-reopenable.
(d) A forced reopen is CAPPED at WARM unless the triggering signal is a hard,
    high-confidence one (payment_confirmed_chat / lets_do_it) — a single
    dampened money_mentioned must NOT force DISREGARDED→HOT.

These drive routes.handle_label_eval with server/_psql mocked (no DB).

Run: python3 hermes-bridge/test_reengage_reopen_guard.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import routes  # noqa: E402
import server  # noqa: E402


class _Harness:
    """Patch server.* + routes._psql for one handle_label_eval call."""

    def __init__(self, *, prev_label, compute_target, compute_sig,
                 close_created_by, confidence=0.2):
        self.prev_label = prev_label
        self.compute_target = compute_target
        self.compute_sig = compute_sig
        self.close_created_by = close_created_by
        self.confidence = confidence
        self.transitions = []   # (from, to, signal, created_by)
        self._orig = {}

    def _psql(self, sql, *a, **k):
        # The reopen block queries customer_label_history for the close source.
        if "customer_label_history" in sql and "to_label" in sql:
            return (self.close_created_by + "\n", None)
        return ("", None)

    def _get_row(self, cid):
        return {"customer_id": cid, "label": self.prev_label,
                "message_count": 7, "name": "Test", "yachts": "", "dates": ""}

    def _compute_label(self, msg, facts):
        return (self.compute_target, self.compute_sig, "ev")

    def _apply(self, cid, frm, to, signal=None, evidence=None,
               message_count=0, created_by="system"):
        self.transitions.append((frm, to, signal, created_by))
        return ("", None)

    def __enter__(self):
        self._orig = {
            "compute_label": server.compute_label,
            "compute_confidence": server.compute_confidence,
            "get_current_label_row": server.get_current_label_row,
            "apply_label_transition": server.apply_label_transition,
            "sameday_interrupt_check": server.sameday_interrupt_check,
            "_psql": routes._psql,
            "update_last_analysis": routes.update_last_analysis,
        }
        server.compute_label = self._compute_label
        server.compute_confidence = lambda sig: self.confidence
        server.get_current_label_row = self._get_row
        server.apply_label_transition = self._apply
        server.sameday_interrupt_check = lambda *a, **k: (False, None)
        routes._psql = self._psql
        routes.update_last_analysis = lambda *a, **k: None
        return self

    def __exit__(self, *exc):
        server.compute_label = self._orig["compute_label"]
        server.compute_confidence = self._orig["compute_confidence"]
        server.get_current_label_row = self._orig["get_current_label_row"]
        server.apply_label_transition = self._orig["apply_label_transition"]
        server.sameday_interrupt_check = self._orig["sameday_interrupt_check"]
        routes._psql = self._orig["_psql"]
        routes.update_last_analysis = self._orig["update_last_analysis"]
        return False


def _run(harness, msg="are you free Saturday for a charter?"):
    resp = {}

    def send(code, body):
        resp["code"] = code
        resp["body"] = body
    with harness:
        routes.handle_label_eval(
            {"customer_id": "111@c.us", "latest_message": msg}, send)
    return resp["body"], harness.transitions


# ---- (a) operator close stays closed ---------------------------------------
def test_operator_disregard_stays_closed():
    h = _Harness(prev_label="DISREGARDED", compute_target="WARM",
                 compute_sig="pricing_inquired",
                 close_created_by="operator:disregard_button")
    body, trans = _run(h)
    assert body["changed"] is False, body
    assert body["label"] == "DISREGARDED", body
    assert trans == [], "operator close must not write a reopen transition"


def test_operator_lost_stays_closed():
    h = _Harness(prev_label="LOST", compute_target="HOT",
                 compute_sig="money_mentioned",
                 close_created_by="operator")
    body, trans = _run(h, msg="how much for 10 pax to charter a yacht?")
    assert body["changed"] is False, body
    assert body["label"] == "LOST", body
    assert trans == []


# ---- (a) system/analyzer close still auto-reopens ---------------------------
def test_system_close_reopens_on_genuine_enquiry():
    h = _Harness(prev_label="LOST", compute_target="WARM",
                 compute_sig="pricing_inquired", close_created_by="system")
    body, trans = _run(h)
    assert body["changed"] is True, body
    assert body["label"] == "WARM", body
    assert len(trans) == 1 and trans[0][1] == "WARM"


def test_cron_dormancy_close_reopens():
    # a dormancy auto-close (system) is reopenable for a returning customer
    h = _Harness(prev_label="DISREGARDED", compute_target="WARM",
                 compute_sig="pricing_inquired", close_created_by="cron-dormancy")
    body, _ = _run(h)
    assert body["changed"] is True and body["label"] == "WARM", body


# ---- (d) dampening cap: money_mentioned HOT is capped to WARM ---------------
def test_forced_reopen_capped_at_warm():
    h = _Harness(prev_label="DISREGARDED", compute_target="HOT",
                 compute_sig="money_mentioned", close_created_by="system")
    body, trans = _run(h, msg="how much to charter a yacht this weekend?")
    assert body["changed"] is True, body
    assert body["label"] == "WARM", "money_mentioned reopen must cap at WARM"
    assert trans[0][1] == "WARM"


def test_hard_signal_reopens_to_full_target():
    # lets_do_it is a hard/high-confidence signal → not capped
    h = _Harness(prev_label="LOST", compute_target="HOT",
                 compute_sig="lets_do_it", close_created_by="system")
    body, _ = _run(h, msg="let's do it, book the yacht for Saturday")
    assert body["changed"] is True and body["label"] == "HOT", body


def test_payment_confirmed_reopens_to_confirmed():
    h = _Harness(prev_label="LOST", compute_target="CONFIRMED",
                 compute_sig="payment_confirmed_chat", close_created_by="system")
    body, _ = _run(h, msg="just paid the deposit for the charter")
    assert body["changed"] is True and body["label"] == "CONFIRMED", body


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print("PASS", fn.__name__)
        except AssertionError as e:
            failed += 1
            print("FAIL", fn.__name__, "-", e or "assert")
        except Exception as e:
            failed += 1
            print("ERROR", fn.__name__, "-", repr(e))
    print(f"{len(fns)-failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
