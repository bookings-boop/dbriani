#!/usr/bin/env python3
"""Idempotently seed the durable conversation store (migration 010) from the
data we already have, so hermes_analyze_lead + the drafter stop depending on a
live WAHA fetch (8% empty, 17% degraded; a 157-msg CONFIRMED booking -> WAHA 0
-> scored 0/100).

Two sources, both fed through the FAIL-SAFE server.record_message
(INSERT ... ON CONFLICT (customer_id, msg_id) DO NOTHING):

  (1) draft_log — incoming_message  -> direction 'in'
                  final_text|draft_text -> direction 'out'
      each keyed by a deterministic draft_id-derived msg_id so a re-run dedupes.
  (2) each lead's CURRENT WAHA history (waha_fetch_raw) — keyed by the provider
      msg_id when present, else a deterministic synthetic id (waha:<cid>:<ts>:
      <bodyhash>) so a re-run dedupes even for id-less messages.

SAFETY / DISCIPLINE (this script mirrors record_message + the draft_log spec):
  * READ-ONLY w.r.t. everything except INSERTing into conversation_messages.
    It only ever SELECTs from draft_log / customer_facts and appends rows.
  * IDEMPOTENT — safe to re-run; ON CONFLICT DO NOTHING means no dup rows.
  * NO-OP gracefully if migration 010 is not applied yet: record_message
    swallows the relation-absent error, and we probe + print + exit 0 first.
  * NEVER touches send/payment paths; never runs against prod automatically —
    it is an operator-invoked one-shot.

Usage:
  python3 scripts/backfill_conversation_messages.py            # both sources
  python3 scripts/backfill_conversation_messages.py --source draftlog
  python3 scripts/backfill_conversation_messages.py --source waha --limit-cids 50
  python3 scripts/backfill_conversation_messages.py --dry-run  # count, no INSERT

The PURE pieces (SQL builders, the encoded-row decoder, the draft_log->rows +
WAHA->rows mappers) are unit-tested in hermes-bridge/test_backfill_conversation.py.
"""
import argparse
import hashlib
import os
import sys

# Make the bridge package importable whether run from the repo root or scripts/.
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "hermes-bridge"))

# Control-char encoding — MUST match server._CONV_FS / server._CONV_NL so the
# decoder below round-trips the encoded SELECT output. _psql is fixed `-tA`
# (one tuple per line, columns joined by '|', rows by '\n'); message/draft
# bodies contain '|' AND newlines, so we emit ONE column per row joining fields
# with US (chr 31) and stand in for in-body newlines with RS (chr 30).
_FS = "\x1f"
_NL = "\x1e"


# --- pure SQL builders -----------------------------------------------

def _enc_text_sql(col):
    """SQL fragment that makes a text column safe for single-column `-tA`
    output: strip US, CR->space, LF->RS placeholder, TAB->space."""
    return ("replace(replace(replace(replace(coalesce(" + col + ",''), "
            "chr(31), ' '), chr(13), ' '), chr(10), chr(30)), chr(9), ' ')")


def draftlog_select_sql():
    """All draft_log rows encoded as one parseable line each:
    draft_id <US> customer_id <US> incoming_message <US> draft_text <US>
    final_text. Oldest-first so the durable store ends up time-ordered."""
    return (
        "SELECT coalesce(draft_id,'') || chr(31) || coalesce(customer_id,'') "
        "|| chr(31) || " + _enc_text_sql("incoming_message")
        + " || chr(31) || " + _enc_text_sql("draft_text")
        + " || chr(31) || " + _enc_text_sql("final_text")
        + " FROM draft_log ORDER BY created_at ASC, id ASC")


def active_cids_sql():
    """Every non-merged lead identity — the WAHA backfill walks these. Merged
    duplicates (merged_into NOT NULL) are skipped so we never resurrect a
    canonicalized-away identity."""
    return ("SELECT customer_id FROM customer_facts "
            "WHERE merged_into IS NULL AND customer_id <> '' "
            "ORDER BY updated_at DESC")


# --- pure decoder ----------------------------------------------------

def decode_rows(out, nfields):
    """Parse _FS/_NL-encoded `-tA` output into a list of field-lists (each of
    length `nfields`). Tolerant: short/garbage lines are skipped, never
    raise. In-body newlines (RS placeholder) are restored to real newlines."""
    rows = []
    for line in (out or "").split("\n"):
        if not line:
            continue
        parts = line.split(_FS)
        if len(parts) < nfields:
            continue
        rows.append([p.replace(_NL, "\n") for p in parts[:nfields]])
    return rows


def parse_draftlog_output(out):
    """decode_rows -> list of draft_log dicts."""
    keys = ("draft_id", "customer_id", "incoming_message",
            "draft_text", "final_text")
    return [dict(zip(keys, r)) for r in decode_rows(out, len(keys))]


# --- pure mappers ----------------------------------------------------

