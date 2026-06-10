#!/usr/bin/env python3
"""Lever 1a — cache_control on the bridge's direct Anthropic calls.

Locks the wire-shape that makes Anthropic prompt-caching actually fire, so the
~19K-token system prompt is cached (read at 0.1x) instead of re-billed at full
input price on every _anthropic_draft / _anthropic_score call:

  1. the STATIC system prompt sits in a `cache_control: ephemeral` system block
  2. the VARIABLE content (history, the draft being scored) sits OUTSIDE that
     cached prefix (in the user turn) — else every call is a cache MISS
  3. the cached prefix is BYTE-IDENTICAL across calls (it IS the cache key)
  4. every call logs API-reported cache read/write usage so future cost is REAL

These tests assert the JSON sent to api.anthropic.com (the real public contract
that determines caching) — never a real network call is made.

Run:  python3 hermes-bridge/test_anthropic_cache.py   (or pytest)
"""
import io
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-real")

import server   # noqa: E402
import routes   # noqa: E402

# Box-only dependencies (docker `psql`, the on-box prompt file) are stubbed so
# the SPLIT/cache-placement logic is what's under test — not the environment.
# On the box these return the real ~58KB prompt, behavior rules, and lead SQL.
server.load_system_prompt = lambda: "STATIC-SYSTEM-PROMPT " * 1200  # ~24K chars
server.fetch_behavior_rules = lambda *a, **k: []
server._psql = lambda *a, **k: ("", None)


class _Resp:
    """Context-manager fake of an Anthropic HTTP response that json.load() reads."""
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def __enter__(self):
        return io.BytesIO(self._b)

    def __exit__(self, *a):
        return False


_FAKE_CONTENT = ('{"messages":["hi there"],"notes_for_zayn":"",'
                 '"score":7,"flags":[],"summary":"ok"}')


def _patched(fn, usage=None):
    """Run fn() with urllib.request.urlopen swapped to capture request bodies.
    Returns the list of parsed request dicts. Restores urlopen in finally."""
    captured = []
    use = usage or {"input_tokens": 5, "output_tokens": 10,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0}

    def fake_urlopen(req, *a, **k):
        captured.append(json.loads(req.data.decode()))
        return _Resp({"content": [{"text": _FAKE_CONTENT}], "usage": use})

    orig = urllib.request.urlopen
    urllib.request.urlopen = fake_urlopen
    try:
        fn()
    finally:
        urllib.request.urlopen = orig
    return captured


# --- build_quality_query_parts: the byte-identical-prefix invariant -----------

def test_quality_parts_recombine_byte_identical():
    p = {"customer_name": "Aisha", "history": "older chat here",
         "incoming_message": "how much for the 55ft saturday?",
         "current_draft": "It's AED 1400/hr, 4hr minimum.",
         "customer_id": "971500000001@c.us"}
    prefix, body = server.build_quality_query_parts(p)
    assert prefix and body
    assert prefix + "\n" + body == server.build_quality_query(p), \
        "split must recombine to the EXACT original query (back-compat + cache key)"


def test_quality_prefix_static_body_variable_no_duplication():
    p = {"customer_name": "Aisha", "incoming_message": "how much?",
         "current_draft": "AED 1400/hr"}
    prefix, body = server.build_quality_query_parts(p)
    assert len(prefix) > 1000, "prefix must carry the ~19K static system prompt"
    assert "DRAFT TO SCORE" not in prefix, "the draft-to-score block is variable"
    assert "AED 1400/hr" in body, "the draft being scored belongs in the body"
    assert "AED 1400/hr" not in prefix, "variable draft must NOT be in cached prefix"


def test_quality_prefix_byte_identical_across_different_drafts():
    a, _ = server.build_quality_query_parts(
        {"current_draft": "draft A", "incoming_message": "msg 1"})
    b, _ = server.build_quality_query_parts(
        {"current_draft": "draft B", "incoming_message": "msg 2"})
    assert a == b, "prefix must be identical regardless of the draft/message"


