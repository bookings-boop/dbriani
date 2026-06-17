#!/usr/bin/env python3
"""Bug 3 (2026-06-17): `/review confirmed` — a focused, uncapped list of
CONFIRMED bookings (upcoming ✅ + past-event 🏁 COMPLETED). Today the arg is
unsupported end-to-end: the bridge only honors hot/warm/cold, so "confirmed"
behaves like a bare /review (full pipeline). Fix is flag-gated
(REVIEW_CONFIRMED_FILTER_ENABLED):
  - server.read_lead_summary('confirmed') scopes the DB read to label=CONFIRMED
    (past-event ones peel into the existing 🏁 COMPLETED virtual section at
    render time — so the render needs NO change);
  - routes.handle_review routes 'confirmed' to that filter + uncaps it, ONLY
    when the flag is on; OFF → 'confirmed' falls through to today's behavior.

Part A drives server.read_lead_summary with _psql captured (assert the WHERE).
Part B drives routes.handle_review with server.* mocked (assert the gating).

Run: python3 hermes-bridge/test_review_confirmed_filter.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import routes  # noqa: E402
import server  # noqa: E402

_FLAG = "REVIEW_CONFIRMED_FILTER_ENABLED"


# ---- Part A: read_lead_summary filter -> WHERE clause -----------------------
def _capture_sql(filter_label):
    """Call read_lead_summary with _psql captured; return all SQL issued."""
    sqls = []
    real = server._psql

    def fake(sql, timeout=12):
        sqls.append(sql)
        return ("", None)   # empty stdout -> zero rows

    server._psql = fake
    try:
        server.read_lead_summary(filter_label)
    finally:
        server._psql = real
    return "\n".join(sqls)


def test_confirmed_filter_scopes_to_confirmed_label():
    sql = _capture_sql("confirmed")
    assert "label = 'CONFIRMED'" in sql, \
        "confirmed filter must scope the read to label=CONFIRMED"


def test_confirmed_filter_does_not_pull_other_tiers():
    sql = _capture_sql("confirmed")
    assert "label IN ('HOT'" not in sql and "label = 'WARM'" not in sql


def test_none_filter_has_no_label_scope():
    sql = _capture_sql(None)
    assert "label = 'CONFIRMED'" not in sql
    assert "label IN ('HOT'" not in sql


def test_hot_filter_unchanged():
    sql = _capture_sql("hot")
    assert "label IN ('HOT','NEEDS_ATTENTION')" in sql


# ---- Part B: handle_review gating -------------------------------------------
def _drive_review(payload, flag_on):
    """Run routes.handle_review with server.* mocked; return what filter the
    read got and what uncap render got."""
    cap = {}
    names = {
        "read_lead_summary": lambda fl=None: (cap.update(read_filter=fl) or []),
        "render_review": lambda scored, totals, mode="ondemand", uncap=False: (
            cap.update(uncap=uncap, mode=mode)
            or {"telegram_text": "", "inline_keyboards": [],
                "header_text": "", "per_lead_messages": [],
                "mark_seen_ids": []}),
        "score_lead": lambda r, x: 0,
        "mark_review_seen": lambda ids: None,
        "refresh_customer_facts_from_waha": lambda c: False,
        "REVIEW_INLINE_REFRESH_CAP": 8,
    }
    saved = {k: getattr(server, k, None) for k in names}
    prev_flag = os.environ.get(_FLAG)
    os.environ[_FLAG] = "1" if flag_on else "0"
    out = {}
    try:
        for k, v in names.items():
            setattr(server, k, v)
        routes.handle_review(payload, lambda code, body: out.update(body))
    finally:
        for k, v in saved.items():
            setattr(server, k, v)
        if prev_flag is None:
            os.environ.pop(_FLAG, None)
        else:
            os.environ[_FLAG] = prev_flag
    return cap, out


def test_confirmed_routes_and_uncaps_when_flag_on():
    cap, out = _drive_review({"filter": "confirmed"}, flag_on=True)
    assert cap.get("read_filter") == "confirmed", \
        "flag ON: 'confirmed' must scope the DB read"
    assert cap.get("uncap") is True, "confirmed list must be uncapped"
    assert out.get("ok") is True


def test_confirmed_falls_through_when_flag_off():
    cap, out = _drive_review({"filter": "confirmed"}, flag_on=False)
    assert cap.get("read_filter") is None, \
        "flag OFF: 'confirmed' must behave as today (full pipeline)"
    assert cap.get("uncap") is False


def test_hot_filter_unaffected_by_flag():
    cap, _ = _drive_review({"filter": "hot"}, flag_on=True)
    assert cap.get("read_filter") == "hot"
    assert cap.get("uncap") is True


def test_bare_review_unchanged():
    cap, _ = _drive_review({}, flag_on=True)
    assert cap.get("read_filter") is None
    assert cap.get("uncap") is False


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn(); print("PASS", fn.__name__)
        except AssertionError as e:
            failed += 1; print("FAIL", fn.__name__, "-", e or "assert")
        except Exception as e:  # noqa: BLE001
            failed += 1; print("ERROR", fn.__name__, "-", repr(e))
    print(f"{len(fns)-failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
