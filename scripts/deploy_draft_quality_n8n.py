#!/usr/bin/env python3
"""Push the draft-quality n8n node edits (items 1B + 4A + 2A) to the LIVE n8n
via n8n_deploy.safe_put — which re-fetches the live staticData immediately
before the PUT (preserving the in-flight draft queue) and does NOT restart the
container. deploy_bridge.py can't push these because its "workflow unchanged"
check only fires on prompt changes, not node-jsCode edits.

Applies the SAME verified EDITS as patch_draft_quality_workflow.py, but to the
freshly-fetched LIVE workflow (idempotent — skips an edit already present).

Dry-run by default; pass --deploy to actually safe_put.
Run: python3 scripts/deploy_draft_quality_n8n.py [--deploy]
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from n8n_deploy import N8N                       # noqa: E402
from patch_draft_quality_workflow import EDITS   # noqa: E402


def patch_nodes(wf):
    by_name = {n.get("name"): n for n in wf.get("nodes", [])}
    applied, failures = [], []
    for node_name, old, new, label in EDITS:
        node = by_name.get(node_name)
        if node is None:
            failures.append(f"{label}: node {node_name!r} NOT FOUND")
            continue
        code = (node.get("parameters") or {}).get("jsCode")
        if not isinstance(code, str):
            failures.append(f"{label}: {node_name!r} has no jsCode")
            continue
        cnt = code.count(old)
        if cnt != 1:
            if old not in code and new in code:
                applied.append(f"-- {label}: already applied (skip)")
                continue
            failures.append(
                f"{label}: expected old-substring once in {node_name!r}, "
                f"found {cnt} (and new not already present)")
            continue
        node["parameters"]["jsCode"] = code.replace(old, new)
        applied.append(f"OK {label}")
    return applied, failures


def main():
    deploy = "--deploy" in sys.argv
    n = N8N()
    wf = n.get_workflow()
    applied, failures = patch_nodes(wf)
    for a in applied:
        print("  ", a)
    if failures:
        print("\nABORT — not deploying. Failures:")
        for f in failures:
            print("  x", f)
        sys.exit(1)
    if not deploy:
        print("\nDRY RUN — live workflow patched in memory, NOT pushed. "
              "Re-run with --deploy.")
        return
    print("\ndeploying via n8n_deploy.safe_put (preserves live draft queue, "
          "no container restart)...")
    n.safe_put(wf, tag="DRAFTQUAL")
    print("\n✅ n8n workflow pushed.")


if __name__ == "__main__":
    main()
