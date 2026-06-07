#!/usr/bin/env python3
"""Patch the REPO workflow's "Apply Improvement" node — deterministic rebuild.

WHY (audit #1 RCA, 2026-06-07): the n8n "Apply Improvement" auto quality-improve
loop swapped the regenerated draft into the Telegram card with a brittle
`tp.text.indexOf(orig)` substring match. The card renders a NUMBERED preview
("1. .. 2. ..") while `orig` is the "\\n\\n"-joined draft_text, so for any
multi-bubble draft indexOf == -1 and it fell to the else branch, APPENDING
"improved draft: <text>" AFTER the "📝 Notes" line as DEAD text — while STILL
writing the improved text to Redis. The operator saw the old numbered draft +
orphaned text under Notes, but ✅ Send fanned the (invisible) improved text:
a what-I-see != what-sends trap.

THE FIX: replace the brittle swap with the DETERMINISTIC DRAFT-section rebuild
(fmtHdr/fmtPrev built from g.messages — the SAME array written to fields —
spliced between the "💬 DRAFT" header and the "\\n\\n📝 Notes" delimiter, with
a header-only fallback). It never depends on finding the original verbatim and
NEVER appends "improved draft:". The replacement text is imported from
scripts/deploy_apply_improvement_fix.py so the REPO node is byte-for-byte the
SAME rebuild the orchestrator safe_put's to the live node — repo == box == live.

This script is the SAFE way to edit the 540KB workflow JSON: it parses, finds
the node, applies a regex-anchored single-line substitution with per-edit
ASSERTIONS, then re-reads and re-asserts the persisted file. NEVER hand-edit
the JSON. Idempotent — re-running on an already-fixed node is a no-op.

Run from repo root:  python3 scripts/patch_apply_improvement.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = ROOT / "workflows" / "phase-1b-telegram.json"
NODE = "Apply Improvement"
REBUILD_MARKER = "fmtHdr(g.messages)"   # present only in the rebuild
SPLICE_ANCHOR = ".slice(0, dI)"         # the marker-anchored splice
DEAD_TEXT = "improved draft:"           # the brittle append literal — must die

sys.path.insert(0, str(Path(__file__).resolve().parent))
# Reuse the EXACT regex + replacement the live-node fix uses, so the repo
# node matches what safe_put pushes to the box (single source of truth).
from deploy_apply_improvement_fix import OLD_RE, NEW  # noqa: E402


class PatchError(Exception):
    """Raised when the node cannot be safely patched (fail closed)."""


def patch(code):
    """Return jsCode with the brittle indexOf swap replaced by the
    deterministic DRAFT-section rebuild.

    - Idempotent: code that already carries the rebuild (and no dead-text
      append) is returned unchanged.
    - Fail-closed: raises PatchError if the code is neither brittle nor
      already-fixed, or if the post-edit assertions don't hold — so we never
      write an unrecognized / half-patched node.
    """
    already_fixed = (REBUILD_MARKER in code and DEAD_TEXT not in code)
    n = len(OLD_RE.findall(code))
    if n == 0:
        if already_fixed:
            return code
        raise PatchError(
            f"{NODE}: no brittle swap line found and rebuild marker absent — "
            "refusing to patch an unrecognized node (inspect manually)")
    if n != 1:
        raise PatchError(
            f"{NODE}: expected exactly 1 brittle swap line, found {n} — ABORT "
            "(node differs from the known-bad shape; inspect before edit)")
    out = OLD_RE.sub(NEW, code)
    # per-edit assertions — the substitution MUST yield the rebuild and MUST
    # remove the dead-text append. Anything else is a botched edit.
    if REBUILD_MARKER not in out:
        raise PatchError(f"{NODE}: post-edit rebuild marker {REBUILD_MARKER!r} missing")
    if SPLICE_ANCHOR not in out:
        raise PatchError(f"{NODE}: post-edit splice anchor {SPLICE_ANCHOR!r} missing")
    if DEAD_TEXT in out:
        raise PatchError(f"{NODE}: '{DEAD_TEXT}' still present after edit")
    return out


def main():
    if not WORKFLOW_PATH.exists():
        raise PatchError(f"workflow JSON missing: {WORKFLOW_PATH}")
    data = json.loads(WORKFLOW_PATH.read_text())
    node = next((n for n in data.get("nodes", []) if n.get("name") == NODE), None)
    if node is None:
        raise PatchError(f"{NODE} node not found in {WORKFLOW_PATH}")
    code = (node.get("parameters") or {}).get("jsCode", "")
    new_code = patch(code)
    if new_code == code:
        print(f"   {NODE}: already solid — no change "
              f"({REBUILD_MARKER} present, no '{DEAD_TEXT}')")
    else:
        node["parameters"]["jsCode"] = new_code
        # Match deploy_bridge's on-disk format exactly (json.dumps default
        # ensure_ascii=True, indent=2, trailing newline) so the diff is the
        # one node only — verified byte-stable on this file.
        WORKFLOW_PATH.write_text(json.dumps(data, indent=2) + "\n")
        print(f"   {NODE}: brittle indexOf swap -> deterministic "
              f"DRAFT-section rebuild (no '{DEAD_TEXT}' append)")
    # post-write invariant: re-read the persisted file and re-assert.
    fresh = json.loads(WORKFLOW_PATH.read_text())
    fnode = next((n for n in fresh.get("nodes", [])
                  if n.get("name") == NODE), None)
    fcode = (fnode.get("parameters") or {}).get("jsCode", "") if fnode else ""
    if REBUILD_MARKER not in fcode or DEAD_TEXT in fcode:
        raise PatchError(f"{NODE}: persisted JSON failed the post-write invariant")
    print(f"   VERIFY: repo Apply Improvement node is solid "
          f"({REBUILD_MARKER} present, no '{DEAD_TEXT}')")


if __name__ == "__main__":
    try:
        main()
        print("apply-improvement repo-JSON rebuild patched.")
    except PatchError as e:
        sys.exit(f"x patch failed: {e}")
