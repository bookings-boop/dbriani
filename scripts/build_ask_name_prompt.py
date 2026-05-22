#!/usr/bin/env python3
"""
build_ask_name_prompt.py — feature-header (system-prompt side).

Adds one §5 instruction to the LIVE draft's system prompt: ask the customer's
name naturally after 2-3 exchanges if they have not introduced themselves.

WHY a workflow change: the live `Claude AI` draft node uses a ~30 KB system
prompt embedded as a value in the `Build Prompt` Set node — NOT the bridge's
system-prompt.md. The same line is added to system-prompt.md separately (it
feeds the bridge's /improve etc.); this script patches the embedded copy so
the instruction actually reaches live drafts.

Modifies one Set-node assignment. Deploys via n8n_deploy.safe_put.
Refs: docs/feature-header-plan.md Task 8 / Q4.

Usage:
  python3 scripts/build_ask_name_prompt.py            # dry run
  python3 scripts/build_ask_name_prompt.py --deploy
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from n8n_deploy import N8N, DeployError  # noqa: E402

ANCHOR = "People book on emotion and trust."
LINE = ("\n\nIf the customer hasn't given their name and you're 2-3 messages "
        "into the conversation, ask for it naturally — e.g. \"By the way, who "
        "am I speaking with?\" — woven into a normal reply, never as a "
        "standalone question.")
MARKER = "By the way, who am I speaking with?"   # idempotency check


def main():
    deploy = "--deploy" in sys.argv
    print("=== build_ask_name_prompt.py ===")
    n = N8N()
    print(f"1. n8n API: {n.api}")
    wf = n.get_workflow()
    bp = next((x for x in wf["nodes"] if x["name"] == "Build Prompt"), None)
    if bp is None:
        raise DeployError("required node 'Build Prompt' not found")

    asg = (bp["parameters"].get("assignments") or {}).get("assignments") or []
    sp = next((a for a in asg if a.get("name") == "systemPrompt"), None)
    if sp is None:
        raise DeployError("Build Prompt has no 'systemPrompt' assignment")

    val = sp.get("value", "")
    if MARKER in val:
        print("2. already applied — the ask-name line is present. nothing to do.")
        return
    if ANCHOR not in val:
        raise DeployError("Build Prompt systemPrompt: §5 anchor not found "
                          "— aborting")
    if val.count(ANCHOR) != 1:
        raise DeployError(f"§5 anchor is not unique ({val.count(ANCHOR)}x) "
                          "— aborting")
    sp["value"] = val.replace(ANCHOR, ANCHOR + LINE, 1)

    print(f"2. Build Prompt systemPrompt: {len(val)} -> {len(sp['value'])} chars")
    print("3. inserted the §5 ask-name instruction after the rapport line")

    if not deploy:
        print("\nDRY RUN — anchor matched (unique). Nothing deployed. "
              "Re-run with --deploy.")
        return

    print("4. deploying via n8n_deploy.safe_put...")
    final = n.safe_put(wf, tag="ASKNAME")
    fbp = next((x for x in final.get("nodes", []) if x["name"] == "Build Prompt"),
               None)
    fasg = (fbp["parameters"].get("assignments") or {}).get("assignments") or []
    fval = next((a["value"] for a in fasg if a.get("name") == "systemPrompt"), "")
    ok = MARKER in fval
    print(f"5. VERIFY: ask-name line present in live Build Prompt={ok}, "
          f"nodes={len(final.get('nodes', []))}, active={final.get('active')}")
    print("ASK-NAME INSTRUCTION DEPLOYED to the live draft prompt." if ok
          else "x VERIFY FAILED — inspect; PRE-ASKNAME backup saved.")


if __name__ == "__main__":
    try:
        main()
    except DeployError as e:
        sys.exit(f"x {e}")
