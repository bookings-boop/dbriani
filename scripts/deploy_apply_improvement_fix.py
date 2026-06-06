#!/usr/bin/env python3
"""Fix the 'improved draft below the notes is useless' bug (Bug A-S1).

The n8n 'Apply Improvement' node, when it regenerates a better draft, tried to
swap it into the card by finding the ORIGINAL raw draft_text inside the card —
but the card shows a NUMBERED list (1. .. 2. ..), so for multi-message drafts
the match failed and it fell back to appending 'improved draft:\\n<text>' as
DEAD text below the notes (un-actionable; and a 'what I see != what sends'
trap since the improved text was still saved to Redis).

Fix: rebuild the card's DRAFT section in place from the improved messages,
using the SAME numbered format Queue & Format uses, with a delimiter-based
fallback (between the DRAFT header and the Notes line) so it can never again
dump dead text.

Replaces ONE line in the node (the brittle `const swapped = ...`). Deploys via
the safe path (re-fetches the live draft queue; backs up; fails closed; verifies).
Run from repo root:  python3 scripts/deploy_apply_improvement_fix.py
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from n8n_deploy import N8N, DeployError  # noqa: E402

NODE = "Apply Improvement"

# Matches the whole brittle line regardless of exact leading whitespace.
OLD_RE = re.compile(
    r"[ \t]*const swapped = \(orig && tp\.text\.indexOf\(orig\)[^\n]*\n")

# In-place DRAFT-section rebuild (same numbered format as Queue & Format).
NEW = (
    "      const fmtPrev = (ms) => ms.length === 1 ? String(ms[0]) : "
    "ms.map((m, i) => (i + 1) + '. ' + m).join(NL);\n"
    "      const fmtHdr = (ms) => ms.length === 1 ? '\U0001F4AC DRAFT:' : "
    "('\U0001F4AC DRAFT (' + ms.length + ' messages):');\n"
    "      const nb = fmtHdr(g.messages) + NL + fmtPrev(g.messages);\n"
    "      const oMsgs = Array.isArray(pending.messages) ? "
    "pending.messages.map(String) : [];\n"
    "      const ob = oMsgs.length ? (fmtHdr(oMsgs) + NL + fmtPrev(oMsgs)) : '';\n"
    "      let swapped;\n"
    "      if (ob && tp.text.indexOf(ob) >= 0) { swapped = tp.text.split(ob).join(nb); }\n"
    "      else { const dI = tp.text.indexOf('\U0001F4AC DRAFT'); "
    "const nI = tp.text.indexOf(NL + NL + '\U0001F4DD Notes'); "
    "swapped = (dI >= 0 && nI > dI) ? (tp.text.slice(0, dI) + nb + tp.text.slice(nI)) "
    ": (dI >= 0 ? (tp.text.slice(0, dI) + nb) : (tp.text + NL + NL + nb)); }\n")


def patch(code):
    n = len(OLD_RE.findall(code))
    if n != 1:
        raise DeployError(
            f"expected exactly 1 brittle swap line in {NODE}, found {n} "
            "— ABORT (live node differs; inspect before edit)")
    return OLD_RE.sub(NEW, code)


def main():
    n = N8N()
    wf = n.get_workflow()
    node = next((x for x in wf.get("nodes", []) if x.get("name") == NODE), None)
    if not node:
        raise DeployError(f"node {NODE!r} not found")
    code = (node.get("parameters", {}) or {}).get("jsCode", "")
    node["parameters"]["jsCode"] = patch(code)
    print(f"   {NODE}: brittle swap -> in-place DRAFT-section rebuild")
    n.safe_put(wf, tag="APPLY_IMPROVEMENT_FIX")
    fresh = n.get_workflow()
    fn = next((x for x in fresh.get("nodes", []) if x.get("name") == NODE), None)
    live = (fn.get("parameters", {}) or {}).get("jsCode", "") if fn else ""
    ok = ("fmtHdr(g.messages)" in live and "improved draft:" not in live)
    print("   VERIFY:", "OK — in-place rebuild live" if ok else "FAILED")
    if not ok:
        raise DeployError("post-deploy verify failed")


if __name__ == "__main__":
    try:
        main()
        print("apply-improvement card rebuild deployed.")
    except DeployError as e:
        sys.exit(f"x deploy failed: {e}")
