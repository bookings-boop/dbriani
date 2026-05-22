#!/usr/bin/env python3
"""
build_prompt_calibration.py — mirror the 7 behavioural calibration edits from
system-prompt.md into Build Prompt's embedded systemPrompt (the string the
live drafting Claude AI node actually reads).

Issues fixed (from operator observation of production drafts):
  1. Verbosity mismatch — match customer message length.
  2. File / PDF delivery honesty — don't promise files Hermes can't send.
  3. Phone-call push — reverse the proposal-recommend-call hard rule.
  4. Name overuse — max 2 times per conversation.
  5. Feature stacking — narrow with one question before listing options.
  6. Fabricating under uncertainty — "let me confirm" beats false confidence.
  7. Multi-thread messages — one thread per message.

Each edit is anchored on a unique pre-existing string; the script aborts if
any anchor doesn't match (so re-runs are safe and structural drift surfaces
loudly).

Usage:
  python3 scripts/build_prompt_calibration.py            # dry run
  python3 scripts/build_prompt_calibration.py --deploy
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from n8n_deploy import N8N, DeployError  # noqa: E402

EDITS = [
    # 1 — Hard Rule 5: reverse the phone-call push
    (
        "5. **For proposals, recommend a phone call within the first 3 "
        "messages.** Proposal occasion has 0% chat-only conversion — text "
        "alone doesn't close emotional high-ticket bookings.",
        "5. **Never offer a phone call unsolicited.** Dubriani sells via "
        "text — Maria is the text channel. Only mention a call if the "
        "customer explicitly asks for one. For proposals, keep the "
        "conversation in text: describe the experience vividly, lean on the "
        "trust signals. If a call is genuinely needed, the operator makes "
        "it manually.",
    ),
    # 2 — Length rules: add length-matching + one-thread
    (
        "### Length rules\n"
        "- Qualifying questions and acknowledgements: short (under 80 chars)."
        " \"Sweet! What's your date and pax?\" / \"Got it!\" / \"Allow me to"
        " check.\"\n"
        "- Work messages: as long as they need to be. A proper 3-yacht "
        "recommendation with URLs and prices is 200–400 chars and that's "
        "correct. Don't truncate.",
        "### Length rules\n"
        "- **Match the customer's message length.** A 5-word question gets "
        "a one-or-two-sentence answer. Short questions = short answers. "
        "Don't unpack what they didn't ask. Let the customer pull more if "
        "they want more.\n"
        "- **One thread per message.** Don't stack multiple topics, "
        "questions, or upsells. Address what was asked, plus at most one "
        "focused follow-up — never a wall of options.\n"
        "- Qualifying questions and acknowledgements: short (under 80 chars)."
        " \"Sweet! What's your date and pax?\" / \"Got it!\" / \"Allow me to"
        " check.\"\n"
        "- Work messages CAN run long when the situation calls for it (a "
        "proper 3-yacht recommendation is 200–400 chars), but the customer's"
        " signal length is the default ceiling. If you're drafting a "
        "paragraph in reply to a one-line question, you're doing it wrong.",
    ),
    # 3 — Voice & Tone: rein in name use
    (
        "- Use the customer's name as soon as you have it. Ask for it on "
        "first reply if missing: \"May I take your name so I can address "
        "you correctly?\"",
        "- Use the customer's name **sparingly** — maximum two times per "
        "conversation: once to acknowledge them after they share it, once "
        "near the close. In between, no name. Over-using a name reads as "
        "scripted and salesy. (Section 5 covers asking for the name "
        "naturally if missing — don't ask twice.)",
    ),
    # 4 — Mandatory Qualification: narrow before listing
    (
        "Build rapport before quoting prices. Ask 1–2 interested questions "
        "about what they're planning. People book on emotion and trust.",
        "Build rapport before quoting prices. Ask 1–2 interested questions "
        "about what they're planning. People book on emotion and trust.\n\n"
        "**Narrow before listing.** When a customer says something vague "
        "(\"something nice\", \"looking for a yacht\", \"for a special "
        "date\"), ask **ONE** clarifying question first — don't dump "
        "options or add-ons. Pick the most useful question for their "
        "context (date? occasion? guests?) and stop there. Catalog dumps "
        "lose customers; focused questions win them.",
    ),
    # 5 — Recommendation Rule: fix the PDF promise
    (
        "For each yacht, the **3 mandatory trust signals** (non-negotiable):"
        "\n- 📄 Branded PDF brochure\n"
        "- 📍 Google Business / GMB link with reviews\n"
        "- 🎥 Yacht video or Instagram reel",
        "For each yacht, the **3 trust signals** to mention (when they add "
        "something — not on every reply):\n"
        "- 📄 Branded PDF brochure — operator-sent. You can say *\"i'll have"
        " the brochure sent across shortly\"* but **never** *\"sending now"
        "\"* (you can't attach files yourself).\n"
        "- 📍 Google Business / GMB link with reviews — share the link "
        "directly in text.\n"
        "- 🎥 Yacht video or Instagram reel — share the link directly in "
        "text.\n\n"
        "When the customer asks about a specific yacht detail, **describe "
        "it in text first**. Only mention the brochure / video / reviews "
        "when they genuinely help — don't bundle them into every reply.",
    ),
    # 6 — Multi-day Itineraries: PDF wording
    (
        "4 itineraries on file (mention when client asks for \"trip\", "
        "\"cruise\", \"multi-day\", or stays > 1 day). Flag in "
        "`notes_for_zayn` so Zayn can send the relevant PDF:",
        "4 itineraries on file (mention when client asks for \"trip\", "
        "\"cruise\", \"multi-day\", or stays > 1 day). Flag in "
        "`notes_for_zayn` so Zayn can send the relevant PDF — your text "
        "reply describes the route in 1–2 sentences and **never** promises "
        "to attach the PDF yourself:",
    ),
    # 7 — What You DON'T Do: append 4 new don'ts
    (
        "- Don't promise outdoor balloon decor.",
        "- Don't promise outdoor balloon decor.\n"
        "- **Don't promise to send files (PDF, image, document, video) "
        "\"now\" or attach them yourself — you can't.** If the file exists "
        "and the operator will follow up, say *\"i'll have it sent across "
        "shortly\"*. If you're not sure the file exists, describe the "
        "contents in text. Never fabricate documents.\n"
        "- **Don't over-promise under uncertainty.** When something isn't "
        "fully confirmed (pricing, availability, an add-on detail), *\"let "
        "me confirm and get back to you\"* is the right move. Honesty beats "
        "false confidence.\n"
        "- **Don't push phone calls.** Hard Rule 5 covers it.\n"
        "- **Don't repeat the customer's name in every message.** Max two "
        "times per conversation (§4).",
    ),
]

# A sentinel string that only the NEW version contains — used for idempotency.
ALREADY_APPLIED_MARKER = "Never offer a phone call unsolicited"


def main():
    deploy = "--deploy" in sys.argv
    print("=== build_prompt_calibration.py ===")
    n = N8N()
    print(f"1. n8n API: {n.api}")
    wf = n.get_workflow()
    bp = next((x for x in wf["nodes"] if x["name"] == "Build Prompt"), None)
    if bp is None:
        raise DeployError("Build Prompt node not found")
    asg = bp["parameters"]["assignments"]["assignments"]
    sp = next((a for a in asg if a["name"] == "systemPrompt"), None)
    if sp is None:
        raise DeployError("Build Prompt: systemPrompt assignment not found")

    if ALREADY_APPLIED_MARKER in sp["value"]:
        print("2. already applied — nothing to do.")
        return

    before_len = len(sp["value"])
    for i, (old, new) in enumerate(EDITS, 1):
        if old not in sp["value"]:
            raise DeployError(
                "edit #%d anchor not found in Build Prompt systemPrompt — "
                "first 80 chars: %r" % (i, old[:80]))
        sp["value"] = sp["value"].replace(old, new, 1)
        print(f"2.{i} edit applied (+{len(new) - len(old)} chars)")

    after_len = len(sp["value"])
    print(f"3. Build Prompt systemPrompt: {before_len} -> {after_len} chars "
          f"(+{after_len - before_len})")

    if not deploy:
        print("\nDRY RUN — all 7 anchors matched. Nothing deployed. "
              "Re-run with --deploy.")
        return

    print("4. deploying via n8n_deploy.safe_put...")
    final = n.safe_put(wf, tag="PROMPTCAL")
    by_f = {x["name"]: x for x in final.get("nodes", [])}
    asg_f = (by_f.get("Build Prompt", {}).get("parameters", {})
             .get("assignments", {}).get("assignments", []))
    spf = next((a for a in asg_f if a["name"] == "systemPrompt"), None)
    ok = bool(spf and ALREADY_APPLIED_MARKER in spf["value"])
    print(f"5. VERIFY: calibration marker present={ok}, "
          f"nodes={len(final.get('nodes', []))}, active={final.get('active')}")
    print("PROMPT CALIBRATION DEPLOYED."
          if ok else "x VERIFY FAILED — PRE-PROMPTCAL backup saved.")


if __name__ == "__main__":
    try:
        main()
    except DeployError as e:
        sys.exit(f"x {e}")
