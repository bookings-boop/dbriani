#!/usr/bin/env python3
"""Merge the other terminal's additive /followup-sweep engine into the repo
(2026-06-07). Base = repo HEAD (all my fixes); graft their ADDITIVE pieces:
  - handle_followup_sweep (extracted from the box routes.py) -> repo routes.py
  - server.py: import + routes-list entry + dispatch elif
The 2 new files (reengage_quote.py, cron-reengage-quote.py) were already scp'd
from the box into the repo. Their function CALLS scan_followup_eligibility /
handle_draft_followup (which in the repo carry my fixes incl. merged-exclusion),
so after this merge their engine inherits the merged-spam fix automatically.

Assertion-guarded; writes only if every graft point is unambiguous.
Run: python3 scripts/merge_followup_sweep.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BOX_ROUTES = Path("/tmp/box_routes.py")
ROUTES = ROOT / "hermes-bridge" / "routes.py"
SERVER = ROOT / "hermes-bridge" / "server.py"
fail = []


def extract_func(text, name):
    lines = text.splitlines(keepends=True)
    start = next((i for i, l in enumerate(lines) if l.startswith(f"def {name}(")), None)
    if start is None:
        return None
    end = next((j for j in range(start + 1, len(lines)) if lines[j].startswith("def ")), len(lines))
    return "".join(lines[start:end]).rstrip() + "\n"


def sub_once(text, old, new, label):
    if text.count(old) != 1:
        fail.append(f"{label}: anchor count={text.count(old)} (need 1)")
        return text
    return text.replace(old, new, 1)


# --- routes.py: graft the function ---
box = BOX_ROUTES.read_text()
func = extract_func(box, "handle_followup_sweep")
if not func:
    sys.exit("x could not extract handle_followup_sweep from box routes.py")
routes = ROUTES.read_text()
if "handle_followup_sweep" in routes:
    print("-- routes.py: handle_followup_sweep already present, skipping")
else:
    routes = sub_once(routes, "def handle_learn(payload, send):",
                      func + "\n\n" + "def handle_learn(payload, send):",
                      "routes.py function insert")
    if not fail:
        ROUTES.write_text(routes)
        print(f"OK routes.py: grafted handle_followup_sweep ({func.count(chr(10))} lines)")

# --- server.py: import + routes-list + dispatch ---
server = SERVER.read_text()
if "handle_followup_sweep" in server:
    print("-- server.py: handle_followup_sweep already wired, skipping")
else:
    server = sub_once(server, "    handle_followup_action,\n",
                      "    handle_followup_action,\n    handle_followup_sweep,\n",
                      "server.py import")
    server = sub_once(server, '                             "/followup-action",\n',
                      '                             "/followup-action", "/followup-sweep",\n',
                      "server.py routes-list")
    server = sub_once(server,
                      '        elif self.path == "/followup-action":\n'
                      '            handle_followup_action(payload, self._send)\n',
                      '        elif self.path == "/followup-action":\n'
                      '            handle_followup_action(payload, self._send)\n'
                      '        elif self.path == "/followup-sweep":\n'
                      '            handle_followup_sweep(payload, self._send)\n',
                      "server.py dispatch")
    if not fail:
        SERVER.write_text(server)
        print("OK server.py: grafted import + routes-list + dispatch")

if fail:
    print("\nABORT — graft anchors not unique; no partial write trusted:")
    for f in fail:
        print("  x", f)
    sys.exit(1)
print("\nmerge complete (repo).")
