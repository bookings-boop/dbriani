#!/usr/bin/env python3
"""
build_redis_phase_a.py — staticData→Redis migration, PHASE A (READS).

Switches the read-only enrichment nodes to read the draft from REDIS
first (it's already dual-written there by Queue & Format / Persist Draft),
keeping the legacy staticData read as a FALLBACK. No writes are removed —
fully reversible. The atomic-commit + Persist nodes already keep Redis
authoritative; this phase stops new reads from depending on staticData.

  • Prep Regen        -> Parse Callback's Redis draft (pc.draft)
  • Check Pending     -> Queue & Format's in-execution draft object
  • Auto Prep         -> Queue & Format's in-execution draft object
  • Find Break        -> Queue & Format's in-execution draft object
  • Prep Paylink      -> Parse Callback's Redis draft (primary)
  • Prep Learn        -> bridge /queue get
  • Build Paylink From Reply -> bridge /queue get

Each edit is anchored on the exact current code; a non-unique anchor is a
HARD STOP (also catches local⇄live drift before any deploy).

USAGE:
  python3 scripts/build_redis_phase_a.py            # dry-run: edit+syntax-check local in memory, no write, no deploy
  python3 scripts/build_redis_phase_a.py --deploy   # safe_put to live, then write local file
"""
import sys
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from n8n_deploy import N8N  # noqa: E402

LOCAL = ROOT / "workflows" / "phase-1b-telegram.json"
TAG = "REDIS_PHASE_A"

_GET = (
    "  const r = await _http({ method: 'POST',\n"
    "    url: 'http://172.18.0.1:8788/queue',\n"
    "    headers: { 'Content-Type': 'application/json',\n"
    "      'X-Bridge-Token': $env.BRIDGE_TOKEN || '' },\n"
)

# node name -> list of (old_anchor, new_text). Each anchor MUST occur exactly once.
EDITS = {
    "Prep Regen": [(
        "const draftId = $('Parse Callback').item.json.draft_id;\n"
        "const data = $getWorkflowStaticData('global');\n"
        "const draft = (data.pendingQueue || []).find(d => d.id === draftId);\n"
        "if (!draft) {",
        "const pc = $('Parse Callback').item.json;\n"
        "const draftId = pc.draft_id;\n"
        "// Phase A (Redis-migration): Redis draft first, staticData fallback.\n"
        "let draft = pc.draft || null;\n"
        "if (!draft) {\n"
        "  const data = $getWorkflowStaticData('global');\n"
        "  draft = (data.pendingQueue || []).find(d => d.id === draftId) || null;\n"
        "}\n"
        "if (!draft) {",
    )],
    "Check Pending": [
        (
            "const data = $getWorkflowStaticData('global');\n"
            "const d = (data.pendingQueue || []).find(x => x.id === draftId);",
            "// Phase A (Redis-migration): use Queue & Format's in-execution\n"
            "// draft object; staticData fallback.\n"
            "let d = null;\n"
            "try { d = ($('Queue & Format').item.json || {}).draft || null; } catch (e) { d = null; }\n"
            "if (!d) {\n"
            "  const data = $getWorkflowStaticData('global');\n"
            "  d = (data.pendingQueue || []).find(x => x.id === draftId);\n"
            "}",
        ),
        (
            "  telegram_message_id: d.telegram_message_id,",
            "  telegram_message_id: sid.telegram_message_id || d.telegram_message_id,",
        ),
    ],
    "Auto Prep": [
        (
            "const data = $getWorkflowStaticData('global');\n"
            "const d = (data.pendingQueue || []).find(x => x.id === draftId);\n"
            "if (!d) return [];",
            "// Phase A (Redis-migration): use Queue & Format's in-execution draft.\n"
            "let d = null;\n"
            "try { d = ($('Queue & Format').item.json || {}).draft || null; } catch (e) { d = null; }\n"
            "if (!d) {\n"
            "  const data = $getWorkflowStaticData('global');\n"
            "  d = (data.pendingQueue || []).find(x => x.id === draftId);\n"
            "}\n"
            "if (!d) return [];",
        ),
        (
            "  telegram_message_id: d.telegram_message_id,",
            "  telegram_message_id: sid.telegram_message_id || d.telegram_message_id,",
        ),
    ],
    "Find Break": [(
        "const data = $getWorkflowStaticData('global');\n"
        "const d = (data.pendingQueue || []).find(x => x.id === sid.draft_id) || {};",
        "// Phase A (Redis-migration): use Queue & Format's in-execution draft.\n"
        "let d = null;\n"
        "try { d = ($('Queue & Format').item.json || {}).draft || null; } catch (e) { d = null; }\n"
        "if (!d) {\n"
        "  const data = $getWorkflowStaticData('global');\n"
        "  d = (data.pendingQueue || []).find(x => x.id === sid.draft_id) || {};\n"
        "}\n"
        "d = d || {};",
    )],
    "Prep Paylink": [(
        "const pc = $('Parse Callback').item.json;\n"
        "const draft = pc.draft || {};\n"
        "const data = $getWorkflowStaticData('global');\n"
        "const entry = (data.pendingQueue || []).find(d => d.id === pc.draft_id) || draft;",
        "const pc = $('Parse Callback').item.json;\n"
        "const draft = pc.draft || {};\n"
        "// Phase A (Redis-migration): Redis draft (Parse Callback) is primary.\n"
        "const data = $getWorkflowStaticData('global');\n"
        "const sdEntry = (data.pendingQueue || []).find(d => d.id === pc.draft_id);\n"
        "const entry = (draft && draft.customer_phone) ? draft : (sdEntry || draft);",
    )],
    "Prep Learn": [(
        "const ptr = $('Process Text Reply').item.json;\n"
        "const draftId = ptr.draft_id;\n"
        "const data = $getWorkflowStaticData('global');\n"
        "const d = (data.pendingQueue || []).find(x => x.id === draftId) || {};",
        "const ptr = $('Process Text Reply').item.json;\n"
        "const draftId = ptr.draft_id;\n"
        "// Phase A (Redis-migration): fetch the draft from Redis; staticData fallback.\n"
        "const _http = this.helpers.httpRequest.bind(this.helpers);\n"
        "let d = null;\n"
        "try {\n"
        + _GET +
        "    body: { action: 'get', draft_id: draftId }, json: true, timeout: 8000 });\n"
        "  if (r && r.found && r.draft) d = r.draft;\n"
        "} catch (e) { d = null; }\n"
        "if (!d) {\n"
        "  const data = $getWorkflowStaticData('global');\n"
        "  d = (data.pendingQueue || []).find(x => x.id === draftId) || {};\n"
        "}",
    )],
    "Build Paylink From Reply": [(
        "const inp = $input.item.json;\n"
        "const data = $getWorkflowStaticData('global');\n"
        "const entry = (data.pendingQueue || []).find(d => d.id === inp.draft_id) || {};",
        "const inp = $input.item.json;\n"
        "// Phase A (Redis-migration): fetch the draft from Redis; staticData fallback.\n"
        "const _http = this.helpers.httpRequest.bind(this.helpers);\n"
        "let entry = null;\n"
        "try {\n"
        + _GET +
        "    body: { action: 'get', draft_id: inp.draft_id }, json: true, timeout: 8000 });\n"
        "  if (r && r.found && r.draft) entry = r.draft;\n"
        "} catch (e) { entry = null; }\n"
        "if (!entry) {\n"
        "  const data = $getWorkflowStaticData('global');\n"
        "  entry = (data.pendingQueue || []).find(d => d.id === inp.draft_id) || {};\n"
        "}",
    )],
}


