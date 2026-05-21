#!/usr/bin/env python3
"""
build_reply_to_claude.py - operator-text-goes-to-Claude rule.

Re-scopes the reply-to-a-draft-card behaviour in Process Text Reply:

  BEFORE: reply to a draft card with text  ->  text sent verbatim to customer
  AFTER : reply to a draft card with text  ->  feedback to Claude (refine);
          the draft is re-drafted and the card updated. Nothing reaches the
          customer until Send.
          Exceptions (explicit operator directives):
            "send this: X"  -> send X verbatim to the customer
            "ask this: X"   -> refine with the instruction to ask X

(PDF-attachment handling — "here is the pdf attached" -> Claude drafts from the
PDF — is a separate follow-up; it needs Telegram document download.)

Patches one node (Process Text Reply), backs up, PUTs, verifies.

Usage:  python3 scripts/build_reply_to_claude.py
"""
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SSH = ["ssh", "-o", "ConnectTimeout=30", "dubriani-ec2"]
API = None
WF_NAME = "Dubriani Phase 1B — WhatsApp + Telegram Approval"
ALLOWED_SETTINGS = {"saveExecutionProgress", "saveManualExecutions",
                    "saveDataErrorExecution", "saveDataSuccessExecution",
                    "executionTimeout", "errorWorkflow", "timezone",
                    "executionOrder"}

OLD = """// (A) reply to a draft card with text -> send that text to the customer
const r = msg.reply_to_message;
if (r && r.message_id) {
  // newest-match: telegram message_ids collide across bot/session resets
  let d = null;
  for (let _i = queue.length - 1; _i >= 0; _i--) {
    if (queue[_i].telegram_message_id === r.message_id) { d = queue[_i]; break; }
  }
  if (d && text) {
    return { json: { action: 'send_to_customer', origin: 'reply',
      target_chatId: d.customer_phone, final_text: text, draft_id: d.id,
      admin_chat_id: adminChatId } };
  }
}"""

NEW = """// (A) reply to a draft card — DEFAULT: feedback to Claude (refine). Only an
//     explicit "send this: X" sends X verbatim; "ask this: X" -> refine to ask X.
const r = msg.reply_to_message;
if (r && r.message_id) {
  // newest-match: telegram message_ids collide across bot/session resets
  let d = null;
  for (let _i = queue.length - 1; _i >= 0; _i--) {
    if (queue[_i].telegram_message_id === r.message_id) { d = queue[_i]; break; }
  }
  if (d && text) {
    const lo = text.toLowerCase();
    if (lo.indexOf('send this:') === 0) {
      // explicit directive — send the operator's text verbatim
      return { json: { action: 'send_to_customer', origin: 'reply',
        target_chatId: d.customer_phone, final_text: text.slice(10).trim(),
        draft_id: d.id, admin_chat_id: adminChatId } };
    }
    // everything else is FEEDBACK FOR CLAUDE — refine the draft, never auto-send
    let fb = text;
    if (lo.indexOf('ask this:') === 0) {
      fb = 'Ask the customer: ' + text.slice(9).trim();
    }
    d.status = 'pending';
    return { json: { action: 'refine', draft_id: d.id, feedback: fb,
      admin_chat_id: adminChatId } };
  }
}"""


def die(m):
    sys.exit(f"x {m}")


def load_key():
    for ln in (ROOT / ".env").read_text().splitlines():
        if ln.startswith("N8N_API_KEY="):
            v = ln.split("=", 1)[1].strip().strip("'\"")
            if v:
                return v
    die("N8N_API_KEY not in .env")


