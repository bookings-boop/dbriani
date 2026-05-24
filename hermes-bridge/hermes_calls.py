#!/usr/bin/env python3
"""hermes_calls.py — primitives for invoking the Hermes CLI agent.

Higher-level domain wrappers (extract_customer_facts, hermes_analyze_lead,
classify_feedback, build_query) live with their feature modules and call
these primitives.

Hermes is a CLI tool we shell into rather than a library — keeps the
bridge runtime free of model-client dependencies and lets us pin the
Hermes version independently of the bridge."""
import json
import os
import re
import subprocess
import time

from util import log  # noqa: F401 — re-exported


# --- paths + timeouts ----------------------------------------------
HOME = os.path.expanduser("~")
HERMES = os.path.join(HOME, ".local", "bin", "hermes")
HERMES_TIMEOUT = int(os.environ.get("BRIDGE_HERMES_TIMEOUT", "120"))

# Output parsing regexes (compiled once at import).
SESSION_RE = re.compile(r"session_id:\s*(\S+)")
FENCE_RE = re.compile(r"```(?:json)?", re.IGNORECASE)


def run_hermes(query, timeout=None):
    """Invoke `hermes chat -q <query>` in YOLO mode, return
    (returncode, stdout, stderr, elapsed_ms). The bridge always uses
    --source tool (so Hermes knows it's being called programmatically)
    and -Q (no-prompt-on-tool-confirm). PATH is prepended with the
    hermes bin dir so any sub-tools Hermes spawns find their siblings.

    Caller is responsible for parsing the JSON Hermes returns —
    use extract_json() below. Caller also picks the timeout; we default
    to HERMES_TIMEOUT (~2min) for drafting which can take that long
    on a cold model server."""
    if timeout is None:
        timeout = HERMES_TIMEOUT
    cmd = [HERMES, "--profile", "default", "chat", "-q", query, "-Q",
           "--source", "tool", "--yolo", "-t", "memory"]
    log("hermes call:", " ".join(c for c in cmd if c != query))
    env = dict(os.environ)
    env["PATH"] = os.path.dirname(HERMES) + os.pathsep + env.get("PATH", "")
    t0 = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          timeout=timeout, env=env)
    return proc.returncode, proc.stdout, proc.stderr, \
        int((time.time() - t0) * 1000)


def extract_json(stdout):
    """Pull the outermost {...} JSON object out of Hermes stdout.
    Hermes wraps responses in chat-formatted text + optional
    ```json fences; we strip the fences and take the first complete
    brace pair. Returns (parsed_dict_or_None, raw_blob_or_text)."""
    text = FENCE_RE.sub("", stdout)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None, text.strip()
    blob = text[start:end + 1]
    try:
        return json.loads(blob), blob
    except json.JSONDecodeError:
        return None, blob


def extract_session(*streams):
    """Find 'session_id: <id>' line in any of the passed stdout/stderr
    streams. Hermes prints its session_id once per invocation; we
    persist it on the drafting trigger so a subsequent /improve call
    can resume the same conversation context."""
    m = SESSION_RE.search("\n".join(streams))
    return m.group(1) if m else None
