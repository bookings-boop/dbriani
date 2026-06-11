#!/usr/bin/env python3
"""Weekly quote-mix report — the quote-routing experiment's ground-truth
metric on REAL traffic (read-only). Counts yacht quote-events in OUTBOUND
messages (durable store conversation_messages, live since 2026-06-07) and
reports band shares: Princess 60/Bella vs Bliss 55 in the budget band,
Haigan vs Zirve in the 4,500 band, and Satoshi rate discipline (sub-2,000
quotes = policy violations; 1,500 allowed only as the morning value option).

Run from repo root:  python3 scripts/quote_mix_report.py [days]
"""
import json
import re
import subprocess
import sys

DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 7
PROXIMITY = 80  # chars after a yacht name in which a rate counts as its quote

YACHTS = [  # live catalog + key legacy names (detection lexicon)
    "princess 60", "bella 125", "bella", "diana 50", "elise 50", "elan 44",
    "élan 44", "novia 55", "zenith 64", "von dutch", "bliss 55", "azimut 62",
    "eva 60", "cabo 77", "azimut 50", "belle 75", "sunseeker 88", "azimut 79",
    "pershing 5x", "monaco 60", "satoshi", "ferretti 670", "cante",
    "carina 75", "azimut 70", "azimut 77", "haigan", "zirve 72", "azimut 88",
    "luna 101", "galeon 780", "notorious", "benetti 120", "asya 110",
    "ferretti 780", "pershing 82", "royal mirage", "zeta 100", "eclipse 90",
    "tatti", "sapphire 150", "dolce vita", "riva 82", "baglietto",
    "princess x95", "lamborghini", "odysea", "aurora 130", "sunseeker 131",
    "royalty", "saffuriya", "thunder", "mila 141", "athena 170",
    "skyfall", "finesse", "sofiya", "solana 95",
]
RATE_RE = re.compile(r"([\d][\d,]{2,})\s*(?:AED)?\s*/\s*(?:hr|hour)", re.I)

SQL = ("SELECT COALESCE(json_agg(json_build_object('d', ts::date, 'c', "
       "customer_id, 'b', body)), '[]'::json) FROM conversation_messages "
       f"WHERE direction='out' AND created_at >= now() - interval '{DAYS} days';")
raw = subprocess.run(
    ["ssh", "dubriani-ec2",
     "docker exec n8n-postgres-1 psql -U n8n -d n8n -tA -c \"" + SQL + "\""],
    capture_output=True, text=True, timeout=120).stdout.strip()
rows = json.loads(raw) if raw else []
print(f"=== QUOTE-MIX REPORT — last {DAYS} days, {len(rows)} outbound messages "
      f"(durable store) ===")

def band(rate):
    if rate <= 1500: return "budget ≤1,500"
    if rate < 3000: return "1,501–2,999"
    if rate <= 4400: return "3,000–4,400"
    if rate <= 5500: return "4,500–5,500"
    if rate < 9000: return "6,000–8,999"
    return "VIP 9,000+"

events = []   # (yacht, rate, date, cid)
for r in rows:
    body = r.get("b") or ""
    low = body.lower()
    marks = []
    for y in YACHTS:
        start = 0
        while True:
            i = low.find(y, start)
            if i < 0: break
            marks.append((i, y)); start = i + 1
    # longest-name-wins on overlapping positions (bella vs bella 125)
    marks.sort(key=lambda m: (m[0], -len(m[1])))
    dedup, taken = [], set()
    for pos, y in marks:
        if pos not in taken:
            dedup.append((pos, y)); taken.add(pos)
    for pos, y in dedup:
        window = body[pos:pos + len(y) + PROXIMITY]
        m = RATE_RE.search(window)
        if m:
            rate = int(m.group(1).replace(",", ""))
            if 200 <= rate <= 200000:
                events.append((y, rate, r.get("d"), r.get("c")))

from collections import Counter, defaultdict
per_yacht = Counter(e[0] for e in events)
per_band = defaultdict(Counter)
for y, rate, _, _ in events:
    per_band[band(rate)][y] += 1

print(f"\nquote-events detected: {len(events)}")
print("\n--- per yacht ---")
for y, n in per_yacht.most_common():
    print(f"  {y:<16} {n}")
print("\n--- band shares ---")
for b in sorted(per_band):
    tot = sum(per_band[b].values())
    tops = ", ".join(f"{y} {n}/{tot}" for y, n in per_band[b].most_common(4))
    print(f"  {b:<14} n={tot}: {tops}")

bud = per_band.get("budget ≤1,500", Counter())
pb = bud.get("princess 60", 0) + bud.get("bella", 0)
bl = bud.get("bliss 55", 0)
tot = sum(bud.values())
print(f"\n⭐ KPI-1 Princess60+Bella share of budget band: {pb}/{tot}"
      + (f" ({pb/tot:.0%})" if tot else ""))
print(f"   Bliss 55 share of budget band: {bl}/{tot}"
      + (f" ({bl/tot:.0%})" if tot else ""))
h = per_yacht.get("haigan", 0); z = per_yacht.get("zirve 72", 0)
print(f"⭐ KPI-2 Haigan vs Zirve 72 quotes: {h} vs {z}")
sat = [rate for y, rate, _, _ in events if y == "satoshi"]
viol = [r for r in sat if r < 2000 and r != 1500]
low15 = [r for r in sat if r == 1500]
print(f"⭐ KPI-3 Satoshi quotes: {len(sat)} total | at 1,500 (must be "
      f"budget-triggered — spot-check): {len(low15)} | BELOW FLOOR/off-policy "
      f"(<2,000, ≠1,500): {len(viol)}{' ⚠️' if viol else ''}")
anchor = sum(1 for r in rows if "1,100" in (r.get("b") or "")
             and "bliss" in (r.get("b") or "").lower())
print(f"⭐ KPI-4 Bliss 1,100 anchor mentions in outbound: {anchor}")
