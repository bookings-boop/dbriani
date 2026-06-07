#!/usr/bin/env python3
"""cron-reminders.py — deliver due customer-trigger reminders (Step 6).

Run every 15 minutes by system cron. For each `customer_triggers` row that is
status='pending' and past its reminder_date, sends a Telegram reminder to the
operator and marks the row status='reminded' (so it fires once).

Quiet hours: 22:00–08:00 Asia/Dubai — the run is skipped; pending triggers
keep until the next run after 08:00. Config from ~/hermes-bridge/.env:
  ADMIN_TG_TOKEN, ADMIN_CHAT_ID, BRIDGE_PG_CONTAINER (optional).

Stdlib only.
"""
import datetime
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request

HOME = os.path.expanduser("~")
ENV = {}
try:
    for ln in open(os.path.join(HOME, "hermes-bridge", ".env")):
        ln = ln.strip()
        if "=" in ln and not ln.startswith("#"):
            k, v = ln.split("=", 1)
            ENV[k] = v
except FileNotFoundError:
    pass

TG_TOKEN = ENV.get("ADMIN_TG_TOKEN", "")
CHAT_ID = ENV.get("ADMIN_CHAT_ID", "")
PG = ENV.get("BRIDGE_PG_CONTAINER", "n8n-postgres-1")
TS = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def out(*a):
    print(TS, *a, flush=True)


def quiet_hours():
    """True during 22:00–08:00 Asia/Dubai (UTC+4)."""
    dubai = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=4)
    return dubai.hour >= 22 or dubai.hour < 8


def psql(sql):
    r = subprocess.run(
        ["docker", "exec", PG, "psql", "-U", "hermes_rw", "-d", "n8n",
         "-tA", "-F", "\t", "-c", sql],
        capture_output=True, text=True, timeout=20)
    return r.stdout, r.returncode, r.stderr


def tg_send(text):
    data = urllib.parse.urlencode({"chat_id": CHAT_ID, "text": text}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data=data)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.load(resp).get("ok") is True
    except Exception as e:
        out("telegram send error:", repr(e))
        return False


def main():
    if not TG_TOKEN or not CHAT_ID:
        out("ABORT: ADMIN_TG_TOKEN / ADMIN_CHAT_ID not set in ~/hermes-bridge/.env")
        sys.exit(1)
    if quiet_hours():
        out("quiet hours (Dubai) — skipping; pending triggers keep till morning")
        return

    # Audit #9/#21 (2026-06-07): join customer_facts so we (a) SUPPRESS
    # reminders for leads now terminal (LOST/DISREGARDED/CONFIRMED), paused, or
    # merged away — a trigger saved while a lead was active must not fire after
    # it's closed/merged (or, worse, ping a Layer-3 block-listed contact) — and
    # (b) show the CURRENT canonical name (post-/name), not the frozen snapshot.
    rows_out, rc, err = psql(
        "SELECT t.id, COALESCE(NULLIF(cf.name,''), t.customer_name), "
        "t.customer_id, t.trigger_type, COALESCE(t.trigger_context,'') "
        "FROM customer_triggers t "
        "LEFT JOIN customer_facts cf ON cf.customer_id = t.customer_id "
        "WHERE t.status='pending' AND t.reminder_date <= now() "
        "  AND COALESCE(cf.merged_into,'') = '' "
        "  AND (cf.label IS NULL OR (cf.label NOT IN "
        "       ('LOST','DISREGARDED','CONFIRMED') "
        "       AND cf.label NOT LIKE 'PAUSED_%')) "
        "ORDER BY t.reminder_date LIMIT 20")
    if rc != 0:
        out("trigger query failed:", err.strip()[:200])
        sys.exit(1)
    rows = [ln.split("\t") for ln in rows_out.splitlines() if ln.strip()]
    if not rows:
        out("no due triggers")
        return

    sent = 0
    for r in rows:
        r = (r + ["", "", "", "", ""])[:5]
        tid, cname, cid, ttype, ctx = r
        who = cname.strip() or cid.strip() or "a customer"
        msg = (f"⏰ Follow-up due\n\n{who}\n{ttype}: {ctx}\n\n"
               "(auto-detected from their message — handle it in WhatsApp)")
        if tg_send(msg):
            psql(f"UPDATE customer_triggers SET status='reminded' WHERE id={int(tid)}")
            sent += 1
            out(f"reminded trigger {tid} ({ttype})")
        else:
            out(f"send failed for trigger {tid} — left pending")
    out(f"done — {sent}/{len(rows)} reminders sent")


if __name__ == "__main__":
    main()