def ssh_run(script, stdin, label, timeout=60):
    last = ""
    for attempt in range(1, 11):
        r = None
        try:
            r = subprocess.run(SSH + [script], input=stdin, capture_output=True,
                               text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            last = f"timed out after {timeout}s"
        if r is not None:
            if r.returncode == 0:
                return r.stdout
            if r.returncode != 255:
                die(f"{label}: ssh exit {r.returncode}\n{r.stderr[:400]}")
            last = f"ssh exit 255 ({r.stderr.strip()[:100]})"
        if attempt < 10:
            print(f"   [{label}] connect attempt {attempt} failed: {last} — retry 5s")
            time.sleep(5)
    die(f"{label}: connection failed after 10 attempts — {last}")


def ssh_upload(data, remote, label):
    for attempt in range(1, 11):
        r = None
        try:
            r = subprocess.run(SSH + [f"cat > {remote}"], input=data,
                               capture_output=True, timeout=90)
        except subprocess.TimeoutExpired:
            r = None
        if r is not None and r.returncode == 0:
            return
        if attempt < 10:
            print(f"   [{label}] upload attempt {attempt} failed — retry 5s")
            time.sleep(5)
    die(f"{label}: upload failed after 10 attempts")


def resolve_base():
    out = ssh_run("docker inspect n8n-n8n-1 --format "
                  "'{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}'",
                  "", "resolve-ip", timeout=45)
    ip = out.strip()
    if not ip:
        die("could not resolve n8n container IP")
    return f"http://{ip}:5678/api/v1"


def as_json(t, label):
    try:
        return json.loads(t)
    except Exception:
        die(f"{label}: non-JSON response:\n{t[:500]}")


def main():
    key = load_key()
    global API
    API = resolve_base()
    print(f"=== build_reply_to_claude.py ===\n1. n8n API: {API}")

    items = (as_json(ssh_run(
        f'read -r K; curl -s -m25 -H "X-N8N-API-KEY: $K" {API}/workflows',
        key, "list"), "list").get("data") or [])
    target = next((w for w in items if w.get("name") == WF_NAME), None)
    if not target:
        die(f"workflow {WF_NAME!r} not found")
    wf_id = target["id"]
    wf = as_json(ssh_run(
        f'read -r K; curl -s -m25 -H "X-N8N-API-KEY: $K" {API}/workflows/{wf_id}',
        key, "get"), "get")
    was_active = bool(wf.get("active"))
    print(f"2. live workflow {wf_id}: {len(wf['nodes'])} nodes, active={was_active}")

    ptr = next((n for n in wf["nodes"] if n["name"] == "Process Text Reply"), None)
    if not ptr:
        die("'Process Text Reply' not found")
    js = ptr["parameters"].get("jsCode", "")
    if "feedback to Claude (refine)" in js and "send this:" in js:
        print("3. already applied — reply-to-card already routes to Claude")
        return
    if OLD not in js:
        die("the reply-to-card block does not match expectation — not patching blindly")
    ptr["parameters"]["jsCode"] = js.replace(OLD, NEW, 1)
    print("3. patched Process Text Reply — reply-to-card now routes to Claude (refine)")

    ts = time.strftime("%Y%m%d_%H%M%S")
    bk = ROOT / "workflows" / f"phase-1b-telegram.PRE-REPLYCLAUDE-{ts}.json"
    bk.write_text(json.dumps(wf, indent=2, ensure_ascii=False) + "\n")
    print(f"4. backed up live -> {bk.name}")

    put = {
        "name": wf.get("name", WF_NAME),
        "nodes": wf["nodes"],
        "connections": wf["connections"],
        "settings": {k: v for k, v in (wf.get("settings") or {}).items()
                     if k in ALLOWED_SETTINGS},
    }
    if isinstance(wf.get("staticData"), (dict, str)) and wf.get("staticData"):
        put["staticData"] = wf["staticData"]
    ssh_upload((json.dumps(put, ensure_ascii=False) + "\n").encode("utf-8"),
               "/tmp/rtc_wf.json", "upload")
    res = None
    for attempt in range(1, 13):
        out = ssh_run(
            f'read -r K; curl -s -m90 -X PUT -H "X-N8N-API-KEY: $K" '
            f'-H "Content-Type: application/json" --data-binary @/tmp/rtc_wf.json '
            f'{API}/workflows/{wf_id}', key, "PUT", timeout=120)
        res = as_json(out, "PUT")
        if res.get("id") == wf_id:
            break
        if "unauthorized" in out.lower() and attempt < 12:
            print(f"   PUT attempt {attempt}: transient unauthorized — retry in 12s")
            time.sleep(12)
            continue
        ssh_run("rm -f /tmp/rtc_wf.json", "", "cleanup")
        die(f"PUT failed: {out[:300]}")
    ssh_run("rm -f /tmp/rtc_wf.json", "", "cleanup")
    print(f"5. PUT OK ({len(res.get('nodes', []))} nodes)")

    if was_active:
        ssh_run(f'read -r K; curl -s -m25 -X POST -H "X-N8N-API-KEY: $K" '
                f'{API}/workflows/{wf_id}/activate', key, "activate")
        print("6. re-activated")

    final = as_json(ssh_run(
        f'read -r K; curl -s -m25 -H "X-N8N-API-KEY: $K" {API}/workflows/{wf_id}',
        key, "verify"), "verify")
    fptr = next((n for n in final["nodes"] if n["name"] == "Process Text Reply"), {})
    ok = "feedback to Claude (refine)" in fptr.get("parameters", {}).get("jsCode", "")
    print(f"7. VERIFY: reply-to-Claude rule present={ok}, active={final.get('active')}")
    print("REPLY-TO-CLAUDE RULE DEPLOYED — operator text routes to Claude; "
          "only 'send this:' sends verbatim." if ok
          else "x VERIFY FAILED — inspect; PRE-REPLYCLAUDE backup saved.")


if __name__ == "__main__":
    main()
