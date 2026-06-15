#!/usr/bin/env python3
"""Read-only recycled-LID audit — Stage 0 of the phone-keyed identity cure
(docs/identity-phone-keyed-plan.md).

Finds active @lid rows whose live WAHA phone resolution points at a DIFFERENTLY-
NAMED existing @c.us customer — the Qurbani/Antonio recycled-LID pattern, where
WhatsApp recycled an old LID onto a different person's number so their messages
land on a stale-named row. NO WRITES. Capped WAHA calls.

Run on the box:
  cd ~/hermes-bridge && set -a && . ./.env && set +a && python3 /tmp/recycled_lid_audit.py
"""
import os
import sys

sys.path.insert(0, os.path.expanduser("~/hermes-bridge"))
from db import _psql            # noqa: E402
from waha import lid_to_cus     # noqa: E402

CAP = int(os.environ.get("AUDIT_CAP", "50"))
US = "\x1f"
_PLACEHOLDER = {"", "unknown", "unknown customer", "customer", "guest",
                "there", "client", "lead", ".", "n/a", "na", "none"}


def _real_name(n):
    n = " ".join((n or "").strip().lower().split())
    if not n or n in _PLACEHOLDER:
        return False
    return not (n.startswith("+") or n.replace(" ", "").lstrip("+").isdigit())


def _q(s):
    return s.replace("'", "''")


out, _ = _psql(
    "SELECT customer_id || '" + US + "' || COALESCE(name,'') || '" + US +
    "' || COALESCE(yachts,'') FROM customer_facts "
    "WHERE customer_id LIKE '%@lid' AND merged_into IS NULL "
    "AND label <> 'DISREGARDED' "
    "ORDER BY updated_at DESC LIMIT " + str(CAP))
rows = []
for ln in (out or "").strip().splitlines():
    p = ln.split(US)
    if len(p) >= 2:
        rows.append((p[0].strip(), p[1].strip(),
                     (p[2] if len(p) > 2 else "").strip()))

suspects = []
for lid, lname, lyacht in rows:
    try:
        cus = lid_to_cus(lid)
    except Exception:
        continue
    if not cus.endswith("@c.us") or cus == lid:
        continue
    cout, _e = _psql(
        "SELECT COALESCE(name,'') || '" + US + "' || COALESCE(merged_into,'') "
        "FROM customer_facts WHERE customer_id = '" + _q(cus) + "'")
    cline = (cout or "").strip()
    if not cline:
        continue  # no separate @c.us row → normal (will merge cleanly)
    cname = cline.split(US)[0].strip()
    merged = cline.split(US)[1].strip() if US in cline else ""
    # recycled-LID candidate: the @lid and its resolved @c.us carry DISTINCT
    # real names (= the @lid's id now belongs to a different person) AND they
    # are NOT already unified (the @c.us isn't merged into this @lid). The
    # already-merged name-variant case (Shane/Shanebabu) is benign, not recycled.
    if (_real_name(lname) and _real_name(cname)
            and lname.strip().lower() != cname.strip().lower()
            and merged != lid):
        suspects.append((lid, lname, cus, cname, merged, lyacht))

print("Audited %d active @lid rows (cap %d). RECYCLED-LID SUSPECTS: %d"
      % (len(rows), CAP, len(suspects)))
for lid, lname, cus, cname, merged, ly in suspects:
    tag = " [c.us already merged: %s]" % merged if merged else ""
    print("  WARN %s name=%r -> WAHA %s name=%r%s | @lid yachts: %s"
          % (lid, lname, cus, cname, tag, ly[:30]))
if not suspects:
    print("  none — no active @lid resolves to a differently-named @c.us")
