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
import threading
import time

from util import log  # noqa: F401 — re-exported


# --- paths + timeouts ----------------------------------------------
HOME = os.path.expanduser("~")
HERMES = os.path.join(HOME, ".local", "bin", "hermes")
HERMES_TIMEOUT = int(os.environ.get("BRIDGE_HERMES_TIMEOUT", "120"))

# Concurrency cap on Hermes CLI subprocesses. Production incident
# 2026-05-28: operator tapped [Draft nudge] on ~7 leads at once →
# 7 concurrent `hermes chat` subprocesses (each cold-starts + calls
# Claude) → load 2.8 on a 2-vCPU box, memory near-full → executions
# crashed, "nothing happening". Each Hermes call is ~30-60s; running
# them all at once thrashes the box. Cap concurrency so the box stays
# healthy; excess calls queue for a slot (bounded by the caller's
# own n8n timeout). 3 is a safe default for a 2-vCPU / 4GB box.
HERMES_MAX_CONCURRENCY = int(
    os.environ.get("BRIDGE_HERMES_CONCURRENCY", "3"))
_HERMES_SEM = threading.BoundedSemaphore(HERMES_MAX_CONCURRENCY)

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
    env = dict(os.environ)
    env["PATH"] = os.path.dirname(HERMES) + os.pathsep + env.get("PATH", "")
    # Acquire a concurrency slot. If the box is saturated this blocks
    # until a slot frees (or the caller's HTTP timeout fires upstream).
    waited0 = time.time()
    _HERMES_SEM.acquire()
    queue_ms = int((time.time() - waited0) * 1000)
    if queue_ms > 500:
        log(f"hermes call: WAITED {queue_ms}ms for a concurrency slot "
            f"(cap={HERMES_MAX_CONCURRENCY})")
    log("hermes call:", " ".join(c for c in cmd if c != query))
    t0 = time.time()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, env=env)
    finally:
        _HERMES_SEM.release()
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
