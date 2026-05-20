#!/usr/bin/env python3
"""
build_workflow.py - Dubriani Phase 1B workflow builder.

Generates the single, COMMIT-SAFE N8N workflow from one canonical source.

Inputs
------
  * system-prompt.md                         canonical agent system prompt
  * workflows/phase-1b-telegram.json          structural base (node graph)
        ...or workflows/phase-1b-telegram.local.json on the first
        bootstrap run, before the committed file exists.

Output
------
  * workflows/phase-1b-telegram.json          committed workflow, containing:
      - the system prompt embedded in BOTH Set nodes ("Build Prompt" and
        "Build Regen Prompt") from ONE source string, so they cannot drift
      - Anthropic / WAHA / Supermemory  ->  N8N credential references BY NAME
      - Telegram                        ->  {{ $env.TELEGRAM_BOT_TOKEN }}
                                            in every bot API URL
      - NO inline secrets, ever.

Design properties
-----------------
  * Never reads .env. The workflow JSON is a pure function of
    system-prompt.md + the node graph, so no secret can ever reach it.
  * Idempotent. Node IDs and webhookIds are preserved from the base and
    never regenerated; running this twice yields byte-identical output.
  * Aborts if any secret-shaped string survives in the generated output.

Usage:  python3 scripts/build_workflow.py
"""
import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROMPT_FILE = ROOT / "system-prompt.md"
COMMITTED_FILE = ROOT / "workflows" / "phase-1b-telegram.json"
LOCAL_BASE = ROOT / "workflows" / "phase-1b-telegram.local.json"

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
TELEGRAM_HOST = "api.telegram.org"
WAHA_MARKER = "/api/sendText"
SUPERMEMORY_HOST = "api.supermemory.ai"
HERMES_BRIDGE_MARKER = ":8788/"  # matches /draft and /save-rule

# Credential names referenced by the committed workflow. These must match the
# names of the credentials created in N8N (see docs/credentials-map.md).
CRED = {
    "anthropic":     ("httpHeaderAuth", "Anthropic API"),
    "waha":          ("httpHeaderAuth", "WAHA API"),
    "supermemory":   ("httpBearerAuth", "Bearer Auth account"),
    "hermes_bridge": ("httpHeaderAuth", "Hermes Bridge"),
}

# Strings that must NEVER appear in the committed workflow.
SECRET_PATTERNS = [
    r"sk-ant-",                          # Anthropic key
    r"Ev0-Dub-",                         # WAHA key
    r"/bot\d{6,}:[A-Za-z0-9_-]{20,}/",   # Telegram bot token inside a URL
]


def classify(node):
    """Return 'anthropic' | 'waha' | 'telegram' | 'supermemory' | None."""
    url = node.get("parameters", {}).get("url", "")
    bare = url.lstrip("=")
    if bare == ANTHROPIC_URL:
        return "anthropic"
    if TELEGRAM_HOST in bare:
        return "telegram"
    if WAHA_MARKER in bare:
        return "waha"
    if SUPERMEMORY_HOST in bare:
        return "supermemory"
    if HERMES_BRIDGE_MARKER in bare:
        return "hermes_bridge"
    return None


def strip_header(params, name):
    hp = params.get("headerParameters", {}).get("parameters", [])
    params.setdefault("headerParameters", {})["parameters"] = [
        h for h in hp if h.get("name", "").lower() != name.lower()
    ]


def use_credential(node, kind):
    """Rewire an HTTP node to authenticate via an N8N credential, by name.
    `id` is null on purpose: N8N matches the credential by name on import."""
    cred_type, cred_name = CRED[kind]
    p = node["parameters"]
    p["authentication"] = "genericCredentialType"
    p["genericAuthType"] = cred_type
    node["credentials"] = {cred_type: {"id": None, "name": cred_name}}


