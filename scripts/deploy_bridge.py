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
# Drive-link registry — appended to the system prompt at deploy time
# so Hermes sees the 85+ available files inline. Kept as a separate
# file (docs/file-registry.md) so it can be edited without bumping
# the system-prompt and to keep the prompt focused on rules vs data.
FILE_REGISTRY = ROOT / "docs" / "file-registry.md"
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


# Backup retention — keep the N most recent of each pattern, delete the
# rest. Without this, every deploy leaves a snapshot behind forever; we
# accumulated 112 backup files (63MB) before this was added. Five gives
# enough rollback history without polluting the tree.
BACKUP_KEEP = 5


def prune_local_backups():
    """Delete older PRE-DEPLOY workflow backups in workflows/, keep the 5
    most recent. Also delete LIVE-backup-* files (created by deploy_workflow.py)
    and hermes-bridge/server.PRE-*.py / system-prompt.PRE-*.md snapshots
    if they exist. Mtime-sorted (newest first), older ones unlinked."""
    targets = [
        (ROOT / "workflows", "phase-1b-telegram.PRE-DEPLOY-*.json"),
        (ROOT / "workflows", "phase-1b-telegram.LIVE-backup-*.json"),
        # NOTE (audit #17, 2026-06-07): do NOT add a broad
        # "phase-1b-telegram.PRE-*.json" glob here — it would also prune
        # n8n_deploy.safe_put's PRE-{tag}-* snapshots, which are the only
        # on-disk record of live-only workflow edits (e.g. PRE-APPLY_IMPROVEMENT
        # before this same run can re-import over that fix). Keep them.
        (BRIDGE_DIR, "server.PRE-*.py"),
        (BRIDGE_DIR, "system-prompt.PRE-*.md"),
    ]
    pruned = 0
    for dir_, pattern in targets:
        files = sorted(dir_.glob(pattern),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        for f in files[BACKUP_KEEP:]:
            try:
                f.unlink()
                pruned += 1
            except OSError as e:
                print(f"  WARN: couldn't prune {f.name}: {e}")
    if pruned:
        print(f"   pruned {pruned} old local backup(s) "
              f"(kept {BACKUP_KEEP} per pattern)")


def prune_box_backups():
    """Delete older .bak.<ts> backups on the box, keep the 5 most recent.
    The box accumulates one .bak.<ts> per deploy in ~/hermes-bridge/."""
    cmd = (
        f"cd ~/hermes-bridge && "
        f"for pat in 'server.py.bak.*' 'system-prompt.md.bak.*'; do "
        f"  ls -1t $pat 2>/dev/null | tail -n +$(({BACKUP_KEEP}+1)) | "
        f"  xargs -r rm -f; "
        f"done && echo BOX_PRUNED"
    )
    out, _, _ = ssh_run(cmd, "prune-box-backups", timeout=15)
    if "BOX_PRUNED" in out:
        print(f"   pruned old box-side .bak.<ts> (kept {BACKUP_KEEP} per pattern)")


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


def _assert_apply_improvement_solid(path):
    """Fail-closed deploy guard (audit #1 RCA, 2026-06-07).

    The n8n 'Apply Improvement' node MUST carry the deterministic DRAFT-section
    rebuild (fmtHdr(g.messages) spliced between the '💬 DRAFT' header and the
    '\\n\\n📝 Notes' delimiter) and NEVER the brittle indexOf swap that appended
    'improved draft:' dead text under Notes (what-I-see != what-sends). This
    full-import re-pushes the ENTIRE repo workflow JSON, so a stale/regressed
    copy here would silently revert the live fix — the exact 06-06->06-07
    regression. die() before the import can ever happen so it can NEVER silently
    regress again. Repo is patched/kept solid via scripts/patch_apply_improvement.py."""
    try:
        data = json.loads(Path(path).read_text())
    except Exception as e:
        die(f"Apply-Improvement guard: could not parse workflow JSON: {e!r}")
    node = next((n for n in data.get("nodes", [])
                 if n.get("name") == "Apply Improvement"), None)
    if not node:
        die("Apply Improvement node missing from workflow JSON — refusing to deploy")
    code = (node.get("parameters") or {}).get("jsCode", "")
    if "improved draft:" in code:
        die("Apply Improvement: brittle 'improved draft:' append present — refusing "
            "to deploy a regressed workflow (audit #1 RCA 2026-06-07). "
            "Re-run scripts/patch_apply_improvement.py to restore the rebuild.")
    if "fmtHdr(g.messages)" not in code:
        die("Apply Improvement: deterministic rebuild marker fmtHdr(g.messages) "
            "missing — refusing to deploy. Re-run scripts/patch_apply_improvement.py.")


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
    if not FILE_REGISTRY.exists():
        die(f"file registry missing: {FILE_REGISTRY}")
    if not WORKFLOW_PATH.exists():
        die(f"workflow JSON missing: {WORKFLOW_PATH}")
    if not SERVER_PY.exists():
        die(f"server.py missing: {SERVER_PY}")
    prompt_text = MASTER_PROMPT.read_text()
    if len(prompt_text) < 1000:
        die(f"master prompt suspiciously short ({len(prompt_text)} chars) — "
            "refusing to deploy")
    registry_text = FILE_REGISTRY.read_text()
    if len(registry_text) < 100:
        die(f"file registry suspiciously short ({len(registry_text)} chars) — "
            "refusing to deploy")
    # Append the registry to the prompt at deploy time so Hermes
    # sees both as one system message. Kept separate on disk so the
    # registry can be edited without bumping system-prompt.md.
    combined_prompt = (
        prompt_text + "\n\n---\n\n# FILE REGISTRY\n" + registry_text)
    print(f"1. master prompt loaded: {len(prompt_text):,} chars, "
          f"{prompt_text.count(chr(10))+1} lines")
    print(f"   + file registry: {len(registry_text):,} chars, "
          f"{registry_text.count(chr(10))+1} lines "
          f"(combined: {len(combined_prompt):,} chars)")

    # 1. back up box bridge files
    ssh_run(f'cd ~/hermes-bridge && cp server.py server.py.bak.{ts} && '
            f'{{ [ -f system-prompt.md ] && cp system-prompt.md '
            f'system-prompt.md.bak.{ts} || true; }} && echo OK', "backup-box")
    print(f"2. backed up box server.py + system-prompt.md (.bak.{ts})")
    prune_box_backups()

    # 2. back up local workflow JSON
    local_backup = WORKFLOW_PATH.with_name(
        f"phase-1b-telegram.PRE-DEPLOY-{ts}.json")
    local_backup.write_bytes(WORKFLOW_PATH.read_bytes())
    print(f"3. backed up local workflow JSON -> {local_backup.name}")
    prune_local_backups()

    # 3. upload all bridge python modules + master prompt to bridge.
    # The bridge was a single server.py file; refactoring week split it
    # into util.py, db.py, etc. — upload every *.py module in BRIDGE_DIR
    # (excluding test_* files which the bridge doesn't need at runtime)
    # so the box always has the full set in sync. Otherwise a deploy
    # that lands a server.py importing a not-yet-uploaded module would
    # crash the bridge on restart.
    modules = sorted(p for p in BRIDGE_DIR.glob("*.py")
                     if not p.name.startswith("test_"))
    for mod in modules:
        ssh_upload(mod.read_bytes(),
                   f"~/hermes-bridge/{mod.name}", f"upload-{mod.name}")
    ssh_upload(prompt_text.encode(),
               "~/hermes-bridge/system-prompt.md", "upload-prompt")
    # The bridge reads file-registry.md at runtime (handle_send_file
    # via _load_file_registry). Upload it alongside the system prompt
    # so /send-file resolves keys to Drive URLs without an extra hop.
    ssh_upload(registry_text.encode(),
               "~/hermes-bridge/file-registry.md", "upload-registry")
    print(f"4. uploaded {len(modules)} module(s) "
          f"({', '.join(m.name for m in modules)}) "
          f"+ system-prompt.md + file-registry.md")

    # 4. compile-check every uploaded module on the box.
    mod_paths = " ".join(f"~/hermes-bridge/{m.name}" for m in modules)
    out, err, _ = ssh_run(
        f"python3 -m py_compile {mod_paths} && echo COMPILE_OK",
        "compile")
    if "COMPILE_OK" not in out:
        die(f"bridge modules failed to compile on the box:\n{err[:400]}")
    print(f"5. all {len(modules)} module(s) compile on the box")

    # 5. inject master prompt + file registry into all 4 workflow Set
    # nodes + rewrite the local workflow JSON so the on-disk file
    # always matches what's live (single source of truth — humans
    # edit MASTER_PROMPT + FILE_REGISTRY; deploy builds the combined
    # text). The pretty-printed copy preserves n8n's two-space indent
    # for diff readability.
    patched_data, patched_names = inject_prompt_into_workflow(
        combined_prompt, WORKFLOW_PATH)
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
    # Post-write / pre-import invariant (audit #1): the exact JSON about to be
    # imported (== what goes live) must carry the Apply-Improvement rebuild and
    # never the brittle 'improved draft:' append. Runs on BOTH the changed and
    # unchanged paths so a future deploy can never silently regress the fix.
    _assert_apply_improvement_solid(WORKFLOW_PATH)
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
        _imp_out, _imp_err, _ = ssh_run(
                f"docker cp /tmp/wf.json {N8N_CONTAINER}:/tmp/wf.json && "
                f"docker exec {N8N_CONTAINER} n8n import:workflow "
                f"--input=/tmp/wf.json && "
                f"docker exec {N8N_CONTAINER} n8n update:workflow "
                f"--id={WORKFLOW_ID} --active=true && echo IMPORTED",
                "n8n-import", timeout=180)
        # Audit #16a/#1 (2026-06-07): ASSERT the import succeeded (mirror the
        # COMPILE_OK gate at the bridge step). Previously this printed success
        # unconditionally, so a silent import failure left the live drafter on a
        # stale workflow while the always-uploaded box-file judges advanced —
        # and, worse, this same full re-import can silently REVERT any live-only
        # node edit. Reminder: keep ALL live n8n edits committed to this repo
        # (the repo is the source deploy_bridge imports from).
        if "IMPORTED" not in (_imp_out or ""):
            die("n8n import did NOT confirm IMPORTED — live workflow may now be "
                f"stale; inspect before retrying:\n{(_imp_err or _imp_out or '')[:400]}")
        print("8. workflow imported into n8n (IMPORTED confirmed)")

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
