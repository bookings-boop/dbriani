#!/usr/bin/env python3
"""Safe prompt-only deploy: inject system-prompt.md (+ file registry) into the
4 n8n Set nodes via n8n_deploy.safe_put — re-fetches live staticData immediately
pre-PUT (preserves pendingQueue), NO container restart, NO full re-import (so it
can't clobber live-only node edits). Use this instead of deploy_bridge.py for a
prompt-only change. Run: python3 scripts/deploy_prompt_safeput.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "hermes-bridge"))
from n8n_deploy import N8N  # noqa: E402

PROMPT_NODES = ("Build Prompt", "Build Regen Prompt",
                "Build Refine Prompt", "Build Lead Prompt")

prompt = (ROOT / "hermes-bridge" / "system-prompt.md").read_text()
registry = (ROOT / "docs" / "file-registry.md").read_text()
if len(prompt) < 1000:
    sys.exit(f"prompt suspiciously short ({len(prompt)}) — refusing")
combined = prompt + "\n\n---\n\n# FILE REGISTRY\n" + registry

n8n = N8N()
wf = n8n.get_workflow()
patched = set()
for node in wf.get("nodes", []):
    if node.get("name") in PROMPT_NODES and node.get("type") == "n8n-nodes-base.set":
        asn = ((node.get("parameters") or {}).get("assignments") or {}).get("assignments") or []
        for a in asn:
            if a.get("name") == "systemPrompt":
                a["value"] = combined
                patched.add(node["name"])
                break
missing = sorted(set(PROMPT_NODES) - patched)
if missing:
    sys.exit(f"missing expected Set nodes: {missing}")
print(f"patched {sorted(patched)}  combined={len(combined):,} chars  "
      f"nodes={len(wf.get('nodes', []))}")
n8n.safe_put(wf, tag="PROMPT-CHARLIE")
print("safe_put OK — prompt updated in 4 Set nodes, no restart")