def draft_log_rows_to_messages(rows):
    """draft_log rows -> [(cid, direction, body, msg_id)]. incoming_message ->
    'in'; final_text (preferred) else draft_text -> 'out'. msg_id is derived
    from the draft_id so a re-run dedupes via ON CONFLICT. Rows with no
    customer_id, or with nothing to record, are skipped."""
    out = []
    for r in rows:
        cid = (r.get("customer_id") or "").strip()
        if not cid:
            continue
        did = (r.get("draft_id") or "").strip()
        inc = (r.get("incoming_message") or "").strip()
        if inc:
            out.append((cid, "in", inc,
                        (f"draftlog:{did}:in" if did else None)))
        outbound = ((r.get("final_text") or "").strip()
                    or (r.get("draft_text") or "").strip())
        if outbound:
            out.append((cid, "out", outbound,
                        (f"draftlog:{did}:out" if did else None)))
    return out


def _synthetic_waha_id(cid, ts, body):
    h = hashlib.sha1((body or "").encode("utf-8")).hexdigest()[:10]
    return f"waha:{cid}:{int(ts or 0)}:{h}"


def waha_rows_to_records(cid, waha_rows):
    """waha_fetch_raw() rows -> [(cid, direction, body, msg_id)]. Empty bodies
    are dropped (no transcript value). msg_id is the provider id when present,
    else a deterministic synthetic id so an id-less message still dedupes on
    a re-run."""
    out = []
    for r in (waha_rows or []):
        body = (r.get("body") or "")
        if not body.strip():
            continue
        direction = "out" if r.get("direction") == "out" else "in"
        mid = r.get("msg_id") or _synthetic_waha_id(cid, r.get("ts"), body)
        out.append((cid, direction, body, mid))
    return out


# --- runner (impure; not unit-tested — guarded fail-safe) ------------

def _table_present(_psql):
    """True only if conversation_messages exists. Any error (incl. the
    relation-absent error BEFORE migration 010 is applied) -> False."""
    _out, err = _psql("SELECT 1 FROM conversation_messages LIMIT 1", timeout=8)
    return err is None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", choices=("all", "draftlog", "waha"),
                    default="all")
    ap.add_argument("--limit-cids", type=int, default=0,
                    help="cap the number of leads scanned for WAHA backfill "
                         "(0 = no cap)")
    ap.add_argument("--waha-limit", type=int, default=200,
                    help="messages to pull per lead from WAHA")
    ap.add_argument("--dry-run", action="store_true",
                    help="count records but issue NO INSERTs")
    args = ap.parse_args(argv)

    import server  # lazy: avoids server import cost for the unit tests
    from db import _psql

    if not _table_present(_psql):
        print("conversation_messages not present — operator must apply "
              "db/migrations/010_conversation_messages.sql first. No-op.")
        return 0

    def _record(cid, direction, body, msg_id):
        if args.dry_run:
            return True
        return server.record_message(cid, direction, body, msg_id)

    n_dl = n_dl_seen = 0
    n_wa = n_wa_seen = 0

    # (1) draft_log
    if args.source in ("all", "draftlog"):
        out, err = _psql(draftlog_select_sql(), timeout=60)
        if err:
            print(f"draft_log read error (skipping): {err}")
        else:
            msgs = draft_log_rows_to_messages(parse_draftlog_output(out))
            n_dl_seen = len(msgs)
            for cid, direction, body, mid in msgs:
                if _record(cid, direction, body, mid):
                    n_dl += 1
        print(f"draft_log: {n_dl}/{n_dl_seen} records "
              f"{'(dry-run) ' if args.dry_run else ''}seeded")

    # (2) WAHA per active lead
    if args.source in ("all", "waha"):
        out, err = _psql(active_cids_sql(), timeout=30)
        cids = [] if err else [ln.strip() for ln in (out or "").splitlines()
                               if ln.strip()]
        if err:
            print(f"customer_facts read error (skipping WAHA): {err}")
        if args.limit_cids > 0:
            cids = cids[:args.limit_cids]
        for i, cid in enumerate(cids, 1):
            try:
                waha_rows = server.waha_fetch_raw(cid, limit=args.waha_limit)
            except Exception as e:  # never let one lead abort the backfill
                print(f"  waha_fetch_raw failed cid={cid!r}: {e!r}")
                continue
            recs = waha_rows_to_records(cid, waha_rows)
            n_wa_seen += len(recs)
            for c, direction, body, mid in recs:
                if _record(c, direction, body, mid):
                    n_wa += 1
            if i % 25 == 0:
                print(f"  ...{i}/{len(cids)} leads scanned")
        print(f"waha: {n_wa}/{n_wa_seen} records "
              f"{'(dry-run) ' if args.dry_run else ''}seeded "
              f"across {len(cids)} leads")

    print(f"DONE — draft_log={n_dl} waha={n_wa} "
          f"total={n_dl + n_wa}{' (dry-run, nothing written)' if args.dry_run else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
