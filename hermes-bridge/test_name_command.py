#!/usr/bin/env python3
"""Unit tests for _name_update_sql() — the /name operator rename builder.

/name <customer> <new name> lets the operator correct a customer's name AND
lock it (name-lock, migration 009) so the per-message fact-extraction can't
revert it (root-caused 2026-06-06: 'Zayn' kept reverting). This is the pure
SQL builder behind the handle_name endpoint.

Contract for _name_update_sql(customer_id, new_name, reason='manual:/name'):
  - sets name to the new value, name_locked=true, and a lock reason.
  - targets exactly the resolved customer_id.
  - literal-escapes every value via _lit (injection-safe on names).

Plain-assert style. Run: python3 hermes-bridge/test_name_command.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from server import _name_update_sql  # noqa: E402


def test_sets_name_and_locks():
    sql = _name_update_sql("971509767187@c.us", "Zayn")
    assert "name = 'Zayn'" in sql, sql
    assert "name_locked = true" in sql, sql
    assert "name_lock_reason = 'manual:/name'" in sql, sql
    assert "WHERE customer_id = '971509767187@c.us'" in sql, sql


def test_custom_reason():
    sql = _name_update_sql("x@c.us", "Bob", "manual:identity_correct")
    assert "name_lock_reason = 'manual:identity_correct'" in sql, sql


def test_escapes_quotes_in_name_and_cid():
    sql = _name_update_sql("x'y@c.us", "O'Brien")
    assert "'O''Brien'" in sql, sql
    assert "'x''y@c.us'" in sql, sql


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} name_command tests passed")