# --- _anthropic_draft: cache_control on system, variable in user turn ----------

def test_draft_caches_system_block_variable_outside():
    cap = _patched(lambda: routes._anthropic_draft(
        "STATIC-DRAFTER-PROMPT", "history here", "Aisha", "971500000001",
        "the newest customer message"))
    req = cap[0]
    sysblk = req["system"][0]
    assert sysblk["cache_control"] == {"type": "ephemeral"}, \
        "the static drafter prompt MUST be cache_control'd"
    assert sysblk["text"] == "STATIC-DRAFTER-PROMPT"
    assert "the newest customer message" in req["messages"][0]["content"], \
        "the variable customer message belongs in the user turn"
    assert "the newest customer message" not in json.dumps(req["system"]), \
        "variable content must sit OUTSIDE the cached prefix"


def test_draft_prefix_byte_identical_across_calls():
    cap = _patched(lambda: (
        routes._anthropic_draft("SAME-PROMPT", "h1", "A", "1", "msg one"),
        routes._anthropic_draft("SAME-PROMPT", "h2", "B", "2", "msg two")))
    assert cap[0]["system"] == cap[1]["system"], \
        "the cached system block must be byte-identical across calls"


# --- _anthropic_score: prefix cached in system, body in user (not duplicated) --

def test_score_with_prefix_caches_and_excludes_prefix_from_body():
    cap = _patched(lambda: routes._anthropic_score(
        "VARIABLE-BODY draft=foo", system_prefix="STATIC-SCORER-PREFIX"))
    req = cap[0]
    cached = [b for b in req["system"] if b.get("cache_control")]
    assert cached and cached[0]["text"] == "STATIC-SCORER-PREFIX", \
        "the static scorer prefix MUST be in a cache_control'd system block"
    user = req["messages"][0]["content"]
    assert "VARIABLE-BODY" in user, "the variable body belongs in the user turn"
    assert "STATIC-SCORER-PREFIX" not in user, \
        "the prefix must NOT be duplicated into the user turn (double-bill)"


def test_score_without_prefix_is_backward_compatible():
    cap = _patched(lambda: routes._anthropic_score("everything-in-one-string"))
    req = cap[0]
    assert req["messages"][0]["content"] == "everything-in-one-string"
    assert not any(b.get("cache_control") for b in req["system"]), \
        "legacy single-arg call keeps current shape (no large cached block)"


# --- usage logging: REAL cache read/write numbers to journald -----------------

def test_draft_logs_real_cache_usage():
    lines = []
    rorig = routes.log
    routes.log = lambda *a: lines.append(" ".join(str(x) for x in a))
    try:
        _patched(lambda: routes._anthropic_draft("P", "h", "n", "p", "m"),
                 usage={"input_tokens": 11, "output_tokens": 22,
                        "cache_creation_input_tokens": 33,
                        "cache_read_input_tokens": 44})
    finally:
        routes.log = rorig
    joined = "\n".join(lines)
    assert "cache" in joined, "must log a cache usage line"
    assert "33" in joined and "44" in joined, \
        "must log REAL cache write (33) and read (44) token counts"


def test_score_logs_real_cache_usage():
    lines = []
    rorig = routes.log
    routes.log = lambda *a: lines.append(" ".join(str(x) for x in a))
    try:
        _patched(lambda: routes._anthropic_score("q", system_prefix="P"),
                 usage={"input_tokens": 1, "output_tokens": 2,
                        "cache_creation_input_tokens": 7,
                        "cache_read_input_tokens": 9})
    finally:
        routes.log = rorig
    joined = "\n".join(lines)
    assert "7" in joined and "9" in joined, "score call must log REAL cache usage"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL {t.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"ERROR {t.__name__}: {e!r}")
            failed += 1
    print(f"\n=== {passed} passed, {failed} failed ===")
    sys.exit(1 if failed else 0)
