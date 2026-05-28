#!/usr/bin/env python3
"""
build_quality_scoring.py — replace the auto-improve REWRITE with a quality
SCORE badge on the card header. No fallback rewrite, no silent changes.

Requires the bridge /quality-check endpoint (deployed separately).

n8n changes (improve chain repointed to scoring):
  • Improve Wait     amount 20 -> 5
  • Hermes Improve   url /improve -> /quality-check (body unchanged)
  • Apply Improvement  rewritten -> "Render Quality Badge" (prepends a
        🟢/🟡/🔴 N/10 line to the ORIGINAL card; draft text untouched;
        skips if not pending or score unavailable)
  • Improve Commit   regen-commit fields:{} -> card-only edit (no Redis text change)
  • Push Improved Text  DELETED (removes the auto-send rewrite)

USAGE:
  python3 scripts/build_quality_scoring.py            # dry-run + checks
  python3 scripts/build_quality_scoring.py --deploy   # deploy + write local
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
TAG = "QUALITY_SCORING"

RENDER_BADGE = r"""// Quality-score badge (replaces the old auto-rewrite). Reads the score
// from the /quality-check call (still wired through the 'Hermes Improve'
// node) and PREPENDS a badge line to the ORIGINAL card. The draft text is
// NEVER changed — no silent rewrites. Skips silently if the operator
// already acted (Redis status != pending) or the score is unavailable.
const qc = $('Hermes Improve').item.json || {};
let st = 'pending';
try {
  const rd = ($('Recheck Draft Status').item.json || {}).draft;
  if (rd && rd.status) st = rd.status;
} catch (e) {}
if (st !== 'pending') return [];
if (qc.ok !== true || typeof qc.score !== 'number') return [];
const score = Math.max(1, Math.min(10, Math.round(qc.score)));
const flags = Array.isArray(qc.flags) ? qc.flags : [];
const reason = (flags.length ? flags.join(', ').replace(/_/g, ' ')
               : String(qc.summary || '')).slice(0, 80);
