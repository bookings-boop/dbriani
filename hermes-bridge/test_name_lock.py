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


# --- migration 011: booking_date_abs / booking_time / addons persistence -----
# W5+W4 captured these in extract_customer_facts + render them in review/info,
# but _upsert_facts_sql never wrote them (dead data). They must now (a) be in
# the INSERT column list + VALUES, and (b) update STICKILY on conflict so a
# later chit-chat message that extracts NOTHING does not blank a previously
# captured value, while a non-blank re-statement DOES replace it.

_BK_FACTS = {"dates": "Jun 20", "yachts": "Bliss 55", "party_size": "8",
             "booking_date_abs": "2026-06-20", "booking_time": "5PM-9PM",
             "addons": "BBQ, jetski"}


def test_booking_detail_columns_in_insert_and_values():
    sql = _upsert_facts_sql("x@c.us", "Bob", _BK_FACTS)
    # the three new columns must be in the INSERT column list ...
    assert "booking_date_abs" in sql, sql
    assert "booking_time" in sql, sql
    assert "addons" in sql, sql
    # ... and the captured values must flow into VALUES via _lit.
    assert "'2026-06-20'" in sql, sql
    assert "'5PM-9PM'" in sql, sql
    assert "'BBQ, jetski'" in sql, sql


def test_booking_detail_sticky_update_on_conflict():
    # A non-blank re-extraction REPLACES (NULLIF passes the value through,
    # COALESCE takes EXCLUDED); a blank one (EXCLUDED -> NULL via _lit ->
    # NULLIF stays NULL) falls back to the stored customer_facts value, so a
    # later chit-chat message can NEVER blank a captured date/time/addons.
    sql = _upsert_facts_sql("x@c.us", "Bob", _BK_FACTS)
    assert ("booking_date_abs = COALESCE(NULLIF(EXCLUDED.booking_date_abs,''), "
            "customer_facts.booking_date_abs)") in sql, sql
    assert ("booking_time = COALESCE(NULLIF(EXCLUDED.booking_time,''), "
            "customer_facts.booking_time)") in sql, sql
    assert ("addons = COALESCE(NULLIF(EXCLUDED.addons,''), "
            "customer_facts.addons)") in sql, sql
    # NOT an unconditional overwrite — that is exactly the blanking bug we avoid.
    assert "booking_date_abs = EXCLUDED.booking_date_abs," not in sql, sql
    assert "booking_time = EXCLUDED.booking_time," not in sql, sql
    assert "addons = EXCLUDED.addons," not in sql, sql


def test_blank_booking_detail_inserts_null():
    # A message with no date/time/addons: _lit('') -> NULL, so the INSERT
    # carries NULL (not ''), and on conflict the sticky COALESCE keeps any
    # prior value. The columns are still listed (so the row shape is stable).
    sql = _upsert_facts_sql("x@c.us", "Bob", {
        "dates": "", "yachts": "", "party_size": "",
        "booking_date_abs": "", "booking_time": "", "addons": ""})
    assert "booking_date_abs" in sql and "booking_time" in sql \
        and "addons" in sql, sql
    # the sticky-keep clause is present regardless of the (blank) values.
    assert "COALESCE(NULLIF(EXCLUDED.booking_date_abs,'')" in sql, sql


def test_existing_facts_logic_untouched():
    # Guard: adding booking detail must not disturb the name-lock / dates /
    # yachts / party_size / message_count contract.
    sql = _upsert_facts_sql("x@c.us", "Bob", _BK_FACTS)
    assert "name = CASE WHEN customer_facts.name_locked" in sql, sql
    assert "dates = EXCLUDED.dates" in sql, sql
    assert "yachts = EXCLUDED.yachts" in sql, sql
    assert "party_size = EXCLUDED.party_size" in sql, sql
    assert "message_count = customer_facts.message_count + 1" in sql, sql


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} name_lock tests passed")
