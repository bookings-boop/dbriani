#!/usr/bin/env python3
"""
deploy_bridge.py — single-command deploy for the Hermes bridge AND the
n8n workflow's embedded system-prompt copies. The master prompt lives at
hermes-bridge/system-prompt.md; this script:

  1. Backs up the box's bridge files (.bak.<ts>) and the local workflow
     JSON (PRE-DEPLOY-<ts>.json).
  2. Uploads hermes-bridge/server.py and hermes-bridge/system-prompt.md
     to the box; compile-checks server.py.
  3. Reads hermes-bridge/system-prompt.md, injects its content into ALL
     FOUR n8n Set nodes that embed the prompt — Build Prompt,
     Build Regen Prompt, Build Refine Prompt, Build Lead Prompt — so the
     customer-message / regen / refine / outbound-lead paths all draft
     from identical rules. Writes the modified workflow to /tmp.
  4. Restarts hermes-bridge.service.
  5. Pushes the workflow via n8n CLI, then docker-restarts n8n-n8n-1
     (mandatory per the post-import Telegram-webhook-404 runbook —
     n8n's in-memory webhook registration doesn't refresh on import).
  6. Verifies: bridge /health, bridge /improve route gated, n8n /healthz,
     Telegram getWebhookInfo (pending_update_count == 0, no last_error).

Drift is impossible after this: editing the master file and running
this script syncs both the bridge service AND every workflow Set node
in one command.

Usage:  python3 scripts/deploy_bridge.py
"""
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BRIDGE_DIR = ROOT / "hermes-bridge"
WORKFLOW_PATH = ROOT / "workflows" / "phase-1b-telegram.json"
MASTER_PROMPT = BRIDGE_DIR / "system-prompt.md"
SERVER_PY = BRIDGE_DIR / "server.py"
WORKFLOW_ID = "azPIy9OcDwiPV5uY"
N8N_CONTAINER = "n8n-n8n-1"
# Every n8n Set node whose `systemPrompt` assignment is treated as the
# single source of truth. All four get the same master content.
PROMPT_NODES = (
    "Build Prompt",
    "Build Regen Prompt",
    "Build Refine Prompt",
    "Build Lead Prompt",
)

SSH = ["ssh", "-o", "ConnectTimeout=30", "dubriani-ec2"]
SYSCTL = "export XDG_RUNTIME_DIR=/run/user/$(id -u); systemctl --user"


def die(m):
    sys.exit(f"x {m}")


def ssh_run(script, label, timeout=120):
    """Retry-with-backoff ssh exec. Returns (stdout, stderr, rc)."""
    last = ""
    for a in range(1, 11):
        r = None
        try:
            r = subprocess.run(SSH + [script], capture_output=True, text=True,
                               timeout=timeout)
        except subprocess.TimeoutExpired:
            last = f"timed out after {timeout}s"
        if r is not None:
            if r.returncode != 255:
                return r.stdout, r.stderr, r.returncode
            last = "ssh exit 255 (connect timeout)"
        if a < 10:
            print(f"  [{label}] connect attempt {a} failed ({last}) — retry 5s",
                  flush=True)
            time.sleep(5)
    die(f"{label}: connection failed after 10 attempts — {last}")


def ssh_upload(data, remote, label):
    """Retry-with-backoff stdin-piped upload."""
    last = ""
    for a in range(1, 11):
        r = None
        try:
            r = subprocess.run(SSH + [f"cat > {remote}"], input=data,
                               capture_output=True, timeout=90)
        except subprocess.TimeoutExpired:
            last = "timed out"
        if r is not None:
            if r.returncode == 0:
                return
            last = f"exit {r.returncode}"
        if a < 10:
            print(f"  [{label}] upload attempt {a} failed ({last}) — retry 5s",
                  flush=True)
            time.sleep(5)
    die(f"{label}: upload failed after 10 attempts — {last}")


def inject_prompt_into_workflow(prompt_text, workflow_path):
    """Read workflow JSON, set the master prompt on every PROMPT_NODES
    Set node's `systemPrompt` assignment, set the top-level id (n8n CLI
    requires it for import), return the modified JSON dict + the set of
    nodes that were actually patched."""
    data = json.loads(workflow_path.read_text())
    data["id"] = WORKFLOW_ID  # n8n import:workflow needs this top-level
    patched = set()
    for node in data.get("nodes", []):
        if node.get("name") not in PROMPT_NODES:
            continue
        if node.get("type") != "n8n-nodes-base.set":
            continue
        params = node.get("parameters") or {}
        asn = (params.get("assignments") or {}).get("assignments") or []
        for a in asn:
            if a.get("name") == "systemPrompt":
                a["value"] = prompt_text
                patched.add(node["name"])
                break
    missing = sorted(set(PROMPT_NODES) - patched)
    if missing:
        die(f"workflow JSON is missing expected Set nodes: {missing}")
    return data, patched


