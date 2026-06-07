#!/usr/bin/env python3
"""build_known_customers.py — regenerate hermes-bridge/known_customers.json,
the draft-time phone -> {name, revenue_aed, n_bookings} lookup that recognises a
returning PAID customer (Step 2 pivot, 2026-06-07).

WHY a flat JSON, not customer_facts:  a box dry-run proved customer_facts holds
only the ~222 ACTIVE-lead rows, not the 1,383 all-time payers — so the prior
"upsert clean profiles into customer_facts" approach reached almost none of the
real payers. This builds a SIMPLE, self-contained lookup keyed by phone, so the
draft path can recognise any of the 1,383 audited payers regardless of whether
they currently have an active customer_facts row.

SOURCES (read-only):
  - clean_revenue_by_customer.csv  -> canonical phone, name, clean_revenue_aed
                                      (the 1,383 audited-CLEAN payers).
  - customer_audited_index.json    -> n_bookings per phone. n_bookings is mapped
                                      for EVERY phone a record owns
                                      (primary_phone + phones[]) so a CSV row
                                      keyed by any of a merged customer's numbers
                                      still resolves its booking count.

OUTPUT:  hermes-bridge/known_customers.json — a flat object
  { "<normalized-phone-digits>": {"name": str,
                                  "revenue_aed": <int|float|null>,
                                  "n_bookings": <int|null>}, ... }
Exactly the 1,383 CSV rows, exactly those 3 fields. Keys are normalized with the
PROVEN bridge normalizer (hermes_exclusion_guards.normalize_phone) so a draft-
time cid/phone resolves to the same key.

PII NOTE:  this file contains customer phone numbers + revenue. It is intended to
be .gitignored and shipped to the box OUT OF BAND (scp), NOT committed.

Usage:
  python3 scripts/build_known_customers.py            # regenerate the JSON
  python3 scripts/build_known_customers.py --csv ... --index ... --out ...
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BRIDGE = ROOT / "hermes-bridge"
sys.path.insert(0, str(BRIDGE))

# THE single proven normalizer (mirrors server._normalize_phone). Same function
# the draft-time lookup uses, so build-side and read-side keys always agree.
from hermes_exclusion_guards import normalize_phone  # noqa: E402

DEFAULT_CSV = Path.home() / "projects" / "whatsapp-classifier" / \
    "clean_revenue_by_customer.csv"
DEFAULT_INDEX = Path.home() / "projects" / "whatsapp-classifier" / \
    "customer_audited_index.json"
DEFAULT_OUT = BRIDGE / "known_customers.json"


def _to_num(v):
    """'994104' -> 994104 (int when whole), '' / non-numeric -> None."""
    s = (str(v or "")).strip().replace(",", "")
    if not s:
        return None
    try:
        f = float(s)
        return int(f) if f.is_integer() else f
    except ValueError:
        return None


def load_bookings_by_phone(path: Path):
    """{normalized_phone: n_bookings} from the audited index. Mapped for every
    phone a record owns (primary_phone + phones[]). Degrade-safe -> {} on error
    (n_bookings then falls to None for every row)."""
    out = {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:  # noqa: BLE001
        print(f"  ! index unreadable ({e!r}) -> n_bookings will be null")
        return out
    records = data.get("customers") if isinstance(data, dict) else data
    for rec in (records or []):
        if not isinstance(rec, dict):
            continue
        nb = rec.get("n_bookings")
        if nb is None:
            continue
        phones = list(rec.get("phones") or [])
        if rec.get("primary_phone"):
            phones.append(rec["primary_phone"])
        for p in phones:
            key = normalize_phone(str(p))
            if key:
                out.setdefault(key, nb)
    return out


def build(csv_path: Path, index_path: Path):
    """Return (mapping, stats). mapping = {phone: {name, revenue_aed,
    n_bookings}} for the 1,383 clean rows, keyed by the proven normalizer."""
    bookings = load_bookings_by_phone(index_path)
    mapping = {}
    rows = empty_keys = collisions = nb_hits = 0
    with open(csv_path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows += 1
            key = normalize_phone((r.get("phone") or "").strip())
            if not key:
                empty_keys += 1
                continue
            if key in mapping:
                collisions += 1  # keep the first row for a duplicate key
                continue
            nb = bookings.get(key)
            if nb is not None:
                nb_hits += 1
            mapping[key] = {
                "name": (r.get("name") or "").strip(),
                "revenue_aed": _to_num(r.get("clean_revenue_aed")),
                "n_bookings": nb,
            }
    stats = {"csv_rows": rows, "entries": len(mapping),
             "empty_keys": empty_keys, "collisions": collisions,
             "with_n_bookings": nb_hits,
             "index_phone_keys": len(bookings)}
    return mapping, stats


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--csv", default=str(DEFAULT_CSV))
    ap.add_argument("--index", default=str(DEFAULT_INDEX))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    args = ap.parse_args()

    print("build_known_customers")
    print(f"  csv:   {args.csv}")
    print(f"  index: {args.index}")
    print(f"  out:   {args.out}")

    mapping, stats = build(Path(args.csv), Path(args.index))
    # Deterministic output: sorted keys, UTF-8 names preserved.
    text = json.dumps(mapping, ensure_ascii=False, indent=2, sort_keys=True)
    Path(args.out).write_text(text + "\n", encoding="utf-8")

    print("\n=== STATS ===")
    for k in ("csv_rows", "entries", "with_n_bookings", "empty_keys",
              "collisions", "index_phone_keys"):
        print(f"  {k:<18} {stats[k]}")
    print(f"  bytes              {len(text.encode('utf-8')) + 1}")

    print("\n=== SAMPLE (first 3 by sorted key) ===")
    for key in list(sorted(mapping))[:3]:
        print(f"  {key} -> {mapping[key]}")
    print("\nWrote", args.out)
    print("PII: gitignore this file; ship to the box out-of-band (scp).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
