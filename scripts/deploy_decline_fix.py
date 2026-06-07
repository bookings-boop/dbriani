#!/usr/bin/env python3
"""Polite-decline / graceful-exit fix (2026-06-07 Anya "yes we found thank you").

Deploys:
  - server.py (broadened _DECLINE_RE -> LOST + enhanced GRACEFUL_GOODBYE_DIRECTIVE
    with the feedback ask) via scp + bridge restart.
  - system-prompt.md decline rule re-embedded into the 4 Build Prompt nodes, and
    the n8n "Queue & Format" empty-messages fallback softened from
    "NO REPLY - likely spam/B2B" -> neutral — both pushed via safe_put (preserves
    the live draft queue, no container restart) + system-prompt.md scp'd to box.

Run:  python3 scripts/deploy_decline_fix.py [--deploy]
"""
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from n8n_deploy import N8N  # noqa: E402

WF_LOCAL = ROOT / "workflows" / "phase-1b-telegram.json"
SP = ROOT / "hermes-bridge" / "system-prompt.md"
REG = ROOT / "docs" / "file-registry.md"
SERVER = ROOT / "hermes-bridge" / "server.py"
PROMPT_NODES = ("Build Prompt", "Build Regen Prompt", "Build Refine Prompt", "Build Lead Prompt")

FALLBACK_OLD = "NO REPLY - likely spam/B2B (see notes). Tap Skip."
FALLBACK_NEW = "No auto-reply generated — review the notes, then Skip or Regen."
SSH = ["ssh", "-o", "ConnectTimeout=25", "dubriani-ec2"]


def combined_prompt():
    p, r = SP.read_text(), REG.read_text()
    if len(p) < 1000 or len(r) < 100:
        sys.exit("x prompt/registry too short — refusing")
    return p + "\n\n---\n\n# FILE REGISTRY\n" + r


def apply_edits(wf, combined):
    notes, failures = [], []
    # 1) soften the empty-messages fallback (find the node carrying it)
    hit_fb = 0
    for n in wf.get("nodes", []):
        code = (n.get("parameters") or {}).get("jsCode")
        if isinstance(code, str) and FALLBACK_OLD in code:
            n["parameters"]["jsCode"] = code.replace(FALLBACK_OLD, FALLBACK_NEW)
            notes.append(f"OK fallback softened in {n.get('name')!r}")
            hit_fb += 1
    if hit_fb == 0:
        if any(FALLBACK_NEW in ((n.get("parameters") or {}).get("jsCode") or "")
               for n in wf.get("nodes", [])):
            notes.append("-- fallback already softened")
        else:
            failures.append("fallback string not found in any node")
    # 2) re-embed prompt into the 4 Build nodes
    by = {n.get("name"): n for n in wf.get("nodes", [])}
    for name in PROMPT_NODES:
        node = by.get(name)
        if node is None:
            failures.append(f"prompt node {name!r} NOT FOUND"); continue
        asn = ((node.get("parameters") or {}).get("assignments") or {}).get("assignments") or []
        hit = False
        for a in asn:
            if a.get("name") == "systemPrompt":
                a["value"] = combined; hit = True; break
        notes.append(f"OK prompt {name}" if hit else f"prompt {name!r}: no systemPrompt assignment")
        if not hit:
            failures.append(f"prompt {name!r}: no systemPrompt assignment")
    return notes, failures


def ssh_run(script, label):
    r = subprocess.run(SSH + [script], capture_output=True, text=True, timeout=120)
    print(f"   [{label}] {r.stdout.strip()}")
    if r.returncode != 0:
        sys.exit(f"x {label} failed: {r.stderr.strip()[:300]}")


def main():
    deploy = "--deploy" in sys.argv
    combined = combined_prompt()
    local = json.loads(WF_LOCAL.read_text())
    notes, failures = apply_edits(local, combined)
    for n in notes:
        print("  ", n)
    if failures:
        print("\nABORT:")
        for f in failures:
            print("  x", f)
        sys.exit(1)
    WF_LOCAL.write_text(json.dumps(local, indent=2) + "\n")
    print(f"local workflow patched -> {WF_LOCAL.name}")
    if not deploy:
        print("\nDRY RUN — local patched (git diff to review). Not pushed. Re-run with --deploy.")
        return

    # server.py -> box + restart bridge
    ts = time.strftime("%Y%m%d_%H%M%S")
    ssh_run(f"cp ~/hermes-bridge/server.py ~/hermes-bridge/server.py.bak.{ts} && echo backed-up", "backup")
    subprocess.run(["scp", "-q", str(SERVER), "dubriani-ec2:~/hermes-bridge/server.py"], check=True)
    ssh_run("cd ~/hermes-bridge && python3 -m py_compile server.py && echo COMPILE_OK", "compile")
    ssh_run("export XDG_RUNTIME_DIR=/run/user/$(id -u); systemctl --user restart hermes-bridge && sleep 3 && "
            "systemctl --user is-active hermes-bridge && curl -s -m10 -o /dev/null -w 'health=%{http_code}' localhost:8788/health", "restart")

    # n8n (Queue&Format + prompt) via safe_put
    n = N8N()
    wf = n.get_workflow()
    _, lf = apply_edits(wf, combined)
    if lf:
        sys.exit("x live apply failed: " + "; ".join(lf))
    print("\npushing to live via safe_put (preserves draft queue, no restart)...")
    n.safe_put(wf, tag="DECLINE")
    subprocess.run(["scp", "-q", str(SP), "dubriani-ec2:~/hermes-bridge/system-prompt.md"], check=True)
    print("scp system-prompt.md -> box OK")
    print("\n✅ decline/graceful-exit fix deployed.")


if __name__ == "__main__":
    main()
