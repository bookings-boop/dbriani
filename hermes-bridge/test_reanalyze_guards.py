#!/usr/bin/env python3
"""F2-trigger — durable re-analysis trigger + load guards (2026-06-10).

Three behaviors locked (design: dubriani_f4_f2seed_designs_2026_06_10 + the
06-10 read-only RCA):

  (a) EVERY fresh inbound enqueues a re-analysis — including bare replies
      ("ok", "?") that fail _facts_extract_gate. The Devanshu case: a reply
      after the verdict never re-triggered analysis because the enqueue was
      guarded by `if do_extract:`.
  (b) Guard A — the drain path canonicalizes merged identities and keeps only
      active-pipeline labels. The queue is fed by RAW inbound cids with NO
      label filter, and the importance UPDATE downstream is unconditional —
      without this, a LOST/SCAM/DISREGARDED or merged-away row gets its
      score/reasoning clobbered. FAIL-CLOSED on DB error (hourly sweep is the
      backstop).
  (c) Guard C — the per-cid dedup marker becomes a real cooldown:
      REANALYZE_DEDUP_TTL (default 1800 s) and the drain NO LONGER deletes
      the marker, so a chatty thread can't burn an Anthropic call every
      5-min cron tick (the 2026-06-07 saturation surface).

Run: python3 hermes-bridge/test_reanalyze_guards.py  (or pytest)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import routes  # noqa: E402
import server  # noqa: E402


class _Send:
    def __init__(self):
        self.status, self.body = None, None

    def __call__(self, status, body):
        self.status, self.body = status, body


class _RedisSpy:
    """Capture _redis command lists; scripted replies by command name."""

    def __init__(self, replies=None):
        self.calls = []
        self.replies = replies or {}

    def __call__(self, args):
        self.calls.append(list(args))
        return self.replies.get(args[0], ("OK", None))


def _patch(mod, **attrs):
    """Set attrs on mod, return dict of originals."""
    old = {}
    for k, v in attrs.items():
        old[k] = getattr(mod, k)
        setattr(mod, k, v)
    return old


def _restore(mod, old):
    for k, v in old.items():
        setattr(mod, k, v)


# --------------------------------------------------------------- (a) enqueue
def _run_customer_facts(msg, spy):
    """Drive handle_customer_facts with a cached (non-None) lead and `msg`,
    with every heavy dep stubbed and routes._enqueue_reanalyze spied."""
    cached = {"name": "X", "message_count": 3, "yachts": "", "dates": "",
              "party_size": ""}
    olds = _patch(
        server,
        get_customer_facts=lambda cid: dict(cached),
        _merge_facts=lambda c, e: dict(c or {}),
        extract_customer_facts=lambda m, h: {},
        upsert_customer_facts=lambda cid, name, merged: (4, None),
        build_customer_header=lambda f: "HDR",
        behavioral_context=lambda c: {},
        waha_lookup_push_name=lambda cid: "",
    )
    oldr = _patch(routes, _enqueue_reanalyze=spy)
    send = _Send()
    try:
        routes.handle_customer_facts(
            {"customer_id": "55@c.us", "incoming_message": msg}, send)
    finally:
        _restore(server, olds)
        _restore(routes, oldr)
    assert send.status == 200, f"handler errored: {send.status} {send.body}"
    return send


def test_bare_reply_inbound_enqueues_reanalyze():
    """msg='ok' fails _facts_extract_gate (real gate) — must STILL enqueue."""
    assert server._facts_extract_gate("ok") is False, \
        "premise broken: 'ok' should not be an extracting message"
    calls = []
    _run_customer_facts("ok", lambda cid: calls.append(cid))
    assert calls == ["55@c.us"], f"bare reply did not enqueue: {calls}"


def test_facts_inbound_still_enqueues_reanalyze():
    """Anchor: an extracting inbound (digits) keeps enqueueing as before."""
    calls = []
    _run_customer_facts("tomorrow for 4 people", lambda cid: calls.append(cid))
    assert calls == ["55@c.us"]


# ----------------------------------------------------------- (c) cooldown TTL
def test_enqueue_cooldown_ttl_default_1800():
    spy = _RedisSpy(replies={"SET": ("OK", None)})
    old = _patch(routes, _redis=spy)
    os.environ.pop("REANALYZE_DEDUP_TTL", None)
    try:
        routes._enqueue_reanalyze("77@c.us")
    finally:
        _restore(routes, old)
    sets = [c for c in spy.calls if c[0] == "SET"]
    assert sets and sets[0][-2:] == ["EX", "1800"], \
        f"expected 1800s cooldown, got {sets}"


def test_enqueue_cooldown_ttl_env_override():
    spy = _RedisSpy(replies={"SET": ("OK", None)})
    old = _patch(routes, _redis=spy)
    os.environ["REANALYZE_DEDUP_TTL"] = "2400"
    try:
        routes._enqueue_reanalyze("77@c.us")
    finally:
        _restore(routes, old)
        os.environ.pop("REANALYZE_DEDUP_TTL", None)
    sets = [c for c in spy.calls if c[0] == "SET"]
    assert sets and sets[0][-2:] == ["EX", "2400"], \
        f"env override ignored: {sets}"


def test_drain_leaves_cooldown_markers():
    """The drain must NOT delete the per-cid markers — they double as the
    post-analysis cooldown (otherwise a chatty thread re-analyzes every
    cron tick regardless of TTL)."""
    spy = _RedisSpy(replies={"LPOP": ("a@c.us\nb@lid", None)})
    old = _patch(routes, _redis=spy)
    try:
        cids = routes._drain_reanalyze_queue(12)
    finally:
        _restore(routes, old)
    assert cids == ["a@c.us", "b@lid"]
    dels = [c for c in spy.calls if c[0] == "DEL"]
    assert not dels, f"drain still deletes cooldown markers: {dels}"


def test_drain_still_dedupes_preserving_order():
    spy = _RedisSpy(replies={"LPOP": ("a@c.us\nb@lid\na@c.us\n", None)})
    old = _patch(routes, _redis=spy)
    try:
        cids = routes._drain_reanalyze_queue(12)
    finally:
        _restore(routes, old)
    assert cids == ["a@c.us", "b@lid"]


# ------------------------------------------------------------ (b) drain guard
def test_filter_keeps_active_drops_terminal_and_merged():
    """Only cids the DB confirms as active+canonical survive."""

    def fake_psql(sql, timeout=12):
        assert "merged_into IS NULL" in sql and "label IN (" in sql
        return ("act@c.us\n", None)

    olds = _patch(server, canonicalize_cid=lambda c: c)
    oldr = _patch(routes, _psql=fake_psql)
    try:
        kept = routes._filter_reanalyze_cids(["act@c.us", "lost@c.us"])
    finally:
        _restore(server, olds)
        _restore(routes, oldr)
    assert kept == ["act@c.us"], f"terminal cid not dropped: {kept}"


def test_filter_canonicalizes_merged_cids_and_dedupes():
    """A raw @lid dup canonicalizes to its survivor; survivor queried once."""
    queried = []

    def fake_psql(sql, timeout=12):
        queried.append(sql)
        return ("canon@c.us\n", None)

    olds = _patch(server, canonicalize_cid=lambda c: "canon@c.us"
                  if c == "dup@lid" else c)
    oldr = _patch(routes, _psql=fake_psql)
    try:
        kept = routes._filter_reanalyze_cids(["dup@lid", "canon@c.us"])
    finally:
        _restore(server, olds)
        _restore(routes, oldr)
    assert kept == ["canon@c.us"], kept
    assert len(queried) == 1 and queried[0].count("canon@c.us") == 1, \
        "canonical cid should be queried exactly once"


def test_filter_fail_closed_on_db_error():
    olds = _patch(server, canonicalize_cid=lambda c: c)
    oldr = _patch(routes, _psql=lambda sql, timeout=12: (None, "boom"))
    try:
        kept = routes._filter_reanalyze_cids(["a@c.us"])
    finally:
        _restore(server, olds)
        _restore(routes, oldr)
    assert kept == [], "filter must FAIL-CLOSED on DB error"


def test_reanalyze_branch_wires_filter_and_skips_when_empty():
    """handle_pipeline_analyze {source:reanalyze}: drained cids must pass
    through _filter_reanalyze_cids; all-filtered → skip response, no
    analysis. (Lock SET/DEL handled by the redis spy.)"""
    spy = _RedisSpy(replies={"SET": ("OK", None), "DEL": ("1", None)})
    seen = []

    def fake_filter(cids):
        seen.append(list(cids))
        return []

    oldr = _patch(routes, _redis=spy,
                  _drain_reanalyze_queue=lambda cap: ["x@c.us", "y@lid"],
                  _filter_reanalyze_cids=fake_filter)
    send = _Send()
    try:
        routes.handle_pipeline_analyze({"source": "reanalyze"}, send)
    finally:
        _restore(routes, oldr)
    assert seen == [["x@c.us", "y@lid"]], \
        f"filter not wired into the drain path: {seen}"
    assert send.status == 200 and send.body.get("skipped") is True, send.body
    assert send.body.get("skipped_reason") == "reanalyze_no_active_leads", \
        send.body


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn(); print("PASS", fn.__name__)
        except AssertionError as e:
            failed += 1; print("FAIL", fn.__name__, "-", e or "assert")
        except Exception as e:  # noqa: BLE001
            failed += 1; print("ERROR", fn.__name__, "-", repr(e))
    print(f"{len(fns)-failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
