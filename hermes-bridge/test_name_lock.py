#!/usr/bin/env python3
"""Unit tests for the name-lock guard in the customer_facts upsert.

Root cause (2026-06-06): upsert_customer_facts wrote `name = EXCLUDED.name`
UNCONDITIONALLY on every message. _merge_facts lets a fresh LLM-extracted name
overwrite the cached one, so an operator's manual rename was wiped the next time
a message arrived (customer 971509767187 "Zayn" kept reverting to a mis-extracted
"Antonio"). Fix: a name_locked flag (migration 009) the upsert must respect —
a locked row keeps its existing name; everything else still updates from the
latest extraction.

Contract for _upsert_facts_sql(customer_id, name, facts):
  - ON CONFLICT, name is conditional on name_locked (locked → keep existing).
  - An UNLOCKED row still takes the freshly-extracted name (no regression).
  - dates / yachts / party_size still overwrite from EXCLUDED unconditionally.
  - message_count still increments atomically in SQL.
  - values are literal-escaped via _lit.

Plain-assert style. Run: python3 hermes-bridge/test_name_lock.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from server import _upsert_facts_sql  # noqa: E402

_FACTS = {"dates": "Jun 20", "yachts": "Bliss 55", "party_size": "8"}


def test_locked_name_is_preserved_not_overwritten():
    sql = _upsert_facts_sql("971509767187@c.us", "Zayn", _FACTS)
    # The name update must be GATED on name_locked, not an unconditional
    # `name = EXCLUDED.name` (the original bug).
    assert "name = CASE WHEN customer_facts.name_locked" in sql, sql
    assert "THEN customer_facts.name" in sql, sql
    assert "name = EXCLUDED.name," not in sql, sql


def test_unlocked_name_still_updates_from_extraction():
    # An unlocked row must still adopt the freshly-extracted name — we only
    # protect names the operator explicitly locked.
    sql = _upsert_facts_sql("x@c.us", "Bob", _FACTS)
    assert "ELSE EXCLUDED.name END" in sql, sql


def test_other_facts_still_overwrite():
    sql = _upsert_facts_sql("x@c.us", "Bob", _FACTS)
    assert "dates = EXCLUDED.dates" in sql, sql
    assert "yachts = EXCLUDED.yachts" in sql, sql
    assert "party_size = EXCLUDED.party_size" in sql, sql


def test_message_count_still_increments():
    sql = _upsert_facts_sql("x@c.us", "Bob", _FACTS)
    assert "message_count = customer_facts.message_count + 1" in sql, sql


def test_values_are_literal_escaped():
    sql = _upsert_facts_sql("x@c.us", "O'Brien", {"dates": "", "yachts": "",
                                                  "party_size": ""})
    # _lit escapes a single quote by doubling it — proves we go through _lit,
    # not raw string interpolation (SQL-injection safety on customer names).
    assert "'O''Brien'" in sql, sql


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} name_lock tests passed")