let badge;
if (score >= 8) {
  badge = '🟢 ' + score + '/10';
} else if (score >= 5) {
  badge = '🟡 ' + score + '/10' + (reason ? ' — ' + reason : '');
} else {
  badge = '🔴 ' + score + '/10' + (reason ? ' — ' + reason : '') + ' · tap 🔄 Regen';
}
const tp = $('Queue & Format').item.json.telegram_payload;
const sid = $('Save Telegram MsgID').item.json;
return [{ json: {
  draft_id: sid.draft_id,
  chat_id: tp.chat_id,
  message_id: sid.telegram_message_id,
  text: (badge + '\n' + tp.text).slice(0, 4000),
  reply_markup: tp.reply_markup
} }];"""

IMPROVE_COMMIT_BODY = (
    '={ "action": "regen-commit", "draft_id": {{ JSON.stringify($json.draft_id) }}, '
    '"fields": {}, "card": { "chat_id": {{ JSON.stringify($json.chat_id) }}, '
    '"message_id": {{ JSON.stringify($json.message_id) }}, '
    '"text": {{ JSON.stringify($json.text) }}, '
    '"reply_markup": {{ JSON.stringify($json.reply_markup) }} } }')

DELETE_NODES = ["Push Improved Text"]


def edit(nb, label):
    # Improve Wait: 20 -> 5
    iw = nb.get("Improve Wait")
    if not iw or iw["parameters"].get("amount") != 20:
        sys.exit(f"x {label}: 'Improve Wait' amount != 20 (got {iw and iw['parameters'].get('amount')!r})")
    iw["parameters"]["amount"] = 5

    # Hermes Improve: /improve -> /quality-check
    hi = nb.get("Hermes Improve")
    if not hi or not str(hi["parameters"].get("url", "")).endswith("/improve"):
        sys.exit(f"x {label}: 'Hermes Improve' url not .../improve")
    hi["parameters"]["url"] = hi["parameters"]["url"].replace("/improve", "/quality-check")

    # Improve Commit: regen-commit fields{draft_text} -> fields{} card-only
    ic = nb.get("Improve Commit")
    if not ic or "regen-commit" not in str(ic["parameters"].get("jsonBody", "")) \
            or "draft_text" not in str(ic["parameters"].get("jsonBody", "")):
        sys.exit(f"x {label}: 'Improve Commit' jsonBody not the expected regen-commit")
    ic["parameters"]["jsonBody"] = IMPROVE_COMMIT_BODY

    # Apply Improvement: rewrite -> render badge
    ai = nb.get("Apply Improvement")
    if not ai or "improved by Hermes" not in ai["parameters"].get("jsCode", ""):
        sys.exit(f"x {label}: 'Apply Improvement' missing expected marker (drift)")
    ai["parameters"]["jsCode"] = RENDER_BADGE
    return {"Apply Improvement": RENDER_BADGE}


def delete_nodes(wf, label):
    present = {n["name"] for n in wf["nodes"]}
    for nm in DELETE_NODES:
        if nm not in present:
            sys.exit(f"x {label}: delete target {nm!r} not found")
    wf["nodes"] = [n for n in wf["nodes"] if n["name"] not in DELETE_NODES]
    conns = wf["connections"]
    for nm in DELETE_NODES:
        conns.pop(nm, None)
    for src, c in conns.items():
        for branch in c.get("main", []):
            if branch:
                branch[:] = [l for l in branch if l.get("node") not in DELETE_NODES]


def verify_conns(wf, label):
    names = {n["name"] for n in wf["nodes"]}
    for src, c in wf["connections"].items():
        if src not in names:
            sys.exit(f"x {label}: connection source {src!r} has no node")
        for branch in c.get("main", []):
            for l in (branch or []):
                if l.get("node") not in names:
                    sys.exit(f"x {label}: dangling link {src} -> {l.get('node')!r}")
    print(f"   ✓ {label}: connections intact; {len(wf['nodes'])} nodes")


def syntax_check(changed):
    node_bin = shutil.which("node") or str(Path.home() / ".local/node/bin/node")
    for name, code in changed.items():
        wrapped = "async function __n8n(){\n" + code + "\n}\n"
        with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False) as f:
            f.write(wrapped)
            tmp = f.name
        r = subprocess.run([node_bin, "--check", tmp], capture_output=True, text=True)
        Path(tmp).unlink(missing_ok=True)
        if r.returncode != 0:
            sys.exit(f"x syntax error in node {name!r}:\n{r.stderr}")
        print(f"   ✓ syntax OK: {name}")


def main():
    deploy = "--deploy" in sys.argv
    print(f"=== {TAG} ({'DEPLOY' if deploy else 'DRY-RUN'}) ===")
    local = json.loads(LOCAL.read_text())
    changed = edit({n["name"]: n for n in local["nodes"]}, "local")
    delete_nodes(local, "local")
    print("edited: Improve Wait(20->5), Hermes Improve(->/quality-check), "
          "Apply Improvement(badge), Improve Commit(fields:{}); deleted Push Improved Text")
    syntax_check(changed)
    verify_conns(local, "local")
    if not deploy:
        print("\nDRY-RUN OK. Re-run with --deploy to push.")
        return
    print("\n--- deploying to live ---")
    n = N8N()
    wf = n.get_workflow()
    edit({nd["name"]: nd for nd in wf["nodes"]}, "live")
    delete_nodes(wf, "live")
    verify_conns(wf, "live")
    n.safe_put(wf, tag=TAG)
    LOCAL.write_text(json.dumps(local, indent=2, ensure_ascii=True) + "\n")
    print(f"   local file updated -> {LOCAL.name}")
    print("\nDEPLOYED. Test: send a customer msg → ~5s + Hermes(~7s) later the")
    print("card header gets a 🟢/🟡/🔴 N/10 badge; draft text unchanged; no rewrite.")


if __name__ == "__main__":
    main()