def apply_edits(nodes_by_name, label):
    changed = {}
    for name, pairs in EDITS.items():
        node = nodes_by_name.get(name)
        if not node:
            sys.exit(f"x {label}: node {name!r} not found")
        if node.get("type", "").split(".")[-1] != "code":
            sys.exit(f"x {label}: node {name!r} is not a code node")
        code = node["parameters"].get("jsCode", "")
        for old, new in pairs:
            cnt = code.count(old)
            if cnt != 1:
                sys.exit(
                    f"x {label}: node {name!r}: anchor matched {cnt} times "
                    f"(expected 1) — possible drift. Anchor head:\n   "
                    + old.splitlines()[0])
            code = code.replace(old, new, 1)
        node["parameters"]["jsCode"] = code
        changed[name] = code
    return changed


def syntax_check(changed):
    node_bin = (shutil.which("node")
                or str(Path.home() / ".local/node/bin/node"))
    if not Path(node_bin).exists() and not shutil.which("node"):
        print("   (node not found — skipping syntax check)")
        return
    for name, code in changed.items():
        wrapped = "async function __n8n(){\n" + code + "\n}\n"
        with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False) as f:
            f.write(wrapped)
            tmp = f.name
        r = subprocess.run([node_bin, "--check", tmp],
                           capture_output=True, text=True)
        Path(tmp).unlink(missing_ok=True)
        if r.returncode != 0:
            sys.exit(f"x syntax error in node {name!r}:\n{r.stderr}")
        print(f"   ✓ syntax OK: {name}")


def main():
    deploy = "--deploy" in sys.argv
    print(f"=== {TAG} ({'DEPLOY' if deploy else 'DRY-RUN'}) ===")

    local = json.loads(LOCAL.read_text())
    local_by_name = {n["name"]: n for n in local["nodes"]}
    print(f"local workflow: {len(local['nodes'])} nodes")
    changed = apply_edits(local_by_name, "local")
    print(f"applied edits to {len(changed)} nodes: {sorted(changed)}")
    syntax_check(changed)

    if not deploy:
        print("\nDRY-RUN OK — anchors matched, syntax valid. "
              "Re-run with --deploy to push.")
        return

    print("\n--- deploying to live ---")
    n = N8N()
    wf = n.get_workflow()
    live_by_name = {nd["name"]: nd for nd in wf["nodes"]}
    apply_edits(live_by_name, "live")   # same edits; anchor-count = drift guard
    n.safe_put(wf, tag=TAG)

    # match the committed file's encoding (ensure_ascii=True) so the git
    # diff is exactly the changed jsCode lines, not a whole-file re-encode.
    LOCAL.write_text(json.dumps(local, indent=2, ensure_ascii=True) + "\n")
    print(f"   local file updated -> {LOCAL.name} (PII-free, ready to commit)")
    print("\nPHASE A DEPLOYED. Operator test matrix:")
    print("  1. Tap 🔁 Regen on a draft → new draft renders (Prep Regen)")
    print("  2. Send a (non-payment) customer msg, wait ~30s → card gets "
          "'✨ improved by Hermes' (Check Pending)")
    print("  3. Tap 💳 Send Link on a draft → amount prompt/link (Prep Paylink)")
    print("  4. Reply with an amount to the prompt → link generated "
          "(Build Paylink From Reply)")
    print("  5. Reply edit-feedback to a card → refine works (regression)")
    print("  6. Confirm a normal draft still posts + Send works (regression)")


if __name__ == "__main__":
    main()
