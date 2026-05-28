#!/usr/bin/env python3
"""
build_debounce_two_tier.py — replace the fixed 60s inbound debounce with a
two-tier wait: 5s for booking-signal messages, 15s otherwise.

The bridge /debounce buffer+flush dedup logic is UNCHANGED — only the wait
duration before flush changes, and it's now computed per-message:
  • Buffer Message (code): classify wh.body -> wait_seconds (5 | 15)
  • Debounce Wait (wait):  amount 60 -> ={{ Buffer Message.wait_seconds }}, unit seconds

USAGE:
  python3 scripts/build_debounce_two_tier.py            # dry-run + syntax
  python3 scripts/build_debounce_two_tier.py --deploy   # deploy + write local
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
TAG = "DEBOUNCE_2TIER"

BUFFER_MESSAGE_NEW = r"""// FR-3 (BUG-1 fix): the bridge buffered this message in Redis and issued
// a token. staticData is NOT used — it cannot coordinate the concurrent
// executions that one-per-inbound-message spawns.
const wh = $('WAHA Webhook').item.json.body.payload;
const r = $('Debounce Buffer').item.json || {};
// Two-tier debounce: booking-signal messages flush fast (5s) so hot leads
// get a near-instant reply; everything else waits 15s to let the customer
// finish a multi-message thought before we draft. (date-within-24h is
// proxied by today/tonight/this evening/same day — no date parsing here.)
const text = String(wh.body || '').toLowerCase();
const BOOKING_SIGNAL = /\b(today|tonight|tonite|this evening|same.?day|right now|asap|how much|price|prices|pricing|cost|rate|rates|quote|deposit|let'?s book|book (it|now|this|the)|i'?ll take it)\b/;
const wait_seconds = BOOKING_SIGNAL.test(text) ? 5 : 15;
return { json: {
  phone: wh.from,
  token: r.token || '',
  degraded: !r.token,       // bridge/Redis error -> Flush Check fails open
  wait_seconds: wait_seconds
} };"""

WAIT_EXPR = "={{ $('Buffer Message').item.json.wait_seconds }}"


def edit(nodes_by_name, label):
    # 1. Buffer Message — full replace (guarded by markers)
    bm = nodes_by_name.get("Buffer Message")
    if not bm:
        sys.exit(f"x {label}: 'Buffer Message' not found")
    cur = bm["parameters"].get("jsCode", "")
    if "Debounce Buffer" not in cur or "degraded" not in cur:
        sys.exit(f"x {label}: 'Buffer Message' missing expected markers (drift)")
    if "wait_seconds" in cur:
        sys.exit(f"x {label}: 'Buffer Message' already has wait_seconds — already applied")
    bm["parameters"]["jsCode"] = BUFFER_MESSAGE_NEW

    # 2. Debounce Wait — dynamic amount (guard: must currently be 60)
    dw = nodes_by_name.get("Debounce Wait")
    if not dw:
        sys.exit(f"x {label}: 'Debounce Wait' not found")
    if dw.get("type", "").split(".")[-1] != "wait":
        sys.exit(f"x {label}: 'Debounce Wait' is not a wait node")
    amt = dw["parameters"].get("amount")
    if amt != 60:
        sys.exit(f"x {label}: 'Debounce Wait' amount is {amt!r}, expected 60 (drift)")
    dw["parameters"]["amount"] = WAIT_EXPR
    dw["parameters"]["unit"] = "seconds"
    return {"Buffer Message": BUFFER_MESSAGE_NEW}


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
    print("edited: Buffer Message (code) + Debounce Wait (amount 60 -> dynamic 5/15s)")
    syntax_check(changed)
    if not deploy:
        print("\nDRY-RUN OK. Re-run with --deploy to push.")
        return
    print("\n--- deploying to live ---")
    n = N8N()
    wf = n.get_workflow()
    edit({nd["name"]: nd for nd in wf["nodes"]}, "live")
    n.safe_put(wf, tag=TAG)
    LOCAL.write_text(json.dumps(local, indent=2, ensure_ascii=True) + "\n")
    print(f"   local file updated -> {LOCAL.name}")
    print("\nDEPLOYED. Test:")
    print("  • 'any yacht for tonight?' -> 5s debounce -> card in ~10s")
    print("  • 'hi'                     -> 15s debounce -> card in ~20-25s")


if __name__ == "__main__":
    main()
