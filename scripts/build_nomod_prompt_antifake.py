#!/usr/bin/env python3
"""
build_nomod_prompt_antifake.py — Nomod follow-up: forbid URL placeholders in
Hermes's reply text.

Bug discovered during e2e verify: when the customer said "yes send payment
link" but Hermes wasn't ready to commit `should_send_payment=true` (still
confirming the price), her reply contained the literal placeholder
"[pay.nomodapp.com link]". The operator pressed Send -> WhatsApp linkified
the bare domain -> the customer tapped through to Nomod's generic homepage.

Fix is prompt-only: add an explicit clause to the payment-link signal block
in Build Prompt's embedded systemPrompt — Hermes must never paste or invent
a URL in her reply, the workflow appends the real link.

Usage:
  python3 scripts/build_nomod_prompt_antifake.py            # dry run
  python3 scripts/build_nomod_prompt_antifake.py --deploy
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from n8n_deploy import N8N, DeployError  # noqa: E402

OLD = ("warm, lowercase; add urgency only if the conversation genuinely "
       "calls for it.\n")

NEW = OLD + (
    "\n**Never paste or invent a URL in your reply** — not "
    "`pay.nomodapp.com`, not `[link]`, no markdown link, nothing. The "
    "workflow appends the real Nomod link automatically, and ONLY when "
    "`should_send_payment` is `true`. Any URL text in your `messages` is "
    "sent to the customer as-is and creates a broken link. When you DO "
    "trigger payment, your reply just confirms warmly — the system adds "
    "the booking summary + AED total + the real link under your reply. "
    "When you DON'T trigger it (still confirming details), simply ask in "
    "words — no link, no placeholder.\n")


def main():
    deploy = "--deploy" in sys.argv
    print("=== build_nomod_prompt_antifake.py ===")
    n = N8N()
    print(f"1. n8n API: {n.api}")
    wf = n.get_workflow()
    by = {x["name"]: x for x in wf["nodes"]}
    if "Build Prompt" not in by:
        raise DeployError("Build Prompt not found")
    asg = by["Build Prompt"]["parameters"]["assignments"]["assignments"]
    sp = next((a for a in asg if a["name"] == "systemPrompt"), None)
    if sp is None:
        raise DeployError("Build Prompt: systemPrompt assignment not found")
    if "Never paste or invent a URL" in sp["value"]:
        print("2. already applied — nothing to do.")
        return
    if OLD not in sp["value"]:
        raise DeployError("Build Prompt: payment-section anchor not found "
                          f"(searched for {OLD!r})")
    sp["value"] = sp["value"].replace(OLD, NEW, 1)
    print(f"2. Build Prompt systemPrompt: +{len(NEW) - len(OLD)} chars "
          "(anti-hallucination clause appended)")

    if not deploy:
        print("\nDRY RUN — anchor matched. Nothing deployed. "
              "Re-run with --deploy.")
        return

    print("3. deploying via n8n_deploy.safe_put...")
    final = n.safe_put(wf, tag="NOMODPROMPTFIX")
    by_f = {x["name"]: x for x in final.get("nodes", [])}
    asg_f = (by_f.get("Build Prompt", {}).get("parameters", {})
             .get("assignments", {}).get("assignments", []))
    spf = next((a for a in asg_f if a["name"] == "systemPrompt"), None)
    ok = bool(spf and "Never paste or invent a URL" in spf["value"])
    print(f"4. VERIFY: anti-URL clause present={ok}, "
          f"nodes={len(final.get('nodes', []))}, active={final.get('active')}")
    print("NOMOD PROMPT FIX DEPLOYED."
          if ok else "x VERIFY FAILED — PRE-NOMODPROMPTFIX backup saved.")


if __name__ == "__main__":
    try:
        main()
    except DeployError as e:
        sys.exit(f"x {e}")
