#!/usr/bin/env python3
"""
fix_lowercase_rule.py - update casing rule #2 in the LIVE workflow's embedded
system prompt (the "Build Prompt" + "Build Regen Prompt" Set nodes).

Rule #2 previously only PERMITTED lowercase for short messages. This makes
lowercase the default style for every reply, while keeping normal
capitalization for proper nouns + the currency "AED", and never altering URLs.
Mirrors the same change in system-prompt.md (the canonical source).

GETs the live workflow, backs it up, patches the two Set nodes, PUTs it back.
staticData (the draft queue) is preserved untouched.

Usage:  python3 scripts/fix_lowercase_rule.py
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
PROMPT_NODES = ["Build Prompt", "Build Regen Prompt"]

OLD = ('2. **Sign as Maria.** Warm, energetic, conversational. Lowercase fine '
       'for short messages ("hi there", "for when?", "got it!"). The lowercase '
       '"hi there" opener has +3.7pp lift over baseline — casual outperforms '
       'formal.')
NEW = ('2. **Sign as Maria.** Warm, energetic, conversational. **Write replies '
       'in lowercase, casual texting style** — lowercase sentence starts '
       'included ("hi there", "for when?", "got it!"); the lowercase "hi '
       'there" opener has +3.7pp lift over baseline, casual outperforms '
       'formal. **Keep normal capitalization only for:** proper nouns (place '
       'names like Dubai, the customer\'s name, yacht and package names) and '
       'the currency code "AED". Copy any link or payment URL exactly as '
       'given — never change its case.')


def die(m):
    sys.exit(f"x {m}")


def load_key():
    for ln in (ROOT / ".env").read_text().splitlines():
        if ln.startswith("N8N_API_KEY="):
            v = ln.split("=", 1)[1].strip()
            if v:
                return v
    die("N8N_API_KEY not in .env")


def resolve_base():
    r = subprocess.run(
        SSH + ["docker inspect n8n-n8n-1 --format "
               "'{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}'"],
        capture_output=True, text=True, timeout=30)
    ip = r.stdout.strip()
    if not ip:
        die("could not resolve n8n container IP")
    return f"http://{ip}:5678/api/v1"


def ssh_run(script, stdin, label, timeout=120):
    r = subprocess.run(SSH + [script], input=stdin, capture_output=True,
                       text=True, timeout=timeout)
    if r.returncode != 0:
        die(f"{label}: ssh exit {r.returncode}\n{r.stderr[:400]}")
    return r.stdout


def ssh_upload(data, remote, label):
    r = subprocess.run(SSH + [f"cat > {remote}"], input=data,
                       capture_output=True, timeout=60)
    if r.returncode != 0:
        die(f"{label}: upload failed")


def as_json(t, label):
    try:
        return json.loads(t)
    except Exception:
        die(f"{label}: non-JSON response:\n{t[:500]}")


def patch_strings(obj, counter):
    """Recursively replace OLD -> NEW in every string value."""
    if isinstance(obj, dict):
        for k in list(obj.keys()):
            obj[k] = patch_strings(obj[k], counter)
        return obj
    if isinstance(obj, list):
        return [patch_strings(x, counter) for x in obj]
    if isinstance(obj, str) and OLD in obj:
        counter[0] += obj.count(OLD)
        return obj.replace(OLD, NEW)
    return obj


def contains_str(obj, needle):
    """Recursively test whether `needle` appears in any string value."""
    if isinstance(obj, dict):
        return any(contains_str(v, needle) for v in obj.values())
    if isinstance(obj, list):
        return any(contains_str(x, needle) for x in obj)
    if isinstance(obj, str):
        return needle in obj
    return False


def main():
    key = load_key()
    global API
    API = resolve_base()
    print(f"1. n8n API: {API}")

    items = (as_json(ssh_run(
        f'read -r K; curl -s -m25 -H "X-N8N-API-KEY: $K" {API}/workflows',
        key, "list"), "list").get("data") or [])
    target = next((w for w in items if w.get("name") == WF_NAME), None)
    if not target:
        die(f"workflow {WF_NAME!r} not found")
    wf_id = target["id"]
    live = as_json(ssh_run(
        f'read -r K; curl -s -m25 -H "X-N8N-API-KEY: $K" {API}/workflows/{wf_id}',
        key, "get"), "get")
    was_active = bool(live.get("active"))
    print(f"2. live workflow {wf_id}: {len(live.get('nodes', []))} nodes, "
          f"active={was_active}")

    ts = time.strftime("%Y%m%d_%H%M%S")
    bk = ROOT / "workflows" / f"phase-1b-telegram.PRE-LOWERCASE-{ts}.json"
    bk.write_text(json.dumps(live, indent=2, ensure_ascii=False) + "\n")
    print(f"3. backed up live -> {bk.name}")

    total = 0
    for nm in PROMPT_NODES:
        node = next((n for n in live["nodes"] if n["name"] == nm), None)
        if not node:
            die(f"node {nm!r} not found in the live workflow")
        params = node.get("parameters", {})
        if not contains_str(params, OLD):
            if contains_str(params, NEW):
                print(f"4. {nm}: already patched — skipping")
                continue
            die(f"{nm}: old casing rule not found and not already patched — "
                f"live prompt differs from expectation; not patching blindly")
        counter = [0]
        node["parameters"] = patch_strings(node["parameters"], counter)
        total += counter[0]
        print(f"4. {nm}: replaced casing rule ({counter[0]}x)")
    if total == 0:
        print("   nothing to change — both nodes already patched")
        return

    put = {
        "name": live.get("name", WF_NAME),
        "nodes": live["nodes"],
        "connections": live["connections"],
        "settings": {k: v for k, v in (live.get("settings") or {}).items()
                     if k in ALLOWED_SETTINGS},
    }
    if isinstance(live.get("staticData"), (dict, str)) and live.get("staticData"):
        put["staticData"] = live["staticData"]

    ssh_upload((json.dumps(put, ensure_ascii=False) + "\n").encode("utf-8"),
               "/tmp/lc_wf.json", "upload")
    res = as_json(ssh_run(
        f'read -r K; curl -s -m90 -X PUT -H "X-N8N-API-KEY: $K" '
        f'-H "Content-Type: application/json" --data-binary @/tmp/lc_wf.json '
        f'{API}/workflows/{wf_id}; rm -f /tmp/lc_wf.json', key, "PUT"), "PUT")
    if res.get("id") != wf_id:
        die(f"PUT did not return the workflow: {json.dumps(res)[:400]}")
    print(f"5. PUT OK ({len(res.get('nodes', []))} nodes)")

    if was_active:
        ssh_run(f'read -r K; curl -s -m25 -X POST -H "X-N8N-API-KEY: $K" '
                f'{API}/workflows/{wf_id}/activate', key, "activate")
        print("6. re-activated")

    final = as_json(ssh_run(
        f'read -r K; curl -s -m25 -H "X-N8N-API-KEY: $K" {API}/workflows/{wf_id}',
        key, "verify"), "verify")
    ok = True
    for nm in PROMPT_NODES:
        node = next((n for n in final["nodes"] if n["name"] == nm), {})
        params = node.get("parameters", {})
        has_new, has_old = contains_str(params, NEW), contains_str(params, OLD)
        print(f"7. VERIFY {nm}: new rule present={has_new}, old gone={not has_old}")
        ok = ok and has_new and not has_old
    print("LOWERCASE RULE DEPLOYED — drafts now default to lowercase style."
          if ok else "x VERIFY FAILED — inspect; PRE-LOWERCASE backup saved.")


if __name__ == "__main__":
    main()
