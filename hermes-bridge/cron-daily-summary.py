#!/usr/bin/env python3
"""cron-daily-summary.py — daily operator digest (Step 9).

Run once a day at 08:00 Asia/Dubai (= 04:00 UTC) by system cron. Summarises:
behavior rules captured in the last 24h, pending follow-up triggers, at-risk
conversations, and any conversation not in `approval` mode. Sends to the
operator via Telegram. Config from ~/hermes-bridge/.env.

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


def psql(sql):
    """Run a SELECT via docker exec; rows as lists of tab-split fields."""
    try:
        r = subprocess.run(
            ["docker", "exec", PG, "psql", "-U", "hermes_rw", "-d", "n8n",
             "-tA", "-F", "\t", "-c", sql],
            capture_output=True, text=True, timeout=25)
        if r.returncode != 0:
            print("query failed:", r.stderr.strip()[:150])
            return []
        return [ln.split("\t") for ln in r.stdout.splitlines() if ln.strip()]
    except Exception as e:
        print("query error:", repr(e))
        return []


def tg_send(text):
    data = urllib.parse.urlencode({"chat_id": CHAT_ID, "text": text}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data=data)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.load(resp).get("ok") is True
    except Exception as e:
        print("telegram send error:", repr(e))
        return False


def main():
    if not TG_TOKEN or not CHAT_ID:
        print("ABORT: ADMIN_TG_TOKEN / ADMIN_CHAT_ID not set")
        sys.exit(1)
    dubai = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=4)
    L = [f"☀️ Dubriani — daily digest ({dubai:%a %d %b})", ""]

    rules = psql("SELECT rule_text, scope, active FROM behavior_rules "
                 "WHERE created_at > now() - interval '24 hours' "
                 "ORDER BY created_at")
    L.append(f"\U0001f9e0 Behavior rules captured (24h): {len(rules)}")
    for rt, sc, act in (r + ["", "", ""] for r in (x[:3] for x in rules[:10])):
        flag = "active" if act == "t" else "INACTIVE — needs your approval"
        L.append(f"  • [{sc}] {rt}  ({flag})")
    L.append("")

    trig = psql("SELECT COALESCE(NULLIF(customer_name,''),customer_id), "
                "trigger_type, COALESCE(trigger_context,'') FROM customer_triggers "
                "WHERE status='pending' ORDER BY reminder_date LIMIT 12")
    L.append(f"⏰ Pending follow-ups: {len(trig)}")
    for who, tt, ctx in (t + ["", "", ""] for t in (x[:3] for x in trig)):
        L.append(f"  • {who} — {tt}: {ctx}")
    L.append("")

    risk = psql("SELECT COALESCE(NULLIF(customer_name,''),customer_id), score, "
                "COALESCE(reason,'') FROM conversation_health "
                "WHERE score IN ('at_risk','cold') ORDER BY updated_at DESC LIMIT 12")
    L.append(f"⚕️ At-risk conversations: {len(risk)}")
    for who, sc, rs in (h + ["", "", ""] for h in (x[:3] for x in risk)):
        L.append(f"  • {who} [{sc}] — {rs}")
    L.append("")

    modes = psql("SELECT DISTINCT ON (customer_id) customer_id, mode "
                 "FROM conversation_modes ORDER BY customer_id, id DESC")
    non_appr = [m for m in modes if (m[1] if len(m) > 1 else "") != "approval"]
    if non_appr:
        L.append(f"\U0001f501 Conversations NOT in approval mode: {len(non_appr)}")
        for m in non_appr:
            L.append(f"  • {m[0]} — {m[1] if len(m) > 1 else '?'}")
        L.append("")

    L.append("— Hermes")
    msg = "\n".join(L)[:4000]
    print("daily summary sent" if tg_send(msg) else "daily summary send FAILED")


if __name__ == "__main__":
    main()
