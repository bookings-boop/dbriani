#!/usr/bin/env python3
"""
build_answer_callback_continue.py — make Answer Callback continue on error
so the "Draft Exists?" expired-card guard is always reachable.

THE PROBLEM
  Answer Callback runs Telegram answerCallbackQuery and sits UPSTREAM of
  the Draft Exists? guard. With the default onError (stopWorkflow), a
  failed answerCallbackQuery — e.g. a stale/expired Telegram callback id —
  hard-stops the execution BEFORE the guard runs. The operator then gets a
  silent error instead of the "draft expired" notice the guard exists for.

THE FIX
  Set Answer Callback onError = continueRegularOutput. answerCallbackQuery
  is cosmetic (it just stops the button's loading spinner); if it fails the
  execution must still proceed to Draft Exists? -> Route Action / Notify
  Draft Expired. Every downstream node reads $('Parse Callback'), not
  Answer Callback's output, so a degraded Answer Callback item is harmless.

  1 node property changed. 0 new nodes, 0 connection changes.
  Deploys via n8n_deploy.safe_put (staticData re-fetched, no clobber).

Usage:
  python3 scripts/build_answer_callback_continue.py            # dry run
  python3 scripts/build_answer_callback_continue.py --deploy
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from n8n_deploy import N8N, DeployError  # noqa: E402

TARGET = "Answer Callback"
NEW_ONERROR = "continueRegularOutput"


def main():
    deploy = "--deploy" in sys.argv
    print("=== build_answer_callback_continue.py ===")
    n = N8N()
    print(f"1. n8n API: {n.api}")
    wf = n.get_workflow()
    node = next((x for x in wf["nodes"] if x["name"] == TARGET), None)
    if not node:
        raise DeployError(f"{TARGET!r} node not found")

    cur = node.get("onError")
    print(f"2. {TARGET!r} onError: {cur!r}  ->  {NEW_ONERROR!r}")
    if cur == NEW_ONERROR:
        print("   already applied — nothing to do.")
        return
    if cur not in (None, "stopWorkflow"):
        raise DeployError(f"unexpected current onError {cur!r} — aborting")

    node["onError"] = NEW_ONERROR
    print("3. change: 1 node property  ·  0 new nodes  ·  0 connection changes")

    if not deploy:
        print("\nDRY RUN — nothing deployed. Re-run with --deploy to safe_put.")
        return

    print("4. deploying via n8n_deploy.safe_put...")
    final = n.safe_put(wf, tag="ANSWERCB")
    fn = next((x for x in final.get("nodes", []) if x["name"] == TARGET), {})
    ok = fn.get("onError") == NEW_ONERROR
    print(f"5. VERIFY: {TARGET!r} onError={fn.get('onError')!r}  ok={ok}  "
          f"nodes={len(final.get('nodes', []))}  active={final.get('active')}")
    print("DEPLOYED — Answer Callback continues to the guard even if "
          "answerCallbackQuery fails." if ok
          else "x VERIFY FAILED — inspect; PRE-ANSWERCB backup saved.")


if __name__ == "__main__":
    try:
        main()
    except DeployError as e:
        sys.exit(f"x {e}")
