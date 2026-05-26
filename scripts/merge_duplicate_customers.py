#!/usr/bin/env python3
"""merge_duplicate_customers.py — one-shot data fix for the
duplicate-cid problem (migration 006).

Finds customer_facts rows that share a name + (likely) belong to one
real customer under different WAHA cid formats. For each duplicate
pair: picks canonical (more messages or earlier first contact), merges
the displayable fields (yachts as union, name=longer-non-empty,
dates=latest), and sets merged_into on the non-canonical row pointing
at the canonical.

ALSO migrates foreign cid references in customer_label_history,
conversation_state, autonomous_sends, customer_notes, customer_triggers
so SELECTs against the canonical cid see the union of both rows'
history.

Idempotent: re-running is safe — already-merged rows have non-NULL
merged_into and are skipped.

Usage:
  python3 scripts/merge_duplicate_customers.py --plan      # show, don't apply
  python3 scripts/merge_duplicate_customers.py --apply     # do the merges
"""
import argparse
import subprocess
import sys


PG_CONTAINER = "n8n-postgres-1"


def psql(sql, ssh_target="dubriani-ec2"):
    """Run psql via ssh+docker exec. Returns (stdout, returncode)."""
    full = (
        f"docker exec {PG_CONTAINER} psql -U n8n -d n8n -tA -c {sql!r}"
    )
    r = subprocess.run(
        ["ssh", "-o", "ConnectTimeout=30", ssh_target, full],
        capture_output=True, text=True, timeout=60)
    return r.stdout, r.returncode


def find_duplicates():
    """Find pairs of customer_facts rows that look like the same person.

    Strategy:
      A. Same non-empty name AND both rows are canonical (merged_into
         IS NULL). Skip rows already merged.
      B. Order pair by message_count DESC; first wins canonical.
    """
    sql = (
        "SELECT name, "
        "array_agg(customer_id ORDER BY message_count DESC, "
        "                       updated_at DESC) AS cids, "
        "array_agg(message_count ORDER BY message_count DESC, "
        "                          updated_at DESC) AS mcs "
        "FROM customer_facts "
        "WHERE name IS NOT NULL AND name <> '' "
        "  AND merged_into IS NULL "
        "GROUP BY name HAVING COUNT(*) > 1"
    )
    out, rc = psql(sql)
    pairs = []
    for line in (out or "").strip().splitlines():
        parts = line.split("|", 2)
        if len(parts) < 3:
            continue
        name = parts[0].strip()
        cids = parts[1].strip("{}").split(",")
        mcs = [int(x) for x in parts[2].strip("{}").split(",") if x]
        if len(cids) < 2:
            continue
        canonical, duplicate = cids[0].strip(), cids[1].strip()
        pairs.append({
            "name": name,
            "canonical": canonical,
            "duplicate": duplicate,
            "canonical_msgs": mcs[0],
            "duplicate_msgs": mcs[1] if len(mcs) > 1 else 0,
            "extra_cids": [c.strip() for c in cids[2:]],
        })
    return pairs


def fetch_row(cid):
    """Pull canonical row fields for a single customer_id."""
    sql = (
        "SELECT COALESCE(name,''), COALESCE(yachts,''), "
        "COALESCE(dates,''), COALESCE(party_size,''), "
        "COALESCE(message_count,0), COALESCE(label,'') "
        f"FROM customer_facts WHERE customer_id = '{cid}'"
    )
    out, _ = psql(sql)
    line = (out or "").strip().splitlines()
    if not line:
        return None
    parts = line[0].split("|")
    if len(parts) < 6:
        return None
    return {
        "name": parts[0], "yachts": parts[1], "dates": parts[2],
        "party_size": parts[3],
        "message_count": int(parts[4]) if parts[4].isdigit() else 0,
        "label": parts[5],
    }


def merge_yachts(a, b):
    """Union, dedup, preserve order: a's yachts first, then b's
    new ones."""
    seen = set()
    out = []
    for src in (a, b):
        for y in (src or "").split(","):
            y = y.strip()
            if y and y.lower() not in seen:
                out.append(y)
                seen.add(y.lower())
    return ", ".join(out)


def longer_non_empty(a, b):
    """Prefer non-empty over empty; otherwise prefer the longer
    string (more specific names like 'Mark Hassan' beat 'Mark')."""
    a, b = (a or "").strip(), (b or "").strip()
    if a and not b:
        return a
    if b and not a:
        return b
    return a if len(a) >= len(b) else b


def merge_plan(pair):
    """Compute the data-merge fields for a duplicate pair."""
    canon = fetch_row(pair["canonical"])
    dup = fetch_row(pair["duplicate"])
    if not canon or not dup:
        return None
    merged = {
        "name": longer_non_empty(canon["name"], dup["name"]),
        "yachts": merge_yachts(canon["yachts"], dup["yachts"]),
        "dates": longer_non_empty(canon["dates"], dup["dates"]),
        "party_size": longer_non_empty(canon["party_size"],
                                       dup["party_size"]),
        "message_count": canon["message_count"] + dup["message_count"],
        "label": canon["label"] or dup["label"],
    }
    return {"canonical": canon, "duplicate": dup, "merged": merged}


