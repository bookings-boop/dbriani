#!/usr/bin/env python3
"""Lever ② — /pending approval-throughput digest (Tier 0).

The leak (recon 2026-06-16): ~20-28 proactive nudge cards are drafted/day and
silently EXPIRE at the 24h Redis TTL if the operator never taps ✅ Send — a
drafted->never-delivered loss that was invisible because the funnel was never
instrumented. Tier 0 surfaces the LIVE pending pile on demand (/pending) so the
operator clears it before expiry, reusing the EXISTING per-card send:/edit:/skip:
callbacks (every send still routes through n8n -> /draft-freshness -> claim-send).

This module is PURE (selection + render, no Redis/PG) so it unit-tests with no
live infra; the endpoint (routes.handle_pending) does the Redis reads and calls
in here. Read-time GHOST SKIP only — never SREM (keeps /pending read-only).

Run: python3 hermes-bridge/test_pending_digest.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pending  # noqa: E402
import routes  # noqa: E402  — exercises the /pending I/O handler


def _nudge(did, status="pending", proactive=True, name="Lead"):
    return {"id": did, "status": status, "is_proactive_nudge": proactive,
            "customer_phone": did.split("_")[0] + "@lid",
            "customer_name": name,
            "draft_text": "Hi " + name + ", still keen on the charter?"}


def test_select_keeps_only_pending_proactive_nudges_oldest_first():
    # draft_id prefix is the creation epoch-ms (server._draft_save format
    # "<ms>_<rand>"), so oldest = smallest prefix = soonest to hit the 24h TTL.
    items = [
        _nudge("3000_aaaaa"),                      # newest pending nudge
        None,                                       # ghost (TTL-expired body)
        _nudge("1000_bbbbb"),                       # oldest pending nudge
        _nudge("2000_ccccc", status="sent"),        # terminal -> drop
        _nudge("2500_ddddd", proactive=False),      # reply draft -> drop
        {"junk": True},                             # malformed -> drop
    ]
    out = pending.select_pending_nudges(items, now_ms=10_000)
    ids = [d["id"] for d in out]
    assert ids == ["1000_bbbbb", "3000_aaaaa"], (
        "keep only pending proactive nudges, oldest-first; got " + repr(ids))


def _btn_callbacks(card):
    return [b.get("callback_data", "") for b in card.get("buttons", [])]


def test_render_empty_pile_is_clean():
    r = pending.render_pending([], stale_ids=set(), now_ms=10_000)
    assert r["count"] == 0
    assert r["fresh_ids"] == [] and r["stale_ids"] == []
    assert r["per_card"] == []
    assert "no proactive nudges" in r["telegram_text"].lower(), r["telegram_text"]


def test_render_fresh_card_has_send_button():
    sel = pending.select_pending_nudges([_nudge("1000_aaaaa", name="Mara")], 10_000)
    r = pending.render_pending(sel, stale_ids=set(), now_ms=2_000_000)
    assert r["count"] == 1
    assert r["fresh_ids"] == ["1000_aaaaa"]
    assert r["stale_ids"] == []
    cbs = _btn_callbacks(r["per_card"][0])
    assert "send:1000_aaaaa" in cbs, cbs
    assert "edit:1000_aaaaa" in cbs and "skip:1000_aaaaa" in cbs, cbs
    assert "Mara" in r["per_card"][0]["text"]


def test_render_stale_card_has_no_send_button_and_is_flagged():
    # a lead who REPLIED since the draft was made must not be one-tap sendable
    sel = pending.select_pending_nudges([_nudge("1000_aaaaa")], 10_000)
    r = pending.render_pending(sel, stale_ids={"1000_aaaaa"}, now_ms=2_000_000)
    assert r["stale_ids"] == ["1000_aaaaa"]
    assert r["fresh_ids"] == []
    cbs = _btn_callbacks(r["per_card"][0])
    assert not any(c.startswith("send:") for c in cbs), (
        "stale card must NOT offer one-tap Send; got " + repr(cbs))
    assert "edit:1000_aaaaa" in cbs and "skip:1000_aaaaa" in cbs, cbs
    assert "⚠" in r["per_card"][0]["text"]


def test_render_partitions_fresh_and_stale_and_counts():
    sel = pending.select_pending_nudges(
        [_nudge("1000_aaaaa"), _nudge("2000_bbbbb"), _nudge("3000_ccccc")], 10_000)
    r = pending.render_pending(sel, stale_ids={"2000_bbbbb"}, now_ms=2_000_000)
    assert r["count"] == 3
    assert r["fresh_ids"] == ["1000_aaaaa", "3000_ccccc"], r["fresh_ids"]
    assert r["stale_ids"] == ["2000_bbbbb"]
    assert "3" in r["telegram_text"]  # header surfaces the total


class _RedisStub:
    """Canned SMEMBERS + GET; records calls so we can assert no Redis touch."""

    def __init__(self, members, bodies):
        self.members = members
        self.bodies = bodies
        self.calls = []

    def __call__(self, args, *a, **k):
        self.calls.append(list(args))
        cmd = str(args[0]).upper()
        if cmd == "SMEMBERS":
            return ("\n".join(self.members), None)
        if cmd == "GET":
            did = str(args[1]).split("draft:", 1)[-1]
            return (self.bodies.get(did, ""), None)
        return ("", None)


def _nudge_json(did, **kw):
    return json.dumps(_nudge(did, **kw))


def test_handle_pending_flag_off_is_dormant():
    os.environ.pop("PENDING_DIGEST_ENABLED", None)
    stub = _RedisStub(["1000_a"], {})
    orig = routes._redis
    routes._redis = stub
    try:
        out = {}
        routes.handle_pending({}, lambda c, b: out.update(code=c, body=b))
    finally:
        routes._redis = orig
    assert out["body"]["enabled"] is False, out["body"]
    assert out["body"]["count"] == 0
    assert stub.calls == [], "flag-off must not touch Redis; got " + repr(stub.calls)


def test_handle_pending_skips_ghosts_and_renders():
    os.environ["PENDING_DIGEST_ENABLED"] = "1"
    stub = _RedisStub(
        ["3000_c", "2000_b", "1000_a"],
        {"1000_a": _nudge_json("1000_a", name="Alpha"),
         "2000_b": "",                                  # ghost (expired body)
         "3000_c": _nudge_json("3000_c", name="Gamma")})
    orig_r, orig_s = routes._redis, routes._pending_stale_ids
    routes._redis = stub
    routes._pending_stale_ids = lambda selected: set()
    try:
        out = {}
        routes.handle_pending({}, lambda c, b: out.update(code=c, body=b))
    finally:
        routes._redis, routes._pending_stale_ids = orig_r, orig_s
        os.environ.pop("PENDING_DIGEST_ENABLED", None)
    b = out["body"]
    assert b["enabled"] is True
    assert b["count"] == 2, b
    assert b["ghosts_skipped"] == 1, b
    assert b["fresh_ids"] == ["1000_a", "3000_c"], b["fresh_ids"]
    assert b["telegram_text"] and b["inline_keyboards"]


def test_handle_pending_marks_stale_no_send():
    os.environ["PENDING_DIGEST_ENABLED"] = "1"
    stub = _RedisStub(["1000_a"], {"1000_a": _nudge_json("1000_a")})
    orig_r, orig_s = routes._redis, routes._pending_stale_ids
    routes._redis = stub
    routes._pending_stale_ids = lambda selected: {"1000_a"}
    try:
        out = {}
        routes.handle_pending({}, lambda c, b: out.update(code=c, body=b))
    finally:
        routes._redis, routes._pending_stale_ids = orig_r, orig_s
        os.environ.pop("PENDING_DIGEST_ENABLED", None)
    b = out["body"]
    assert b["stale_ids"] == ["1000_a"], b
    assert b["fresh_ids"] == []
    cbs = [bt.get("callback_data", "") for bt in b["per_card"][0]["buttons"]]
    assert not any(c.startswith("send:") for c in cbs), cbs


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} pending-digest tests passed")
