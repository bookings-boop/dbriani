#!/usr/bin/env python3
"""AREA C — duplicate follow-up DEDUP + cap-burn + situation summary.

Milena (HOT) got TWO follow-up cards because TWO reengage engines fired at the
top of the hour and both drafted her before either wrote the 48h cooldown
marker (a cross-engine read-read TOCTOU). This locks the bridge-side fixes:

  C1.a  handle_hourly_sweep self-disables its legacy follow-up emission unless
        HOURLY_SWEEP_FOLLOWUPS=1 — so the n8n legacy branch (Engine B) iterates
        an empty array and posts nothing; /hourly-sweep still does its real job
        (label re-analysis / cold-decay).
  C1.b  _claim_reengage — an atomic per-candidate Redis NX claim acquired BEFORE
        drafting so no two callers (engines or concurrent runs) can double-post.
  C1.c  _should_bump_nudge — honor no_state_bump so handle_followup_sweep's
        deferred-bump-after-post is the SINGLE bump (Milena double-bumped to 3 >
        cap 2).
  C1.d  _followup_situation_summary / _last_history_bubble — a pure, no-LLM 1-2
        line operator context block derived from the already-fetched history +
        label row (NEVER a price; no price column exists).

Pure pieces are unit-tested directly; handle_hourly_sweep is run DB-free with
_psql / scan_followup_eligibility monkeypatched.

Run: python3 hermes-bridge/test_followup_dedup.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import routes  # noqa: E402
import server  # noqa: E402


class _Send:
    def __init__(self):
        self.status = None
        self.body = None

    def __call__(self, status, body):
        self.status = status
        self.body = body


# ── C1.c  no_state_bump guard (pure) ─────────────────────────────────────

def test_should_bump_when_flag_absent():
    assert routes._should_bump_nudge({}) is True
    assert routes._should_bump_nudge({"customer_id": "x"}) is True


def test_should_not_bump_when_no_state_bump_true():
    # handle_followup_sweep passes this — it commits the bump itself AFTER a
    # confirmed post, so handle_draft_followup must NOT also bump.
    assert routes._should_bump_nudge({"no_state_bump": True}) is False


def test_should_bump_when_no_state_bump_falsey():
    assert routes._should_bump_nudge({"no_state_bump": False}) is True
    assert routes._should_bump_nudge({"no_state_bump": 0}) is True


def test_should_bump_tolerates_non_dict():
    assert routes._should_bump_nudge(None) is True
    assert routes._should_bump_nudge("garbage") is True


def test_draft_followup_guards_the_bump():
    # Structural lock: the unconditional bump is now behind _should_bump_nudge
    # so the cap can't double-increment.
    assert "_should_bump_nudge" in routes.handle_draft_followup.__code__.co_names


# ── C1.b  atomic per-candidate claim ─────────────────────────────────────

class _PatchRedis:
    """Capture/control routes._redis for the duration of a block."""

    def __init__(self, ret=("OK", None), raises=False):
        self.calls = []
        self._ret = ret
        self._raises = raises

    def _fn(self, args):
        self.calls.append(list(args))
        if self._raises:
            raise RuntimeError("redis down")
        return self._ret

    def __enter__(self):
        self._orig = routes._redis
        routes._redis = self._fn
        return self

    def __exit__(self, *exc):
        routes._redis = self._orig
        return False


def test_claim_acquired_sets_nx_ex():
    with _PatchRedis(ret=("OK", None)) as p:
        assert routes._claim_reengage("25941719961655@lid", 600) is True
    # exactly one SET ... NX EX <ttl>
    assert len(p.calls) == 1, p.calls
    args = p.calls[0]
    assert args[0] == "SET"
    assert args[1] == "reengage:claim:25941719961655@lid"
    assert "NX" in args and "EX" in args
    assert args[args.index("EX") + 1] == "600"


def test_claim_lost_when_key_exists():
    # Redis SET NX returns nothing (not OK) when the key already exists — the
    # other engine/run already claimed this lead this cycle.
    with _PatchRedis(ret=("", None)):
        assert routes._claim_reengage("x@c.us", 600) is False
    with _PatchRedis(ret=(None, None)):
        assert routes._claim_reengage("x@c.us", 600) is False


def test_claim_fail_open_on_redis_error():
    # Engine B is already disabled, so the claim is defense-in-depth; a Redis
    # outage must NOT silently kill reengage coverage. Fail OPEN (proceed) —
    # the 48h SQL cooldown is still the backstop.
    with _PatchRedis(ret=(None, "ERR connection refused")):
        assert routes._claim_reengage("x@c.us", 600) is True
    with _PatchRedis(raises=True):
        assert routes._claim_reengage("x@c.us", 600) is True


def test_claim_empty_cid_is_false():
    with _PatchRedis(ret=("OK", None)) as p:
        assert routes._claim_reengage("", 600) is False
    assert p.calls == []


def test_followup_sweep_uses_claim():
    # Structural lock: the sweep loop acquires the claim before drafting.
    assert "_claim_reengage" in routes.handle_followup_sweep.__code__.co_names


# ── C1.d  situation summary (pure) ───────────────────────────────────────

_HIST = (
    'Customer (3d ago): "is the Bliss 55 free next saturday?"\n'
    'Dubriani (3d ago): "yes! 4-7pm is open, AED 3500 for 3 hours"\n'
    'Customer (2d ago): "great, can you hold it for me?"\n'
)


def test_last_history_bubble_parses_customer_and_dubriani():
    body, ago = routes._last_history_bubble(_HIST, "Customer")
    assert body == "great, can you hold it for me?", (body, ago)
    assert ago == "2d ago", ago
    body, ago = routes._last_history_bubble(_HIST, "Dubriani")
    assert body == "yes! 4-7pm is open, AED 3500 for 3 hours", body
    assert ago == "3d ago", ago


def test_last_history_bubble_none_when_absent():
    assert routes._last_history_bubble("", "Customer") == (None, None)
    assert routes._last_history_bubble(
        "First contact, no prior messages.", "Customer") == (None, None)


def test_last_history_bubble_never_raises_on_garbage():
    assert routes._last_history_bubble("Customer no parens here", "Customer")
    assert routes._last_history_bubble(None, "Dubriani") == (None, None)


def test_situation_summary_status_and_context():
    row = {"label": "HOT", "yachts": "Bliss 55", "dates": "Sat 14 Jun"}
    out = routes._followup_situation_summary(_HIST, row, "8", silence_hours=43.0)
    assert "🧷" in out
    assert "HOT" in out and "Bliss 55" in out and "Sat 14 Jun" in out
    assert "8 pax" in out
    # last customer message surfaced (the operator must be updated)
    assert "great, can you hold it for me?" in out
    # last operator/dubriani message surfaced
    assert "4-7pm is open" in out
    assert "they:" in out and "we:" in out
    # multi-line (status line + context line)
    assert "\n" in out


def test_situation_summary_truncates_long_body():
    long_q = "x" * 200
    hist = f'Customer (1h ago): "{long_q}"\n'
    out = routes._followup_situation_summary(hist, {"label": "WARM"})
    assert "…" in out
    assert ("x" * 200) not in out


def test_situation_summary_never_promises_price_field():
    # No price column exists; the summary must only echo what is in history /
    # row, never fabricate a price line. (Defensive: row has no price key.)
    row = {"label": "HOT", "yachts": "Zenith 64"}
    out = routes._followup_situation_summary("First contact.", row,
                                             silence_hours=50.0)
    # falls back to silence note when no history bubbles
    assert "Zenith 64" in out
    assert "no prior messages" in out


def test_situation_summary_none_safe():
    assert routes._followup_situation_summary("", None) == ""
    assert routes._followup_situation_summary(None, None, None, None) == ""


def _flat_consts(code):
    out = set()
    for c in code.co_consts:
        out.add(c)
        if isinstance(c, tuple):
            out.update(c)
    return out


def test_draft_followup_returns_situation_summary_key():
    # Structural lock: handle_draft_followup builds the summary (calls the pure
    # helper) and ships it under the situation_summary key so both the card
    # builder and (if ever re-enabled) the n8n node can consume it.
    code = routes.handle_draft_followup.__code__
    assert "_followup_situation_summary" in code.co_names, code.co_names
    # The dict key is bundled into a BUILD_CONST_KEY_MAP tuple const.
    assert "situation_summary" in _flat_consts(code)


def test_followup_sweep_injects_situation_summary():
    # Structural lock: the sweep reads situation_summary off the draft body and
    # injects it into the posted card.
    assert "situation_summary" in _flat_consts(
        routes.handle_followup_sweep.__code__)


# ── C1.a  hourly-sweep self-disables follow-up emission ──────────────────

class _HourlyEnv:
    """Run handle_hourly_sweep DB-free: _psql -> empty so no rows/loops;
    scan_followup_eligibility -> a spy; disk check -> no-op."""

    def __init__(self):
        self.scan_calls = 0

    def __enter__(self):
        self._psql = routes._psql
        self._scan = server.scan_followup_eligibility
        self._disk = getattr(server, "_disk_alert_check", None)
        routes._psql = lambda *a, **k: ("", None)

        def _spy():
            self.scan_calls += 1
            return [{"customer_id": "25941719961655@lid", "name": "Milena",
                     "label": "HOT", "silence_hours": 43.0,
                     "silence_window": "soft_checkin"}]
        server.scan_followup_eligibility = _spy
        server._disk_alert_check = lambda: None
        return self

    def __exit__(self, *exc):
        routes._psql = self._psql
        server.scan_followup_eligibility = self._scan
        if self._disk is not None:
            server._disk_alert_check = self._disk
        return False


def test_hourly_sweep_suppresses_followups_by_default():
    os.environ.pop("HOURLY_SWEEP_FOLLOWUPS", None)
    with _HourlyEnv() as env:
        s = _Send()
        routes.handle_hourly_sweep({}, s)
    assert s.status == 200, s.status
    assert s.body.get("ok") is True, s.body
    # Engine B self-disabled: empty array + scan NOT even called.
    assert s.body.get("eligible_followups") == [], s.body
    assert env.scan_calls == 0, "legacy follow-up scan must be skipped by default"


def test_hourly_sweep_emits_followups_when_flag_on():
    os.environ["HOURLY_SWEEP_FOLLOWUPS"] = "1"
    try:
        with _HourlyEnv() as env:
            s = _Send()
            routes.handle_hourly_sweep({}, s)
        assert env.scan_calls == 1, "flag on must restore legacy scan"
        assert s.body.get("eligible_followups") and \
            s.body["eligible_followups"][0]["customer_id"] == \
            "25941719961655@lid", s.body
    finally:
        os.environ.pop("HOURLY_SWEEP_FOLLOWUPS", None)


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn(); print("PASS", fn.__name__)
        except AssertionError as e:
            failed += 1; print("FAIL", fn.__name__, "-", e or "assert")
        except Exception as e:
            failed += 1; print("ERROR", fn.__name__, "-", repr(e))
    print(f"{len(fns)-failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
