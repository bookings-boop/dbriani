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
BRIDGE_TOKEN = ENV.get("BRIDGE_TOKEN", "")
BRIDGE_URL = ENV.get("BRIDGE_URL", "http://localhost:8788")


def bridge_post(path, body):
    """Best-effort POST to the local bridge. Returns parsed dict or {}.
    NEVER raises — the daily digest must send regardless of sweep outcome."""
    try:
        req = urllib.request.Request(
            f"{BRIDGE_URL}{path}", data=json.dumps(body).encode(),
            method="POST",
            headers={"Content-Type": "application/json",
                     "X-Bridge-Token": BRIDGE_TOKEN})
        with urllib.request.urlopen(req, timeout=90) as resp:
            return json.load(resp) or {}
    except Exception as e:
        print(f"bridge_post {path} error:", repr(e))
        return {}


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

    # 📨 Awaiting YOUR reply — customers who messaged after our last outbound
    # (reply or nudge), or we never replied. Same "unanswered on top" rule as
    # /review (operator 2026-05-31). Leads the digest; active leads first, then
    # CONFIRMED post-booking messages; longest-waiting first within each group.
    awaiting = psql(
        "SELECT COALESCE(NULLIF(name,''), "
        "CASE WHEN customer_id LIKE '%@c.us' "
        "THEN '+'||split_part(customer_id,'@',1) "
        "ELSE 'WhatsApp lead ••'||right(split_part(customer_id,'@',1),4) END), "
        "label, "
        "GREATEST(0, EXTRACT(epoch FROM now()-last_customer_message_at))::bigint "
        "FROM v_lead_summary "
        "WHERE last_customer_message_at IS NOT NULL "
        "AND label <> 'DISREGARDED' AND label NOT LIKE 'PAUSED_%' "
        "AND last_customer_message_at > GREATEST("
        "COALESCE(last_operator_reply_at,'epoch'::timestamptz),"
        "COALESCE(last_nudge_drafted_at,'epoch'::timestamptz)) "
        "ORDER BY (label='CONFIRMED'), last_customer_message_at ASC LIMIT 12")
    if awaiting:
        L.append(f"\U0001f4e8 Awaiting YOUR reply: {len(awaiting)} "
                 f"— answer these first")
        for who, lab, wait in ((x + ["", "", ""])[:3] for x in awaiting):
            try:
                sec = int(wait)
                hrs = sec // 3600
                wtxt = (f"{hrs // 24}d" if hrs >= 24
                        else f"{hrs}h" if hrs else f"{max(1, sec // 60)}m")
            except (ValueError, TypeError):
                wtxt = "?"
            L.append(f"  • {who} [{lab}] — waiting {wtxt}")
        L.append("")

    rules = psql("SELECT rule_text, scope, active FROM behavior_rules "
                 "WHERE created_at > now() - interval '24 hours' "
                 "ORDER BY created_at")
    L.append(f"\U0001f9e0 Behavior rules captured (24h): {len(rules)}")
    for rt, sc, act in ((x + ["", "", ""])[:3] for x in rules[:10]):
        flag = "active" if act == "t" else "INACTIVE — needs your approval"
        L.append(f"  • [{sc}] {rt}  ({flag})")
    _ac = psql("SELECT count(*) FROM behavior_rules WHERE active=true")
    _nac = int(_ac[0][0]) if (_ac and _ac[0] and _ac[0][0].isdigit()) else 0
    L.append(f"  → {_nac} active rules total"
             + ("  ⚠️ getting large — ask Claude to consolidate" if _nac > 40 else ""))
    L.append("")

    trig = psql("SELECT COALESCE(NULLIF(customer_name,''),customer_id), "
                "trigger_type, COALESCE(trigger_context,'') FROM customer_triggers "
                "WHERE status='pending' ORDER BY reminder_date LIMIT 12")
    L.append(f"⏰ Pending follow-ups: {len(trig)}")
    for who, tt, ctx in ((x + ["", "", ""])[:3] for x in trig):
        L.append(f"  • {who} — {tt}: {ctx}")
    L.append("")

    # Live "needs attention" list from customer_facts (the authoritative
    # pipeline table) — NOT the legacy conversation_health table, which had
    # gone stale + test-seeded (fake Rajesh/James fixtures were showing in
    # the digest, 2026-05-30). Resolve a human label: real name → formatted
    # phone for @c.us → short WhatsApp-lead handle for @lid; never a raw id.
    risk = psql(
        "SELECT COALESCE(NULLIF(name,''), "
        "CASE WHEN customer_id LIKE '%@c.us' "
        "THEN '+'||split_part(customer_id,'@',1) "
        "ELSE 'WhatsApp lead ••'||right(split_part(customer_id,'@',1),4) END), "
        "label, "
        "COALESCE(NULLIF(suggested_action,''),"
        "LEFT(COALESCE(importance_reasoning,''),110)) "
        "FROM customer_facts "
        "WHERE label IN ('HOT','NEEDS_ATTENTION','WARM') "
        "AND merged_into IS NULL "
        "ORDER BY importance_score DESC NULLS LAST, label LIMIT 10")
    L.append(f"⚠️ Active leads needing attention: {len(risk)}")
    for who, lab, note in ((x + ["", "", ""])[:3] for x in risk):
        L.append(f"  • {who} [{lab}] — {note}")
    L.append("")

    # Resolve a human name (never a raw @lid) for the approval-mode alert.
    modes = psql(
        "SELECT DISTINCT ON (cm.customer_id) "
        "COALESCE(NULLIF(cf.name,''), "
        "CASE WHEN cm.customer_id LIKE '%@c.us' "
        "THEN '+'||split_part(cm.customer_id,'@',1) "
        "ELSE 'WhatsApp lead ••'||right(split_part(cm.customer_id,'@',1),4) END), "
        "cm.mode "
        "FROM conversation_modes cm "
        "LEFT JOIN customer_facts cf ON cf.customer_id = cm.customer_id "
        "ORDER BY cm.customer_id, cm.id DESC")
    non_appr = [m for m in modes if (m[1] if len(m) > 1 else "") != "approval"]
    if non_appr:
        L.append(f"\U0001f501 Conversations NOT in approval mode: {len(non_appr)}")
        for m in non_appr:
            L.append(f"  • {m[0]} — {m[1] if len(m) > 1 else '?'}")
        L.append("")

    caps = psql("SELECT kind, count(*) FROM autonomous_sends WHERE sent_at >= "
                "date_trunc('day', now() AT TIME ZONE 'Asia/Dubai') "
                "AT TIME ZONE 'Asia/Dubai' GROUP BY kind")
    cap_n = {c[0]: c[1] for c in caps if len(c) >= 2}
    L.append(f"🧮 Autonomous caps (today): {cap_n.get('auto','0')} auto-sent, "
             f"{cap_n.get('checkpoint','0')} checkpoints, "
             f"{cap_n.get('intervention','0')} operator interventions")
    L.append("")

    # #5-auto / #6-auto-A daily auto-sweeps (2026-06-01). Run the graceful-
    # close (passed-date → dormant after 2 re-engage attempts + 7d silence,
    # reversible) and the post-trip feedback-card sweep as part of the morning
    # cron. Best-effort — bridge_post never raises, so the digest still sends.
    _dorm = bridge_post("/dormancy-sweep", {})
    _fb = bridge_post("/daily-feedback-sweep", {})
    _dn = _dorm.get("count", 0) if isinstance(_dorm, dict) else 0
    _fn = _fb.get("count", 0) if isinstance(_fb, dict) else 0
    if _dn or _fn:
        L.append(f"🤖 Auto: {_dn} passed-date lead(s) closed (reversible via "
                 f"/label), {_fn} post-trip feedback card(s) posted")
        L.append("")

    L.append("— Hermes")
    msg = "\n".join(L)[:4000]
    print("daily summary sent" if tg_send(msg) else "daily summary send FAILED")


if __name__ == "__main__":
    main()
