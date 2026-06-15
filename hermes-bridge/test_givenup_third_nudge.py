#!/usr/bin/env python3
"""Regression: the THIRD nudge after a decline (Humdan 06-13 10:30) must not be
possible once the lead is terminalized.

(1) Dependency lock: the follow-up eligibility SQL excludes LOST — so fix #2
    (decline -> LOST) removes the lead from the nudge pool via existing
    machinery. This is the loop-closure fix #2 relies on.
(2) Belt-fix #3: the dead Audit-#7 guard read cs.last_analysis_signal, a column
    NEVER written with 'auto:analyzer%' (0/592 rows in prod) — so an analyzer-
    closed COLD lead stayed nudge-eligible. The guard is repaired to read
    customer_label_history instead.

Run: python3 hermes-bridge/test_givenup_third_nudge.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from server import _followup_candidate_sql  # noqa: E402


def test_lost_lead_is_not_nudge_eligible():
    sql = _followup_candidate_sql()
    assert "NOT IN" in sql
    assert "'LOST'" in sql and "'DISREGARDED'" in sql, \
        "a declined->LOST lead must be filtered out of the nudge population"


def test_dead_analyzer_guard_repaired():
    sql = _followup_candidate_sql()
    # the dead predicate read a conversation_state column that is never written
    assert "cs.last_analysis_signal NOT LIKE 'auto:analyzer%'" not in sql, \
        "dead Audit-#7 guard still reads the always-empty cs.last_analysis_signal"
    # repaired guard reads the table where the analyzer-close signal actually lands
    assert "customer_label_history" in sql and "auto:analyzer%" in sql, \
        "repaired guard must consult customer_label_history.signal"


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} givenup_third_nudge tests passed")
