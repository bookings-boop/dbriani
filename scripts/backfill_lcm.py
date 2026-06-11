#!/usr/bin/env python3
"""backfill_lcm.py — paired, tranched backfill of conversation_state
timestamps from WAHA (operator-approved 2026-06-11).

THE GAP: 82 of 165 active-pipeline leads have NULL last_customer_message_at
(rows created by /review renders + analysis writes for imported chats — the
real writers only fire on NEW inbound), blinding owe-reply/staleness for
half the pipeline.

SAFETY MODEL (all operator-mandated):
- PAIRED: last_operator_reply_at backfilled (only-when-NULL) from WAHA
  fromMe in the same statement — unpaired backfill flips owed 19→87 and
  floods ~324 cards/day (the saturation trap).
- TRANCHED (default 15/run, ~daily) — damps the card stream, the hourly
  re-analysis batch, and the COLD-decay wave.
- IDEMPOTENT: WHERE last_customer_message_at IS NULL — re-runs and races
  with live writers are harmless; live writes always win.
- SNAPSHOT ROLLBACK: conversation_state_bak_backfill_20260611 created once
  before the first write. Restore:
    UPDATE conversation_state cs SET
      last_customer_message_at = b.last_customer_message_at,
      last_operator_reply_at   = b.last_operator_reply_at,
      updated_at               = b.updated_at
    FROM conversation_state_bak_backfill_20260611 b
    WHERE b.customer_id = cs.customer_id;
- The OWE_MAX_AGE sweep gate (336h) must be LIVE before the first tranche.

Run ON THE BOX:  python3 backfill_lcm.py [--tranche-size 15] [--execute]
Dry-run (default) prints the tranche rows + owed before/after, writes NOTHING.
"""
import argparse
import os
import sys

sys.path.insert(0, "/home/ubuntu/hermes-bridge")
# Standalone scripts don't inherit the bridge's systemd EnvironmentFile —
# load .env (WAHA_API_KEY/WAHA_BASE etc.) before importing waha.
with open("/home/ubuntu/hermes-bridge/.env") as _f:
    for _ln in _f:
        _ln = _ln.strip()
        if _ln and not _ln.startswith("#") and "=" in _ln:
            _k, _v = _ln.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())
from db import _psql  # noqa: E402
from waha import waha_fetch_raw  # noqa: E402

SNAP = "conversation_state_bak_backfill_20260611"
LABELS = ("'NEW','WARM','HOT','NEEDS_ATTENTION','COLD',"
          "'WAITING_FOR_PAYMENT','CONFIRMED'")
OWED_SQL = (
    "SELECT count(*) FROM customer_facts cf "
    "JOIN conversation_state cs USING (customer_id) "
    "WHERE cf.merged_into IS NULL "
    "AND upper(cf.label) NOT IN ('DISREGARDED','LOST','SCAM','COMPLETED') "
    "AND upper(cf.label) NOT LIKE 'PAUSED%' "
    "AND cs.last_customer_message_at > GREATEST("
    "COALESCE(cs.last_operator_reply_at,'epoch'), "
    "COALESCE(cs.last_nudge_drafted_at,'epoch'))")


def q(sql):
    out, err = _psql(sql, timeout=30)
    if err:
        raise SystemExit(f"psql error: {err}")
    return (out or "").strip()


def owed_count():
    return int(q(OWED_SQL) or "0")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tranche-size", type=int, default=15)
    ap.add_argument("--execute", action="store_true")
    a = ap.parse_args()

    rows = q(
        "SELECT cs.customer_id FROM conversation_state cs "
        "JOIN customer_facts cf USING (customer_id) "
        f"WHERE cf.merged_into IS NULL AND cf.label IN ({LABELS}) "
        "AND cs.last_customer_message_at IS NULL "
        "ORDER BY cf.updated_at DESC "
        f"LIMIT {int(a.tranche_size)}").splitlines()
    cids = [r.strip() for r in rows if r.strip()]
    print(f"tranche candidates (NULL lcm, newest first): {len(cids)}")
    if not cids:
        print("nothing to backfill — done.")
        return

    values = []
    for cid in cids:
        try:
            msgs = waha_fetch_raw(cid, limit=50) or []
        except Exception as e:  # noqa: BLE001
            print(f"  {cid}: WAHA fetch err {e!r} — skipped")
            continue
        # waha_fetch_raw returns {ts, direction:'in'|'out', body, msg_id}
        win = max((m.get("ts") or 0 for m in msgs
                   if m.get("direction") == "in"), default=0)
        wout = max((m.get("ts") or 0 for m in msgs
                    if m.get("direction") == "out"), default=0)
        if not win:
            print(f"  {cid}: no inbound in WAHA — left NULL (by design)")
            continue
        scid = cid.replace("'", "''")
        values.append(
            f"('{scid}', to_timestamp({int(win)}), "
            + (f"to_timestamp({int(wout)})" if wout else "NULL") + ")")
        print(f"  {cid}: in={int(win)} out={int(wout) or '-'}")

    if not values:
        print("no fillable rows in this tranche.")
        return

    update_sql = (
        "UPDATE conversation_state cs SET "
        "last_customer_message_at = v.win, "
        "last_operator_reply_at = COALESCE(cs.last_operator_reply_at, v.wout), "
        "updated_at = now() "
        "FROM (VALUES " + ", ".join(values) + ") "
        "AS v(customer_id, win, wout) "
        "WHERE cs.customer_id = v.customer_id "
        "AND cs.last_customer_message_at IS NULL "
        "AND v.win IS NOT NULL")

    before = owed_count()
    print(f"\nowed BEFORE: {before}")
    if not a.execute:
        print(f"DRY-RUN — would update {len(values)} rows. "
              "Re-run with --execute to write.")
        print("UPDATE shape:\n" + update_sql[:600] + " …")
        return

    snap_exists = q(f"SELECT count(*) FROM information_schema.tables "
                    f"WHERE table_name = '{SNAP}'")
    if snap_exists.strip() == "0":
        q(f"CREATE TABLE {SNAP} AS TABLE conversation_state")
        print(f"snapshot created: {SNAP} "
              f"({q(f'SELECT count(*) FROM {SNAP}')} rows)")
    else:
        print(f"snapshot already exists: {SNAP}")

    q(update_sql)
    after = owed_count()
    filled = q("SELECT count(*) FROM conversation_state cs "
               "JOIN customer_facts cf USING (customer_id) "
               f"WHERE cf.merged_into IS NULL AND cf.label IN ({LABELS}) "
               "AND cs.last_customer_message_at IS NULL")
    print(f"TRANCHE WRITTEN: {len(values)} rows · owed {before} -> {after} "
          f"· remaining NULL: {filled}")


if __name__ == "__main__":
    main()
