#!/usr/bin/env python3
"""Payment-link price gate + mint idempotency (F2-pay, 2026-06-10).

handle_payment_link (routes.py:962) is the single chokepoint all four mint
paths funnel through (n8n Generate Payment Link / Call Nomod / Call Nomod
(Reply), bridge /assist send_paylink). This locks:

  GATE (flag PAYLINK_PRICE_GATE_ENABLED, ships OFF; FAIL-CLOSED, no Redis):
    when ON, only source=="operator_typed" (a human explicitly typed the
    number) may mint; auto_draft / draft_preset / assist_llm_parsed /
    missing source are refused. AI-quoted payment_amount can never reach
    Nomod unconfirmed.
  CEILING (PAYLINK_MAX_AED, default 500000, ALWAYS ON): deterministic
    typo/insanity guard, not a business cap — catches "14900k"→14.9M and
    extra-zero typos. Largest legit link ever: AED 57,773.
  DEDUP (LIVE from deploy, FAIL-OPEN): same canonical cid + same amount
    within PAYLINK_DEDUP_TTL (default 1800s) returns the EXISTING link
    instead of minting a duplicate (Xeno 2026-06-07: 2x AED 14,900 in
    3m18s; 2026-05-22: 5x AED 9,000; 2026-05-24: 3x AED 8,300).
  AUDIT (LIVE, fail-open): every successful mint writes autonomous_sends
    kind='payment_link_minted' with source/draft_id — closes the
    auto-mint//assist invisibility gap.

Receive side untouched. Run: python3 hermes-bridge/test_paylink_gate.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import routes  # noqa: E402
import server  # noqa: E402

ENV_KEYS = ("PAYLINK_PRICE_GATE_ENABLED", "PAYLINK_MAX_AED",
            "PAYLINK_DEDUP_TTL")


class _Send:
    def __init__(self):
        self.status, self.body = None, None

    def __call__(self, status, body):
        self.status, self.body = status, body


class _FakeRedis:
    """Stateful GET/SET NX EX fake; records calls; optional hard failure."""

    def __init__(self, raises=False):
        self.store = {}
        self.calls = []
        self.raises = raises

    def __call__(self, args):
        self.calls.append(list(args))
        if self.raises:
            raise RuntimeError("redis down")
        cmd = args[0].upper()
        if cmd == "GET":
            return (self.store.get(args[1], ""), None)
        if cmd == "SET":
            key, val = args[1], args[2]
            if "NX" in args and key in self.store:
                return ("", None)
            self.store[key] = val
            return ("OK", None)
        return ("OK", None)


def _mint(payload, *, redis=None, psql_err=None, env=None,
          nomod=("https://pay.nomod/x", "LID-1", None)):
    """Drive handle_payment_link with everything faked. Returns
    (send, nomod_calls, psql_inserts, redis)."""
    redis = redis if redis is not None else _FakeRedis()
    nomod_calls, inserts = [], []

    def fake_nomod(amount, summary, cname):
        nomod_calls.append((amount, summary, cname))
        return nomod

    def fake_psql(sql, timeout=12):
        inserts.append(sql)
        return (None, psql_err) if psql_err else ("", None)

    for k in ENV_KEYS:
        os.environ.pop(k, None)
    for k, v in (env or {}).items():
        os.environ[k] = v

    olds = {"PAYMENTS_ENABLED": server.PAYMENTS_ENABLED,
            "nomod_create_link": server.nomod_create_link,
            "canonicalize_cid": server.canonicalize_cid}
    server.PAYMENTS_ENABLED = True
    server.nomod_create_link = fake_nomod
    server.canonicalize_cid = lambda c: (c or "").replace("@lid", "@c.us")
    oldr, oldp = routes._redis, routes._psql
    routes._redis, routes._psql = redis, fake_psql
    send = _Send()
    try:
        routes.handle_payment_link(payload, send)
    finally:
        for k, v in olds.items():
            setattr(server, k, v)
        routes._redis, routes._psql = oldr, oldp
        for k in ENV_KEYS:
            os.environ.pop(k, None)
    assert send.status == 200, send.status
    return send, nomod_calls, inserts, redis


BASE = {"customer_id": "55@c.us", "customer_name": "Luke",
        "payment_summary": "Bliss 55 · Jun 20 · 6 pax",
        "amount": 4200, "source": "operator_typed", "draft_id": "d1"}


# ------------------------------------------------------------------ anchors
def test_basic_mint_succeeds_flag_off():
    send, calls, _, _ = _mint(dict(BASE))
    assert send.body["ok"] is True and send.body["link_id"] == "LID-1"
    assert len(calls) == 1 and calls[0][0] == 4200.0


def test_invalid_amount_still_refused():
    send, calls, _, _ = _mint(dict(BASE, amount="nope"))
    assert send.body["ok"] is False and not calls


# ----------------------------------------------------------------- the gate
def test_flag_off_missing_source_mints():
    """Dormant gate = current behavior: unknown/missing source still mints."""
    p = dict(BASE); p.pop("source"); p.pop("draft_id")
    send, calls, _, _ = _mint(p)
    assert send.body["ok"] is True and len(calls) == 1


def test_flag_on_denies_auto_draft():
    send, calls, _, _ = _mint(dict(BASE, source="auto_draft"),
                              env={"PAYLINK_PRICE_GATE_ENABLED": "1"})
    assert send.body["ok"] is False, send.body
    assert "price gate" in send.body["error"]
    assert not calls, "Nomod was called despite the gate"


def test_flag_on_denies_draft_preset_assist_and_missing():
    for src in ("draft_preset", "assist_llm_parsed", None):
        p = dict(BASE)
        if src is None:
            p.pop("source")
        else:
            p["source"] = src
        send, calls, _, _ = _mint(p, env={"PAYLINK_PRICE_GATE_ENABLED": "1"})
        assert send.body["ok"] is False and not calls, (src, send.body)


def test_flag_on_allows_operator_typed():
    send, calls, _, _ = _mint(dict(BASE),
                              env={"PAYLINK_PRICE_GATE_ENABLED": "1"})
    assert send.body["ok"] is True and len(calls) == 1


def test_gate_fail_closed_when_redis_down():
    """The gate needs no Redis — it must still deny with Redis dead."""
    send, calls, _, _ = _mint(dict(BASE, source="auto_draft"),
                              redis=_FakeRedis(raises=True),
                              env={"PAYLINK_PRICE_GATE_ENABLED": "1"})
    assert send.body["ok"] is False and not calls


# --------------------------------------------------------------- the ceiling
def test_max_aed_default_500k_always_on():
    send, calls, _, _ = _mint(dict(BASE, amount=500001))
    assert send.body["ok"] is False and not calls
    assert "PAYLINK_MAX_AED" in send.body["error"]
    send, calls, _, _ = _mint(dict(BASE, amount=499999))
    assert send.body["ok"] is True and len(calls) == 1


def test_max_aed_env_override():
    send, calls, _, _ = _mint(dict(BASE, amount=1500),
                              env={"PAYLINK_MAX_AED": "1000"})
    assert send.body["ok"] is False and not calls


# ----------------------------------------------------------------- the dedup
def test_dedup_second_same_amount_returns_cached_link():
    redis = _FakeRedis()
    send1, calls1, _, _ = _mint(dict(BASE), redis=redis)
    assert send1.body["ok"] is True and len(calls1) == 1
    send2, calls2, _, _ = _mint(dict(BASE), redis=redis)
    assert send2.body["ok"] is True, send2.body
    assert send2.body.get("deduped") is True, send2.body
    assert send2.body["link_id"] == "LID-1"
    assert not calls2, "second mint hit Nomod despite dedup window"


def test_dedup_key_canonicalizes_cid():
    """@lid alias and its @c.us survivor share one dedup window."""
    redis = _FakeRedis()
    _mint(dict(BASE, customer_id="55@lid"), redis=redis)
    send2, calls2, _, _ = _mint(dict(BASE, customer_id="55@c.us"),
                                redis=redis)
    assert send2.body.get("deduped") is True and not calls2


def test_dedup_different_amount_still_mints():
    redis = _FakeRedis()
    _mint(dict(BASE), redis=redis)
    send2, calls2, _, _ = _mint(dict(BASE, amount=5000), redis=redis)
    assert send2.body["ok"] is True and len(calls2) == 1
    assert not send2.body.get("deduped")


def test_dedup_ttl_default_1800_and_env_override():
    redis = _FakeRedis()
    _mint(dict(BASE), redis=redis)
    sets = [c for c in redis.calls if c[0] == "SET"]
    assert sets and sets[0][-2:] == ["EX", "1800"], sets
    redis2 = _FakeRedis()
    _mint(dict(BASE), redis=redis2, env={"PAYLINK_DEDUP_TTL": "900"})
    sets2 = [c for c in redis2.calls if c[0] == "SET"]
    assert sets2 and sets2[0][-2:] == ["EX", "900"], sets2


def test_dedup_fail_open_when_redis_down():
    send, calls, _, _ = _mint(dict(BASE), redis=_FakeRedis(raises=True))
    assert send.body["ok"] is True and len(calls) == 1


# ----------------------------------------------------------------- the audit
def test_mint_writes_payment_link_minted_audit_row():
    send, _, inserts, _ = _mint(dict(BASE))
    rows = [s for s in inserts if "payment_link_minted" in s]
    assert rows, f"no mint audit INSERT: {inserts}"
    assert "operator_typed" in rows[0] and "d1" in rows[0] and "LID-1" in rows[0]


def test_audit_failure_does_not_block_mint():
    send, calls, _, _ = _mint(dict(BASE), psql_err="db down")
    assert send.body["ok"] is True and len(calls) == 1


def test_deduped_response_skips_duplicate_audit():
    redis = _FakeRedis()
    _, _, ins1, _ = _mint(dict(BASE), redis=redis)
    _, _, ins2, _ = _mint(dict(BASE), redis=redis)
    assert not [s for s in ins2 if "payment_link_minted" in s], \
        "dedup hit must not double-log the mint"


# --------------------------------------------------------------- /assist tag
def test_assist_payload_carries_gated_source():
    """Wiring lock: /assist send_paylink must declare assist_llm_parsed so
    the gate refuses it when ON (decision (d): /assist stays gated-off)."""
    import inspect
    src = inspect.getsource(routes)
    blk = src[src.index('if intent == "send_paylink"'):]
    blk = blk[:blk.index("handle_payment_link(pl_payload")]
    assert "assist_llm_parsed" in blk, \
        "/assist pl_payload does not declare source=assist_llm_parsed"


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
