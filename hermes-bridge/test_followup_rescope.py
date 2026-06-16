#!/usr/bin/env python3
"""Lever ③ — re-scope the proactive candidate SELECT to qualified labels.

Root cause (2026-06-16, grounded): _followup_candidate_sql() is ORDER BY
oldest-first LIMIT 80; the long-ghosted NEW/no-facts pool fills all 80 rows and
is then 100% discarded by _silence_window_for(), starving the newer label-
qualified leads -> live yield 0.

Fix (flag FOLLOWUP_RESCOPE_QUALIFIED_ENABLED, default OFF = legacy byte-for-byte):
ON -> positively INCLUDE only HOT/NEEDS_ATTENTION/WARM/COLD so the budget reaches
them; this also structurally excludes the 269 unanalyzed NULL/no-facts + 11 NEW +
all terminal labels (incl. hermes_disregard under DISREGARDED) in one clause.
ONE variable: ordering + 14d ceiling unchanged.

Run: python3 hermes-bridge/test_followup_rescope.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402


def test_flag_off_is_legacy_permissive_clause():
    os.environ.pop("FOLLOWUP_RESCOPE_QUALIFIED_ENABLED", None)
    sql = server._followup_candidate_sql()
    assert "cf.label IS NULL OR cf.label NOT IN" in sql, sql[-400:]
    assert "cf.label IN ('HOT'" not in sql
    assert "'LOST'" in sql and "'DISREGARDED'" in sql  # terminal still suppressed


def test_flag_on_includes_only_qualified_labels():
    os.environ["FOLLOWUP_RESCOPE_QUALIFIED_ENABLED"] = "1"
    try:
        sql = server._followup_candidate_sql()
    finally:
        os.environ.pop("FOLLOWUP_RESCOPE_QUALIFIED_ENABLED", None)
    assert "cf.label IN ('HOT', 'NEEDS_ATTENTION', 'WARM', 'COLD')" in sql, sql
    # NEW/NULL no longer admitted; the legacy permissive clause is gone
    assert "cf.label IS NULL OR cf.label NOT IN" not in sql


def test_rescope_changes_only_label_clause_not_ordering_or_ceiling():
    os.environ.pop("FOLLOWUP_RESCOPE_QUALIFIED_ENABLED", None)
    off = server._followup_candidate_sql()
    os.environ["FOLLOWUP_RESCOPE_QUALIFIED_ENABLED"] = "1"
    try:
        on = server._followup_candidate_sql()
    finally:
        os.environ.pop("FOLLOWUP_RESCOPE_QUALIFIED_ENABLED", None)
    # one variable: ordering + window bounds identical across the flag
    for clause in ("ORDER BY cs.last_operator_reply_at ASC", "LIMIT 80",
                   "interval '14 days'", "interval '24 hours'",
                   "interval '48 hours'"):
        assert clause in off and clause in on, clause


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} rescope tests passed")
