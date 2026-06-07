#!/usr/bin/env python3
"""cron-intake-gap.py — alert the operator to silently-dropped WhatsApp leads.

A lead reaches WAHA but the webhook can miss it (outage / container-IP drift),
leaving it absent from customer_facts (2026-06-06: Ayaan Nadeem sat invisible
~4 days). This cross-references a WAHA chat overview against the ingested
customer_ids (pure intake_gaps() detector) and Telegrams the operator any recent
un-ingested inbound leads so they can /refresh <id>. READ-ONLY + alert-only:
it NEVER auto-ingests old history. Config from ~/hermes-bridge/.env (same keys
as cron-daily-summary.py). Silent when there are no gaps (no operator spam)."""
import json
import os
import subprocess
import sys
import time
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
WAHA_KEY = ENV.get("WAHA_API_KEY", "")
MAX_AGE_DAYS = 7

sys.path.insert(0, os.path.join(HOME, "hermes-bridge"))
from intake import intake_gaps  # noqa: E402


def waha_base():
    """Live WAHA container IP (drift-proof, mirrors waha.py); caddy URL fallback."""
    try:
        r = subprocess.run(
            ["docker", "inspect", "-f",
             "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
             "n8n-waha-1"], capture_output=True, text=True, timeout=10)
        ip = r.stdout.strip()
        if ip:
            return "http://%s:3000" % ip
    except Exception as e:
        print("waha ip resolve err:", repr(e))
    return "https://waha.13-63-82-112.sslip.io"


def cf_ids():
    try:
        r = subprocess.run(
            ["docker", "exec", PG, "psql", "-U", "hermes_rw", "-d", "n8n",
             "-tA", "-c", "SELECT customer_id FROM customer_facts"],
            capture_output=True, text=True, timeout=25)
        if r.returncode != 0:
            print("psql failed:", r.stderr.strip()[:150])
            return set()
        return set(x.strip() for x in r.stdout.splitlines() if x.strip())
    except Exception as e:
        print("psql error:", repr(e))
        return set()


def waha_chats():
    url = waha_base().rstrip("/") + "/api/default/chats/overview?limit=150"
    req = urllib.request.Request(url, headers={"X-Api-Key": WAHA_KEY})
    with urllib.request.urlopen(req, timeout=40) as resp:
        return json.load(resp)


def tg_send(text):
    data = urllib.parse.urlencode({"chat_id": CHAT_ID, "text": text}).encode()
    req = urllib.request.Request(
        "https://api.telegram.org/bot%s/sendMessage" % TG_TOKEN, data=data)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.load(resp).get("ok") is True
    except Exception as e:
        print("telegram send error:", repr(e))
        return False


def main():
    try:
        chats = waha_chats()
    except Exception as e:
        print("waha fetch error:", repr(e))
        return
    gaps = intake_gaps(chats, cf_ids(), time.time(), MAX_AGE_DAYS)
    print("intake-gap %s: %d dropped lead(s)" %
          (time.strftime("%Y-%m-%d %H:%M"), len(gaps)))
    if not gaps:
        return  # silent — no operator spam when nothing is dropped
    lines = ["⚠️ WhatsApp leads not ingested (webhook miss) "
             "— /refresh <id> to recover:"]
    for g in gaps[:15]:
        nm = g["name"] or "(no name)"
        lines.append("- %s | %s | %sd | %s" %
                     (g["cid"], nm, g["age_days"], g["body"][:50]))
    if len(gaps) > 15:
        lines.append("...and %d more" % (len(gaps) - 15))
    if tg_send("\n".join(lines)):
        print("alerted operator: %d gap(s)" % len(gaps))


if __name__ == "__main__":
    main()