def wait_for_n8n_ready(timeout=120):
    """Poll healthz until n8n responds 200. Returns True if up, False on
    timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        out, _, _ = ssh_run(
            'curl -sf -o /dev/null -w "%{http_code}" '
            'https://n8n.13-63-82-112.sslip.io/healthz', "healthz",
            timeout=15)
        if out.strip() == "200":
            return True
        time.sleep(3)
    return False


def check_telegram_webhook():
    """Run getWebhookInfo, parse pending_update_count + last_error. Returns
    (pending_int, last_error_str or '', url_str)."""
    out, _, _ = ssh_run(
        'TOKEN=$(docker exec ' + N8N_CONTAINER + ' printenv TELEGRAM_BOT_TOKEN); '
        f'curl -s "https://api.telegram.org/bot${{TOKEN}}/getWebhookInfo"',
        "webhook-info", timeout=30)
    try:
        r = json.loads(out).get("result", {})
        return (int(r.get("pending_update_count") or 0),
                (r.get("last_error_message") or ""),
                (r.get("url") or ""))
    except Exception as e:
        return -1, f"parse-err {e!r}", ""


def heal_telegram_webhook(webhook_url):
    """Call setWebhook with drop_pending_updates=true to fix the
    'Wrong response from the webhook: 404 Not Found' state that often
    follows an n8n import + container restart. n8n's in-memory webhook
    registry can lag behind container readiness; re-setting the webhook
    plus discarding the stale retry queue is the documented runbook
    recovery (see docs/fixes-2026-05-23-evening.md §5).

    Returns the setWebhook response (best-effort logging only)."""
    out, _, _ = ssh_run(
        'TOKEN=$(docker exec ' + N8N_CONTAINER + ' printenv TELEGRAM_BOT_TOKEN); '
        f'curl -s -X POST -H "Content-Type: application/json" '
        f'-d \'{{"url":"{webhook_url}","drop_pending_updates":true}}\' '
        f'"https://api.telegram.org/bot${{TOKEN}}/setWebhook"',
        "set-webhook", timeout=30)
    return out


def main():
    ts = time.strftime("%Y%m%d_%H%M%S")
    print("=== deploy_bridge.py — bridge + workflow sync ===")

    # 0. local sanity — master file + workflow exist
    if not MASTER_PROMPT.exists():
        die(f"master prompt missing: {MASTER_PROMPT}")
    if not WORKFLOW_PATH.exists():
        die(f"workflow JSON missing: {WORKFLOW_PATH}")
    if not SERVER_PY.exists():
        die(f"server.py missing: {SERVER_PY}")
    prompt_text = MASTER_PROMPT.read_text()
    if len(prompt_text) < 1000:
        die(f"master prompt suspiciously short ({len(prompt_text)} chars) — "
            "refusing to deploy")
    print(f"1. master prompt loaded: {len(prompt_text):,} chars, "
          f"{prompt_text.count(chr(10))+1} lines")

    # 1. back up box bridge files
    ssh_run(f'cd ~/hermes-bridge && cp server.py server.py.bak.{ts} && '
            f'{{ [ -f system-prompt.md ] && cp system-prompt.md '
            f'system-prompt.md.bak.{ts} || true; }} && echo OK', "backup-box")
    print(f"2. backed up box server.py + system-prompt.md (.bak.{ts})")

    # 2. back up local workflow JSON
    local_backup = WORKFLOW_PATH.with_name(
        f"phase-1b-telegram.PRE-DEPLOY-{ts}.json")
    local_backup.write_bytes(WORKFLOW_PATH.read_bytes())
    print(f"3. backed up local workflow JSON -> {local_backup.name}")

    # 3. upload server.py + master prompt to bridge
    ssh_upload(SERVER_PY.read_bytes(),
               "~/hermes-bridge/server.py", "upload-server")
    ssh_upload(prompt_text.encode(),
               "~/hermes-bridge/system-prompt.md", "upload-prompt")
    print("4. uploaded server.py + system-prompt.md to bridge")

    # 4. compile-check server.py on the box
    out, err, _ = ssh_run(
        "python3 -m py_compile ~/hermes-bridge/server.py && echo COMPILE_OK",
        "compile")
    if "COMPILE_OK" not in out:
        die(f"server.py failed to compile on the box:\n{err[:400]}")
    print("5. server.py compiles on the box")

    # 5. inject master prompt into all 4 workflow Set nodes + rewrite the
    # local workflow JSON so the on-disk file always matches what's live
    # (single source of truth — the local file is no longer where humans
    # edit the prompt; the master is). The pretty-printed copy preserves
    # n8n's two-space indent for diff readability.
    patched_data, patched_names = inject_prompt_into_workflow(
        prompt_text, WORKFLOW_PATH)
    new_pretty = json.dumps(patched_data, indent=2) + "\n"
    # Skip-n8n-restart optimization: if the workflow JSON content is
    # byte-identical to what's already on disk, the n8n side hasn't
    # changed. Skip the import + docker restart entirely. Every n8n
    # restart kills in-flight Debounce Wait nodes — customer messages
    # mid-debounce never produce drafts. Avoiding the restart when
    # nothing changed makes bridge-only deploys safe during traffic.
    existing_pretty = WORKFLOW_PATH.read_text() if WORKFLOW_PATH.exists() else ""
    workflow_unchanged = (new_pretty == existing_pretty)
    WORKFLOW_PATH.write_text(new_pretty)
    tmp_local = Path(f"/tmp/wf-deploy-{ts}.json")
    tmp_local.write_text(json.dumps(patched_data))
    if workflow_unchanged:
        print(f"6. workflow JSON unchanged — will skip n8n import + restart")
    else:
        print(f"6. injected master prompt into {len(patched_names)} Set nodes: "
              f"{sorted(patched_names)} + rewrote local workflow JSON")

    # 6. restart the bridge
    ssh_run(f'{SYSCTL} restart hermes-bridge && echo RESTARTED',
            "restart-bridge")
    time.sleep(3)
    print("7. hermes-bridge restarted")

    # 7. push workflow to n8n + docker-restart (per runbook — in-memory
    # webhook registration doesn't refresh on import alone). Skipped
    # entirely when the workflow content is unchanged.
    if workflow_unchanged:
        print("8. n8n import skipped (workflow JSON unchanged)")
        print("9. n8n restart skipped (workflow JSON unchanged) — "
              "in-flight Debounce Wait nodes preserved")
    else:
        ssh_upload(tmp_local.read_bytes(), "/tmp/wf.json", "upload-wf")
        ssh_run(f"docker cp /tmp/wf.json {N8N_CONTAINER}:/tmp/wf.json && "
                f"docker exec {N8N_CONTAINER} n8n import:workflow "
                f"--input=/tmp/wf.json && "
                f"docker exec {N8N_CONTAINER} n8n update:workflow "
                f"--id={WORKFLOW_ID} --active=true && echo IMPORTED",
                "n8n-import", timeout=180)
        print("8. workflow imported into n8n")

        ssh_run(f"docker restart {N8N_CONTAINER} && echo RESTARTED",
                "n8n-restart", timeout=120)
        print("9. n8n container restarted (refresh in-memory webhook registry)")

    # 8. verify
    if workflow_unchanged:
        print("10. n8n /healthz check skipped (container wasn't restarted)")
    else:
        if not wait_for_n8n_ready():
            die("n8n /healthz never came back 200 after restart")
        print("10. n8n /healthz: 200")

    active, _, _ = ssh_run(f'{SYSCTL} is-active hermes-bridge', "is-active",
                           timeout=15)
    health, _, _ = ssh_run(
        'curl -s -m10 -o /dev/null -w "%{http_code}" localhost:8788/health',
        "health", timeout=15)
    route, _, _ = ssh_run(
        'curl -s -m10 -o /dev/null -w "%{http_code}" -X POST '
        'localhost:8788/improve', "improve-route", timeout=15)
    active, health, route = active.strip(), health.strip(), route.strip()
    print(f"11. bridge: service={active}  /health=HTTP {health}  "
          f"/improve=HTTP {route}  (401 = route live + token-gated)")

    pending, last_err, webhook_url = check_telegram_webhook()
    print(f"12. telegram webhook: pending={pending}  last_err='{last_err}'")

    # Self-heal Telegram webhook 404. The post-import n8n restart sometimes
    # leaves the in-memory webhook registry stale; Telegram's last attempted
    # callback returns 404. Re-setting the webhook with
    # drop_pending_updates=true clears the bad retry queue and re-registers.
    # Operator hit this manually after every other deploy — now automatic.
    if last_err and webhook_url:
        is_404_pattern = ("404" in last_err
                          or "Wrong response" in last_err
                          or "Not Found" in last_err)
        if is_404_pattern:
            print(f"13. webhook self-heal: setWebhook drop_pending_updates=true")
            heal_telegram_webhook(webhook_url)
            time.sleep(6)  # let n8n reattach routes
            pending, last_err, _ = check_telegram_webhook()
            print(f"14. webhook recheck: pending={pending}  last_err='{last_err}'")

    ok = (active == "active" and health == "200" and route == "401"
          and pending == 0 and not last_err)
    if ok:
        print("\n✅ DEPLOY OK — bridge + workflow synced from master prompt.")
    else:
        die(f"VERIFY FAILED — inspect; box bridge backups saved as "
            f".bak.{ts}, local workflow backup at {local_backup}")


if __name__ == "__main__":
    main()
