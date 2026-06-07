#!/usr/bin/env python3
"""Patch workflows/phase-1b-telegram.json for the draft-quality bundle
(2026-06-06): items 1B + 4A (like-for-like swap) and 2A (bubble-array scoring).

Surgical jsCode string replacements with per-edit assertions — writes back ONLY
if EVERY replacement applied exactly once (else aborts, no write). Git is the
backup (committed at a5428a1). Re-dumps with json.dumps(indent=2) to match the
deploy_bridge.py canonical format so the git diff stays minimal.

Run: python3 scripts/patch_draft_quality_workflow.py
"""
import json
import sys
from pathlib import Path

WF = Path(__file__).resolve().parent.parent / "workflows" / "phase-1b-telegram.json"

# (node_name, old_substring, new_substring, label) — order-independent.
EDITS = [
    # --- 1B: pass the ORIGINAL draft to /draft-gated so it can Anthropic-score it
    ("Apply Improvement",
     "        customer_phone: bp.customerPhone || '',\n        threshold: 8,",
     "        customer_phone: bp.customerPhone || '',\n"
     "        original_messages: (Array.isArray(pending.messages) && pending.messages.length) ? pending.messages : (pending.draft_text ? [pending.draft_text] : []),\n"
     "        threshold: 8,",
     "1B: send original_messages to /draft-gated"),
    # --- 1B + 4A: compare like-for-like (Anthropic original_score) and swap on >=
    ("Apply Improvement",
     "    const newScore = (g && g.ok && typeof g.score === 'number') ? Math.max(1, Math.min(10, Math.round(g.score))) : 0;\n"
     "    if (g && g.ok && Array.isArray(g.messages) && g.messages.length && newScore > score) {",
     "    const newScore = (g && g.ok && typeof g.score === 'number') ? Math.max(1, Math.min(10, Math.round(g.score))) : 0;\n"
     "    // 1B: compare like-for-like — the gate Anthropic-scores the ORIGINAL too\n"
     "    // (g.original_score); fall back to the local-Hermes badge only if absent.\n"
     "    // 4A: swap on >= (a tie regen is at least as good, and fresher).\n"
     "    const baseScore = (g && typeof g.original_score === 'number') ? Math.max(1, Math.min(10, Math.round(g.original_score))) : score;\n"
     "    if (g && g.ok && Array.isArray(g.messages) && g.messages.length && newScore >= baseScore) {",
     "1B+4A: like-for-like baseScore + swap on >="),
    # --- 2A: send the bubble ARRAY to /quality-check (badge scorer judges structure)
    ("Check Pending",
     "current_draft: d.draft_text || (Array.isArray(d.messages) ? d.messages.join('\\n\\n') : '')",
     "current_draft: (Array.isArray(d.messages) && d.messages.length) ? d.messages : (d.draft_text || '')",
     "2A: Check Pending sends bubble array"),
    ("Parse Regen",
     "current_draft: new_draft_text },",
     "current_draft: msgs },",
     "2A: Parse Regen sends bubble array"),
    ("Parse Refine",
     "current_draft: msgs.join('\\n\\n') },",
     "current_draft: msgs },",
     "2A: Parse Refine sends bubble array"),
]


def main():
    data = json.loads(WF.read_text())
    by_name = {n.get("name"): n for n in data.get("nodes", [])}
    failures = []
    for node_name, old, new, label in EDITS:
        node = by_name.get(node_name)
        if node is None:
            failures.append(f"{label}: node '{node_name}' NOT FOUND")
            continue
        params = node.get("parameters") or {}
        code = params.get("jsCode")
        if not isinstance(code, str):
            failures.append(f"{label}: node '{node_name}' has no jsCode")
            continue
        cnt = code.count(old)
        if cnt != 1:
            failures.append(
                f"{label}: expected old-substring exactly once in "
                f"'{node_name}', found {cnt}")
            continue
        params["jsCode"] = code.replace(old, new)
        print(f"OK  {label}")
    if failures:
        print("\nABORT — no file written. Failures:")
        for f in failures:
            print("  x", f)
        sys.exit(1)
    WF.write_text(json.dumps(data, indent=2) + "\n")
    print(f"\nwrote {WF.name} ({len(EDITS)} edits applied)")


if __name__ == "__main__":
    main()
