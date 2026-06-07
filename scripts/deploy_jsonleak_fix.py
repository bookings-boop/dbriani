#!/usr/bin/env python3
"""Fix the raw-JSON draft leak (2026-06-07): when the model returns invalid JSON
(e.g. an unescaped " inside a message), the n8n parsers' fallback dumped the raw
JSON object as the draft -> operator saw braces/quotes and could Send raw JSON.

This:
  1. Patches the 4 parser nodes (Parse Response / Regen / Refine / Lead Response)
     so a malformed-JSON fallback NEVER shows raw JSON — it shows a clean
     "tap Regen" card; plain-text (non-JSON) responses still pass through.
  2. Re-embeds the updated system-prompt.md (+ file-registry.md) into the 4 Build
     Prompt nodes so the new JSON-SAFETY rule (escape inner quotes) reaches the
     drafter.
Applies to the LOCAL workflow file (repo == live) and, with --deploy, pushes to
live via n8n_deploy.safe_put (preserves the live draft queue, NO container
restart) + scp's system-prompt.md to the box for the bridge scorer fallback.

Run:  python3 scripts/deploy_jsonleak_fix.py [--deploy]
"""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from n8n_deploy import N8N  # noqa: E402

WF_LOCAL = ROOT / "workflows" / "phase-1b-telegram.json"
SP = ROOT / "hermes-bridge" / "system-prompt.md"
REG = ROOT / "docs" / "file-registry.md"
PROMPT_NODES = ("Build Prompt", "Build Regen Prompt", "Build Refine Prompt", "Build Lead Prompt")

WARN = ("⚠️ Draft came back malformed (invalid JSON from the model) "
        "— tap \U0001f504 Regen for a clean draft.")

# (node_name, original `parsed = {...}` object expression)
PARSERS = [
    ("Parse Response",
     '{ messages: [raw], notes_for_zayn: "(JSON parse failed - Claude returned plain text. Error: " + e.message + ")" }'),
    ("Parse Regen",
     "{ messages: [raw], notes_for_zayn: '(parse fail: ' + e.message + ')' }"),
    ("Parse Refine",
     "{ messages: [raw], notes_for_zayn: '(parse fail: ' + e.message + ')' }"),
    ("Parse Lead Response",
     "{ messages: [raw], notes_for_zayn: '(JSON parse failed - plain text. ' + e.message + ')' }"),
]


def guarded(orig):
    return (
        "  const _rawLooksJson = /^\\s*[{\\[]/.test(raw);\n"
        "  parsed = _rawLooksJson\n"
        "    ? { messages: ['" + WARN + "'], notes_for_zayn: 'parse fail (raw JSON hidden, len ' + raw.length + '): ' + e.message }\n"
        "    : " + orig + ";"
    )


def combined_prompt():
    p = SP.read_text()
    r = REG.read_text()
    if len(p) < 1000 or len(r) < 100:
        sys.exit("x prompt or registry suspiciously short — refusing")
    return p + "\n\n---\n\n# FILE REGISTRY\n" + r


def apply_edits(wf, combined):
    by = {n.get("name"): n for n in wf.get("nodes", [])}
    notes, failures = [], []
    # 1) parser fallback patches
    for name, orig in PARSERS:
        node = by.get(name)
        if node is None:
            failures.append(f"parser {name!r} NOT FOUND"); continue
        code = (node.get("parameters") or {}).get("jsCode")
        if not isinstance(code, str):
            failures.append(f"parser {name!r} has no jsCode"); continue
        old = "  parsed = " + orig + ";"
        new = guarded(orig)
        if old in code:
            node["parameters"]["jsCode"] = code.replace(old, new, 1)
            notes.append(f"OK parser {name}")
        elif "_rawLooksJson" in code:
            notes.append(f"-- parser {name}: already applied")
        else:
            failures.append(f"parser {name!r}: old fallback not found")
    # 2) prompt re-embed
    for name in PROMPT_NODES:
        node = by.get(name)
        if node is None:
            failures.append(f"prompt node {name!r} NOT FOUND"); continue
        asn = ((node.get("parameters") or {}).get("assignments") or {}).get("assignments") or []
        hit = False
        for a in asn:
            if a.get("name") == "systemPrompt":
                a["value"] = combined; hit = True; break
        notes.append(f"OK prompt {name}" if hit else f"prompt {name!r}: no systemPrompt assignment")
        if not hit:
            failures.append(f"prompt {name!r}: no systemPrompt assignment")
    return notes, failures


def main():
    deploy = "--deploy" in sys.argv
    combined = combined_prompt()

    # --- LOCAL file (repo == live) ---
    local = json.loads(WF_LOCAL.read_text())
    notes, failures = apply_edits(local, combined)
    for n in notes:
        print("  ", n)
    if failures:
        print("\nABORT — failures:")
        for f in failures:
            print("  x", f)
        sys.exit(1)
    WF_LOCAL.write_text(json.dumps(local, indent=2) + "\n")
    print(f"local workflow patched -> {WF_LOCAL.name}")

    if not deploy:
        print("\nDRY RUN — local file patched (git diff to review). Not pushed. Re-run with --deploy.")
        return

    # --- LIVE via safe_put (fetch live, apply, push; preserves staticData) ---
    n = N8N()
    wf = n.get_workflow()
    ln, lf = apply_edits(wf, combined)
    if lf:
        print("\nABORT (live) — failures:")
        for f in lf:
            print("  x", f)
        sys.exit(1)
    print("\npushing to live via safe_put (preserves draft queue, no restart)...")
    n.safe_put(wf, tag="JSONLEAK")
    # bridge scorer fallback also reads system-prompt.md
    subprocess.run(["scp", "-q", str(SP), "dubriani-ec2:~/hermes-bridge/system-prompt.md"], check=True)
    print("scp system-prompt.md -> box OK")
    print("\n✅ json-leak fix deployed.")


if __name__ == "__main__":
    main()