def apply_merge(pair, plan):
    """Execute the merge in one transaction:
      1. UPDATE canonical row with merged fields
      2. Migrate foreign cid references (customer_label_history,
         conversation_state, autonomous_sends, customer_notes,
         customer_triggers) from duplicate cid → canonical cid
      3. SET merged_into pointer on duplicate row
    """
    c = pair["canonical"]
    d = pair["duplicate"]
    m = plan["merged"]
    # Escape single quotes for SQL literal embedding.
    def lit(v):
        if v is None or v == "":
            return "NULL"
        return "'" + str(v).replace("'", "''") + "'"
    tx = (
        "BEGIN; "
        # 1. merge canonical row's facts
        "UPDATE customer_facts SET "
        f"name = {lit(m['name'])}, "
        f"yachts = {lit(m['yachts'])}, "
        f"dates = {lit(m['dates'])}, "
        f"party_size = {lit(m['party_size'])}, "
        f"message_count = {m['message_count']}, "
        "updated_at = now() "
        f"WHERE customer_id = {lit(c)}; "
        # 2. migrate FK references — best-effort UPDATEs. Each table
        # may or may not have rows for the duplicate cid; the UPDATE
        # is a no-op when there are none. PK constraints on
        # conversation_state (customer_id is PK) mean we need to
        # handle conflict — merge by 'whichever timestamps newer'.
        f"UPDATE customer_label_history SET customer_id = {lit(c)} "
        f"WHERE customer_id = {lit(d)}; "
        f"UPDATE autonomous_sends SET customer_id = {lit(c)} "
        f"WHERE customer_id = {lit(d)}; "
        f"UPDATE customer_notes SET customer_id = {lit(c)} "
        f"WHERE customer_id = {lit(d)}; "
        f"UPDATE customer_triggers SET customer_id = {lit(c)} "
        f"WHERE customer_id = {lit(d)}; "
        # conversation_state has customer_id as PK — UPSERT-style:
        # if canonical row exists AND duplicate row exists, take
        # max of timestamps from duplicate into canonical; then
        # delete duplicate.
        f"INSERT INTO conversation_state (customer_id, "
        " last_customer_message_at, last_operator_reply_at, "
        " last_review_seen_at, last_nudge_drafted_at, "
        " last_analyzed_at, last_analysis_signal, "
        " last_analysis_confidence, reengage_attempts, "
        " followup_count, updated_at) "
        f"SELECT {lit(c)}, last_customer_message_at, "
        " last_operator_reply_at, last_review_seen_at, "
        " last_nudge_drafted_at, last_analyzed_at, "
        " last_analysis_signal, last_analysis_confidence, "
        " reengage_attempts, COALESCE(followup_count, 0), now() "
        f"FROM conversation_state WHERE customer_id = {lit(d)} "
        "ON CONFLICT (customer_id) DO UPDATE SET "
        " last_customer_message_at = GREATEST("
        "    conversation_state.last_customer_message_at, "
        "    EXCLUDED.last_customer_message_at), "
        " last_operator_reply_at = GREATEST("
        "    conversation_state.last_operator_reply_at, "
        "    EXCLUDED.last_operator_reply_at), "
        " last_review_seen_at = GREATEST("
        "    conversation_state.last_review_seen_at, "
        "    EXCLUDED.last_review_seen_at), "
        " last_nudge_drafted_at = GREATEST("
        "    conversation_state.last_nudge_drafted_at, "
        "    EXCLUDED.last_nudge_drafted_at), "
        " last_analyzed_at = GREATEST("
        "    conversation_state.last_analyzed_at, "
        "    EXCLUDED.last_analyzed_at), "
        " reengage_attempts = GREATEST("
        "    conversation_state.reengage_attempts, "
        "    EXCLUDED.reengage_attempts), "
        " followup_count = GREATEST("
        "    conversation_state.followup_count, "
        "    EXCLUDED.followup_count), "
        " updated_at = now(); "
        f"DELETE FROM conversation_state WHERE customer_id = {lit(d)}; "
        # 3. SET merged_into pointer on duplicate row
        "UPDATE customer_facts SET "
        f"merged_into = {lit(c)}, updated_at = now() "
        f"WHERE customer_id = {lit(d)}; "
        "COMMIT;"
    )
    out, rc = psql(tx)
    return rc == 0, out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", action="store_true",
                    help="show merge plan; do not apply")
    ap.add_argument("--apply", action="store_true",
                    help="execute the merges")
    args = ap.parse_args()

    if not (args.plan or args.apply):
        sys.exit("usage: --plan | --apply")

    pairs = find_duplicates()
    print(f"=== {len(pairs)} duplicate pair(s) to merge ===\n")
    if not pairs:
        return
    for p in pairs:
        plan = merge_plan(p)
        if plan is None:
            print(f"SKIP {p['name']!r}: row fetch failed")
            continue
        print(f"━━━ {p['name']} ━━━")
        print(f"  canonical: {p['canonical']}"
              f" ({plan['canonical']['message_count']} msgs, "
              f"label={plan['canonical']['label']})")
        print(f"  duplicate: {p['duplicate']}"
              f" ({plan['duplicate']['message_count']} msgs, "
              f"label={plan['duplicate']['label']})")
        if p.get("extra_cids"):
            print(f"  extra (will need re-run): {p['extra_cids']}")
        print(f"  → merged name: {plan['merged']['name']!r}")
        print(f"  → merged yachts: {plan['merged']['yachts']!r}")
        print(f"  → merged dates: {plan['merged']['dates']!r}")
        print(f"  → merged msgs: {plan['merged']['message_count']}")
        if args.apply:
            ok, out = apply_merge(p, plan)
            if ok:
                print(f"  ✓ MERGED")
            else:
                print(f"  ✗ FAILED: {(out or '')[:300]}")
        print()


if __name__ == "__main__":
    main()
