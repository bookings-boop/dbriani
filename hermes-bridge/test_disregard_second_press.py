#!/usr/bin/env python3
"""F2 (2026-06-12): second-press Disregard escalation — the operator owns the
lead, the analyzer advises ONCE. A plain Disregard on a non-terminal lead
defers to Hermes (RULE 3 hard-codes keep_open for B2B/broker → a single press
can never close such a lead; Olga = 15 vetoed presses). A REPEAT press within
30 days of a keep_open veto escalates to the force-close path.

Drives routes.handle_lead_analyze_disregard with server.* + routes._psql
mocked (no DB / no Hermes).

Run: python3 hermes-bridge/test_disregard_second_press.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import routes  # noqa: E402
import server  # noqa: E402


class _Harness:
    """Patch server.* + routes._psql for one handle_lead_analyze_disregard call.

    prior_veto: True → the prior-keep_open SELECT returns a row (escalate).
    hermes_verdict: what hermes_analyze_lead returns ('keep_open'|'close'|None).
    """

    def __init__(self, *, label, prior_veto=False, hermes_verdict="keep_open"):
        self.label = label
        self.prior_veto = prior_veto
        self.hermes_verdict = hermes_verdict
        self.hermes_called = False
        self.transitions = []     # (from, to, signal, created_by)
        self.snapshot_writes = []
        self._orig = {}

    def _psql(self, sql, *a, **k):
        if "disregard_verdict = 'keep_open'" in sql and "30 days" in sql:
            return (("1\n", None) if self.prior_veto else ("", None))
        if sql.strip().startswith("UPDATE customer_facts SET"):
            self.snapshot_writes.append(sql)
            return ("", None)
        if "EXTRACT(EPOCH" in sql:
            return ("3600\n", None)
        return ("", None)

    def _row(self, cid):
        return {"customer_id": cid, "label": self.label, "message_count": 7,
                "name": "Olga"}

    def _facts(self, cid):
        return {"name": "Olga", "message_count": 7}

    def _apply(self, cid, frm, to, signal=None, evidence=None,
               message_count=0, created_by="system"):
        self.transitions.append((frm, to, signal, created_by))
        return ("", None)

    def _analyze(self, cid, history, facts, message_count=0, silent_hours=None):
        self.hermes_called = True
        if self.hermes_verdict is None:
            return None
        return {"verdict": self.hermes_verdict, "reasoning": "B2B broker",
                "suggested_action": "", "importance_score": 12}

    def _hist(self, cid, waha_limit=100):
        return {"history": "Customer (1h ago): \"for my client\""}

    def __enter__(self):
        for name, fn in [
            ("get_current_label_row", self._row),
            ("get_customer_facts", self._facts),
            ("apply_label_transition", self._apply),
            ("hermes_analyze_lead", self._analyze),
            ("build_analyzer_history", self._hist),
            ("waha_fetch_history", lambda c, **k: {"history": ""}),
            ("_name_fallback", lambda c: "Olga"),
            ("_md_escape", lambda s: s),
        ]:
            self._orig[name] = getattr(server, name, None)
            setattr(server, name, fn)
        self._orig["_psql"] = routes._psql
        routes._psql = self._psql
        return self

    def __exit__(self, *a):
        for name, fn in self._orig.items():
            if name == "_psql":
                routes._psql = fn
            elif fn is not None:
                setattr(server, name, fn)


def _call(h, payload):
    out = {}
    with h:
        routes.handle_lead_analyze_disregard(
            payload, lambda code, body: out.update(body))
    return out


# --- press 1: no prior veto → Hermes consulted, keep_open, no close ----------
def test_first_press_consults_hermes_and_vetoes():
    h = _Harness(label="NEW", prior_veto=False, hermes_verdict="keep_open")
    out = _call(h, {"customer_id": "84491334889553@lid"})
    assert h.hermes_called is True, "first press must consult Hermes"
    assert out.get("verdict") == "keep_open"
    assert out.get("label_after") == "NEW"
    assert not h.transitions, "no label change on a veto"
    assert "reply_markup" in out, "override buttons must be offered"


# --- press 2: prior keep_open within 30d → escalate to force close -----------
def test_second_press_escalates_to_force_close():
    h = _Harness(label="NEW", prior_veto=True)
    out = _call(h, {"customer_id": "84491334889553@lid"})
    assert h.hermes_called is False, "escalation must SKIP Hermes"
    assert out.get("label_after") == "DISREGARDED"
    assert out.get("verdict") == "close"
    assert len(h.transitions) == 1
    frm, to, signal, created_by = h.transitions[0]
    assert (frm, to) == ("NEW", "DISREGARDED")
    assert signal == "operator_override"
    assert created_by == "operator:disregard_force"


# --- prior verdict was 'close' (not keep_open) → no escalation ---------------
def test_prior_close_does_not_escalate():
    # prior_veto=False models the keep_open-SELECT returning empty (the row
    # exists but disregard_verdict='close', so the WHERE clause excludes it).
    h = _Harness(label="NEW", prior_veto=False, hermes_verdict="keep_open")
    out = _call(h, {"customer_id": "x@lid"})
    assert h.hermes_called is True
    assert out.get("verdict") == "keep_open"


# --- COLD lead → one-tap close, no Hermes, no escalation SELECT needed --------
def test_cold_label_one_tap_close():
    h = _Harness(label="COLD", prior_veto=False)
    out = _call(h, {"customer_id": "x@lid"})
    assert h.hermes_called is False
    assert out.get("label_after") == "DISREGARDED"
    frm, to, signal, created_by = h.transitions[0]
    assert created_by == "operator:disregard_force"
    # one-tap terminal close evidence is NOT the override wording
    assert signal == "operator_override"


# --- explicit force=true (Close anyway button) → force path, no Hermes -------
def test_explicit_force_button():
    h = _Harness(label="NEW", prior_veto=False)
    out = _call(h, {"customer_id": "x@lid", "force": True})
    assert h.hermes_called is False
    assert out.get("label_after") == "DISREGARDED"


# --- Hermes timeout on a first press → degraded, no label change -------------
def test_hermes_timeout_degrades_safely():
    h = _Harness(label="NEW", prior_veto=False, hermes_verdict=None)
    out = _call(h, {"customer_id": "x@lid"})
    assert out.get("degraded") is True
    assert not h.transitions


# --- a real close verdict still routes via reclassify (LOST/DISREGARDED) -----
def test_first_press_close_verdict_still_closes():
    h = _Harness(label="NEW", prior_veto=False, hermes_verdict="close")
    out = _call(h, {"customer_id": "x@lid"})
    assert h.hermes_called is True
    assert out.get("verdict") == "close"
    assert len(h.transitions) == 1
    assert h.transitions[0][1] in ("DISREGARDED", "LOST", "SCAM")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} disregard_second_press tests passed")