def externalize(node, kind):
    p = node["parameters"]
    if kind == "anthropic":
        strip_header(p, "x-api-key")
        use_credential(node, "anthropic")
    elif kind == "waha":
        strip_header(p, "X-Api-Key")
        use_credential(node, "waha")
    elif kind == "supermemory":
        # Already credential-backed in the delivered workflow; normalise the
        # reference to name-only so it does not depend on a stored id.
        use_credential(node, "supermemory")
    elif kind == "hermes_bridge":
        # The Call Hermes Bridge node(s): authenticate via the "Hermes Bridge"
        # httpHeaderAuth credential (header X-Bridge-Token).
        use_credential(node, "hermes_bridge")
    elif kind == "telegram":
        # Swap whatever sits between '/bot' and the next '/' for the $env
        # expression; '=' marks the whole url field as an N8N expression.
        new = re.sub(r"/bot[^/]+/", "/bot{{ $env.TELEGRAM_BOT_TOKEN }}/",
                     p["url"].lstrip("="), count=1)
        p["url"] = "=" + new


def inject_prompt(wf, prompt_text):
    """Embed the canonical prompt into every Set node field named
    'systemPrompt'. Returns the count (expected: 2)."""
    count = 0
    for node in wf.get("nodes", []):
        if node.get("type") != "n8n-nodes-base.set":
            continue
        for a in node.get("parameters", {}).get("assignments", {}).get("assignments", []):
            if a.get("name") == "systemPrompt":
                a["value"] = prompt_text
                count += 1
    return count


def scan_for_secrets(text):
    hits = []
    for pat in SECRET_PATTERNS:
        hits += [m.group(0)[:24] for m in re.finditer(pat, text)]
    return hits


def main():
    ap = argparse.ArgumentParser(description="Build the committed Dubriani Phase 1B workflow.")
    ap.add_argument("--externalize", action="store_true",
                    help="Accepted for compatibility; this is the only mode.")
    ap.parse_args()

    if not PROMPT_FILE.exists():
        sys.exit(f"x {PROMPT_FILE} not found.")
    prompt_text = PROMPT_FILE.read_text()

    base = COMMITTED_FILE if COMMITTED_FILE.exists() else LOCAL_BASE
    if not base.exists():
        sys.exit("x No base workflow found (neither phase-1b-telegram.json "
                 "nor phase-1b-telegram.local.json).")
    wf = json.loads(base.read_text())

    n_prompt = inject_prompt(wf, prompt_text)
    # After the Hermes-bridge rewiring (Step 3d) the workflow no longer embeds
    # the system prompt - Hermes reads it on the box at
    # ~/hermes-bridge/system-prompt.md. 0 is the post-3d normal; 2 is the
    # legacy direct-Claude layout. Anything else means drift.
    if n_prompt not in (0, 2):
        print(f"!  systemPrompt injected into {n_prompt} Set node(s) - expected 0 or 2.")

    touched = {"anthropic": 0, "waha": 0, "telegram": 0,
               "supermemory": 0, "hermes_bridge": 0}
    for node in wf.get("nodes", []):
        kind = classify(node)
        if kind:
            externalize(node, kind)
            touched[kind] += 1

    text = json.dumps(wf, indent=2, ensure_ascii=False) + "\n"
    hits = scan_for_secrets(text)
    if hits:
        sys.exit(f"x ABORT - secret-shaped strings survived externalization: {hits}")

    COMMITTED_FILE.write_text(text)

    print(f"OK  {COMMITTED_FILE.relative_to(ROOT)}  [commit-safe, no secrets]")
    print(f"    base: {base.name}   nodes: {len(wf.get('nodes', []))}")
    print(f"    credential refs -> anthropic={touched['anthropic']} "
          f"waha={touched['waha']} supermemory={touched['supermemory']} "
          f"hermes_bridge={touched['hermes_bridge']}")
    print(f"    telegram $env URLs -> {touched['telegram']}")
    print(f"    systemPrompt injected into {n_prompt} Set node(s)")
    print(f"    secret scan: clean")


if __name__ == "__main__":
    main()
