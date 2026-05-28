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

# Two-lane concurrency control — fixes a PRIORITY INVERSION (incident
# 2026-05-28: the operator's interactive edit/refine/nudge waited
# 10-17s behind bursts of background lead-analysis / customer-facts
# Hermes calls because every call shared ONE pool). _HERMES_TOTAL is the
# global cap (max model subprocesses the box can run). _HERMES_BG caps
# BACKGROUND work one slot BELOW total, so an INTERACTIVE call (operator
# is actively waiting) always finds a free slot instead of queuing
# behind the background backlog.
#
# Lock order is always BG → TOTAL (background path only); interactive
# takes TOTAL alone. No circular wait → no deadlock. Background can hold
# at most (cap-1) slots, guaranteeing ≥1 reachable by interactive.
_HERMES_TOTAL = threading.BoundedSemaphore(HERMES_MAX_CONCURRENCY)
# Background cap — kept TWO below total so the hourly proactive sweep
# (which fires ~9 lead-analysis calls at once) can never occupy more
# than 1 slot, leaving ≥2 free for the operator's interactive work
# (drafts, nudges, edits). Production 2026-05-28: a sweep coinciding
# with a live session pushed interactive calls into 40s+ queue waits
# and made buttons unresponsive. Tunable via env.
HERMES_BG_CONCURRENCY = int(os.environ.get(
    "BRIDGE_HERMES_BG_CONCURRENCY",
    str(max(1, HERMES_MAX_CONCURRENCY - 2))))
_HERMES_BG = threading.BoundedSemaphore(HERMES_BG_CONCURRENCY)

# Output parsing regexes (compiled once at import).
SESSION_RE = re.compile(r"session_id:\s*(\S+)")
FENCE_RE = re.compile(r"```(?:json)?", re.IGNORECASE)


def run_hermes(query, timeout=None, priority="interactive"):
    """Invoke `hermes chat -q <query>` in YOLO mode, return
    (returncode, stdout, stderr, elapsed_ms). The bridge always uses
    --source tool (so Hermes knows it's being called programmatically)
    and -Q (no-prompt-on-tool-confirm). PATH is prepended with the
    hermes bin dir so any sub-tools Hermes spawns find their siblings.

    priority: 'interactive' (default — operator is actively waiting:
    refine / nudge / assist / draft; gets a reserved slot) or
    'background' (proactive lead-analysis, customer-facts extraction,
    auto-improve, learn — capped one slot below total so it can never
    starve interactive work).

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
    # Acquire a concurrency slot. Background work first passes the BG
    # gate (cap-1) so it leaves a slot for interactive; both lanes then
    # take a global slot. Lock order BG→TOTAL avoids deadlock.
    background = (priority == "background")
    waited0 = time.time()
    if background:
        _HERMES_BG.acquire()
    _HERMES_TOTAL.acquire()
    queue_ms = int((time.time() - waited0) * 1000)
    if queue_ms > 500:
        log(f"hermes call: WAITED {queue_ms}ms for a concurrency slot "
            f"(cap={HERMES_MAX_CONCURRENCY}, prio={priority})")
    log("hermes call:", " ".join(c for c in cmd if c != query))
    t0 = time.time()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, env=env)
    finally:
        _HERMES_TOTAL.release()
        if background:
            _HERMES_BG.release()
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
