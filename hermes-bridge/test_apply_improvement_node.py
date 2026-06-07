#!/usr/bin/env python3
"""Regression guard for the n8n "Apply Improvement" node card rebuild (audit #1).

THE BUG (2026-06-07 RCA): the auto quality-improve loop swapped the improved
draft into the Telegram card with a brittle `tp.text.indexOf(orig)` substring
match. For multi-bubble drafts the card shows a NUMBERED preview ("1. .. 2. ..")
while `orig` is the "\n\n"-joined form, so indexOf == -1 and it APPENDED
"improved draft: <text>" after the "📝 Notes" line as DEAD text — AND still
wrote the improved text to Redis. Result: what-I-see != what-sends.

THE FIX: a DETERMINISTIC DRAFT-section rebuild keyed on g.messages alone
(fmtHdr/fmtPrev, spliced between the "💬 DRAFT" header and the "\n\n📝 Notes"
delimiter), with NO "improved draft:" append ever. The repo workflow JSON is
the source of truth deploy_bridge re-imports, so it MUST carry the fix, and
deploy_bridge MUST refuse to deploy a regressed copy.

This test covers, all offline / no network:
  1. patch_apply_improvement.patch() turns brittle node code into the rebuild.
  2. The canonical repo workflows/phase-1b-telegram.json "Apply Improvement"
     node carries the rebuild marker and NEVER the "improved draft:" literal.
  3. deploy_bridge._assert_apply_improvement_solid() passes the real repo JSON
     and die()s (SystemExit) on a regressed / missing node.

Plain-assert style. Run: python3 hermes-bridge/test_apply_improvement_node.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
WORKFLOW = ROOT / "workflows" / "phase-1b-telegram.json"
sys.path.insert(0, str(SCRIPTS))

import patch_apply_improvement as pai  # noqa: E402
import deploy_bridge  # noqa: E402

REBUILD_MARKER = "fmtHdr(g.messages)"
DEAD_TEXT = "improved draft:"

# A minimal brittle fragment matching the live/repo node's swap region.
BRITTLE = (
    "      const improved = g.messages.join(NL + NL);\n"
    "      const orig = pending.draft_text || '';\n"
    "      const swapped = (orig && tp.text.indexOf(orig) >= 0) ? "
    "tp.text.split(orig).join(improved) : "
    "(tp.text + NL + NL + 'improved draft:' + NL + improved);\n"
    "      cardText = (badgeFor(newScore, g.flags, g.summary) + NL + swapped)"
    ".slice(0, 4000);\n")


def _apply_node(code):
    return {"nodes": [{"name": "Apply Improvement",
                       "parameters": {"jsCode": code}}]}


def test_patch_replaces_brittle_swap_with_rebuild():
    out = pai.patch(BRITTLE)
    assert REBUILD_MARKER in out, "rebuild marker missing after patch"
    assert DEAD_TEXT not in out, "'improved draft:' dead-text append still present"
    # the deterministic splice anchor must be present
    assert ".slice(0, dI)" in out, "DRAFT-section splice anchor missing"


def test_patch_is_idempotent():
    once = pai.patch(BRITTLE)
    twice = pai.patch(once)
    assert once == twice, "patch is not idempotent"
    assert DEAD_TEXT not in twice


def test_patch_rejects_unexpected_node():
    # No brittle line and no rebuild marker => can't safely patch.
    try:
        pai.patch("const x = 1;\n")
    except pai.PatchError:
        return
    raise AssertionError("patch() should refuse code it cannot classify")


def test_repo_workflow_node_is_solid():
    data = json.loads(WORKFLOW.read_text())
    node = next((n for n in data["nodes"]
                 if n.get("name") == "Apply Improvement"), None)
    assert node is not None, "Apply Improvement node missing from repo workflow"
    code = (node.get("parameters") or {}).get("jsCode", "")
    assert REBUILD_MARKER in code, \
        "repo Apply Improvement node lacks the rebuild marker (regressed JSON)"
    assert DEAD_TEXT not in code, \
        "repo Apply Improvement node still has the 'improved draft:' append"


def test_deploy_guard_passes_real_repo_json():
    # Should NOT raise / exit on the (now-fixed) canonical repo JSON.
    deploy_bridge._assert_apply_improvement_solid(WORKFLOW)


def _guard_dies(code_or_data):
    if isinstance(code_or_data, str):
        data = _apply_node(code_or_data)
    else:
        data = code_or_data
    fd, path = tempfile.mkstemp(suffix=".json")
    try:
        os.write(fd, json.dumps(data).encode())
        os.close(fd)
        try:
            deploy_bridge._assert_apply_improvement_solid(Path(path))
        except SystemExit:
            return True
        return False
    finally:
        os.unlink(path)


def test_deploy_guard_dies_on_brittle_node():
    assert _guard_dies(BRITTLE), "guard did not die on brittle node"


def test_deploy_guard_dies_on_missing_marker():
    assert _guard_dies("const swapped = 'x';\n"), \
        "guard did not die when rebuild marker absent"


def test_deploy_guard_dies_on_missing_node():
    assert _guard_dies({"nodes": [{"name": "Other", "parameters": {}}]}), \
        "guard did not die when Apply Improvement node missing"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} apply-improvement node tests passed")
