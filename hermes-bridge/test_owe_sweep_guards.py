#!/usr/bin/env python3
"""Owe-reply sweep card-integrity fixes (Eva/Thunder incident, 2026-06-10).

FIX B — sweep cards must be EDITABLE:
  (1) after posting a card the sweep persists the Telegram message_id onto
      the draft via _draft_update (which also maintains the tgmsg reverse
      index) — without it regen-commit hard-fails ("card.chat_id/message_id
      required") and EVERY operator edit of a sweep card has been silently
      discarded since the sweep went live 2026-06-09 (Eva's 87,731 discount
      edit became a useless "rule captured" notice; exec 22653).
  (2) regen-commit FALLBACK: when card.message_id is missing (legacy sweep
      cards), post the refined text as a FRESH card, persist the new
      message id, update Redis — instead of rejecting the edit.

FIX A — the sweep must not clobber an actively-worked thread:
  (3) skip candidates whose last inbound is fresher than
      OWE_SWEEP_FRESH_SKIP (default 1800 s) — the live draft pipeline owns
      those (the sweep dup'd Eva's 6h-quote card 10 min after her message,
      with an invented undiscounted price, and auto-superseded it);
  (4) skip candidates that already have an open (pending/awaiting_*) draft.

Run: python3 hermes-bridge/test_owe_sweep_guards.py  (or pytest)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import routes  # noqa: E402
import server  # noqa: E402
import review  # noqa: E402
import reengage_quote  # noqa: E402
import waha  # noqa: E402
import hermes_exclusion_guards as heg  # noqa: E402


class _Send:
    def __init__(self):
        self.status, self.body = None, None

    def __call__(self, status, body):
        self.status, self.body = status, body


class _RedisSpy:
    def __init__(self, replies=None):
        self.calls = []
        self.replies = replies or {}

    def __call__(self, args):
        self.calls.append(list(args))
        key = (args[0], args[1] if len(args) > 1 else "")
        if key in self.replies:
            return self.replies[key]
        return self.replies.get(args[0], ("OK", None))


def _patch(mod, **attrs):
    old = {}
    for k, v in attrs.items():
        old[k] = getattr(mod, k, None)
        setattr(mod, k, v)
    return old


def _restore(mod, old):
    for k, v in old.items():
        setattr(mod, k, v)


# ----------------------------------------------------------- sweep harness
def _run_sweep(row, *, latest_draft=None, tg_resp=None):
    """Drive handle_owe_reply_sweep with one candidate row, everything heavy
    faked. Returns (send, spies dict)."""
    spies = {"draft_update": [], "draft_save": [], "tg": [], "followup": []}
    redis = _RedisSpy(replies={
        ("SET", "lock:owe_reply_sweep"): ("OK", None),
        "SET": ("OK", None), "INCR": ("1", None),
    })

    def fake_followup(payload, cap):
        spies["followup"].append(payload)
        cap(200, {"ok": True, "draft_text": "hi Eva — about the Thunder…",
                  "customer_name": row.get("name"), "label": row.get("label"),
                  "quality_badge": "", "situation_summary": ""})

    def fake_tg(method, body, timeout=8):
        spies["tg"].append((method, body))
        return (tg_resp if tg_resp is not None
                else {"ok": True, "result": {"message_id": 777}}), None

    def fake_draft_update(did, fields):
        spies["draft_update"].append((did, dict(fields or {})))
        return {"id": did, **(fields or {})}, None

    olds_s = _patch(
        server,
        read_lead_summary=lambda f: [dict(row)],
        _draft_save=lambda d: spies["draft_save"].append(dict(d)),
        _tg_post=fake_tg,
        _draft_update=fake_draft_update,
        _draft_latest_for_customer=lambda cid, want_status=None: (latest_draft, None),
    )
    olds_rv = _patch(review,
                     _owe_reply_candidates=lambda rows: list(rows),
                     _last_msg_is_inbound=lambda raw: True)
    olds_rq = _patch(reengage_quote,
                     build_followup_card=lambda *a, **k: {
                         "text": "CARD", "reply_markup": {"inline_keyboard": []}})
    olds_w = _patch(waha, phone_for_cid=lambda cid: "",
                    waha_fetch_raw=lambda cid, limit=20: [])
    olds_h = _patch(heg, is_excluded=lambda p: False)
    olds_rt = _patch(routes, _redis=redis,
                     handle_draft_followup=fake_followup)
    os.environ.pop("OWE_SWEEP_FRESH_SKIP", None)
    send = _Send()
    try:
        routes.handle_owe_reply_sweep({"dry_run": False, "limit": 5}, send)
    finally:
        _restore(server, olds_s)
        _restore(review, olds_rv)
        _restore(reengage_quote, olds_rq)
        _restore(waha, olds_w)
        _restore(heg, olds_h)
        _restore(routes, olds_rt)
    assert send.status == 200, send.status
    return send, spies


ROW_OLD = {"customer_id": "199145352634437@lid", "name": "Eva",
           "label": "WARM", "last_customer_message_at_seconds": 7200}


def test_sweep_posts_card_for_genuinely_stale_lead():
    """Anchor: a 2h-stale owed lead with no open draft still gets carded."""
    send, spies = _run_sweep(dict(ROW_OLD))
    assert send.body.get("posted") == 1, send.body
    assert spies["draft_save"], "draft was not saved"
    assert any(m == "sendMessage" for m, _ in spies["tg"])


def test_sweep_backfills_telegram_message_id_after_post():
    """FIX B-1: posted card's message_id must be persisted onto the draft."""
    send, spies = _run_sweep(dict(ROW_OLD))
    assert send.body.get("posted") == 1, send.body
    hits = [(d, f) for d, f in spies["draft_update"]
            if f.get("telegram_message_id") == 777]
    assert hits, f"message_id 777 never persisted: {spies['draft_update']}"
    saved_id = spies["draft_save"][0]["id"]
    assert hits[0][0] == saved_id, "backfill targeted the wrong draft"


def test_sweep_skips_fresh_inbound():
    """FIX A-1: inbound 10 min ago — live pipeline owns it; sweep skips."""
    row = dict(ROW_OLD, last_customer_message_at_seconds=600)
    send, spies = _run_sweep(row)
    assert send.body.get("posted") == 0, send.body
    assert not spies["draft_save"], "sweep drafted a fresh live thread"
    assert send.body.get("skipped_fresh") == 1, send.body


def test_sweep_skips_open_pending_draft():
    """FIX A-2: an open pending card exists — never supersede it."""
    send, spies = _run_sweep(dict(ROW_OLD),
                             latest_draft={"id": "x1", "status": "pending"})
    assert send.body.get("posted") == 0, send.body
    assert not spies["draft_save"]
    assert send.body.get("skipped_open_draft") == 1, send.body


def test_sweep_skips_awaiting_edit_draft():
    send, spies = _run_sweep(dict(ROW_OLD),
                             latest_draft={"id": "x2", "status": "awaiting_edit"})
    assert send.body.get("posted") == 0 and not spies["draft_save"], send.body


def test_sweep_ignores_closed_drafts():
    """A sent/superseded latest draft must NOT block the sweep."""
    send, spies = _run_sweep(dict(ROW_OLD),
                             latest_draft={"id": "x3", "status": "sent"})
    assert send.body.get("posted") == 1, send.body


# ------------------------------------------------------ regen-commit fallback
def _run_regen(card, *, draft=None, tg_resp=None, tg_err=None):
    spies = {"tg": [], "update": []}

    def fake_tg(method, body, timeout=8):
        spies["tg"].append((method, dict(body)))
        if tg_err:
            return None, tg_err
        return (tg_resp if tg_resp is not None
                else {"ok": True, "result": {"message_id": 888}}), None

    def fake_update(did, fields):
        spies["update"].append((did, dict(fields or {})))
        return {"id": did, "messages": fields.get("messages") or []}, None

    olds = _patch(server, _tg_post=fake_tg, _draft_update=fake_update,
                  _draft_get=lambda did: (draft, None))
    send = _Send()
    try:
        routes.handle_queue({"action": "regen-commit", "draft_id": "d1",
                             "fields": {"messages": ["new text"]},
                             "card": card}, send)
    finally:
        _restore(server, olds)
    assert send.status == 200
    return send, spies


def test_regen_commit_normal_path_unchanged():
    send, spies = _run_regen({"chat_id": 55, "message_id": 9000, "text": "T"})
    assert send.body.get("ok") is True, send.body
    assert spies["tg"][0][0] == "editMessageText"


def test_regen_commit_fallback_posts_fresh_card_when_message_id_missing():
    """FIX B-2: legacy sweep card (message_id None) — edit must still land."""
    send, spies = _run_regen({"chat_id": 55, "message_id": None, "text": "T"})
    assert send.body.get("ok") is True, send.body
    assert spies["tg"] and spies["tg"][0][0] == "sendMessage", spies["tg"]
    did, fields = spies["update"][0]
    assert fields.get("telegram_message_id") == 888, \
        "fallback did not persist the new card's message_id"
    assert fields.get("messages") == ["new text"]
    assert send.body.get("fallback_new_card") is True


def test_regen_commit_fallback_uses_draft_chat_when_card_lacks_it():
    send, spies = _run_regen({"message_id": None, "text": "T"},
                             draft={"id": "d1", "telegram_chat_id": 5532831477})
    assert send.body.get("ok") is True, send.body
    assert spies["tg"][0][1].get("chat_id") == 5532831477


def test_regen_commit_fallback_fails_closed_without_any_chat():
    send, spies = _run_regen({"message_id": None, "text": "T"}, draft=None)
    assert send.body.get("ok") is False
    assert not spies["update"], "Redis must stay unchanged when no card lands"


def test_regen_commit_fallback_telegram_failure_leaves_redis_unchanged():
    send, spies = _run_regen({"chat_id": 55, "message_id": None, "text": "T"},
                             tg_err="HTTP 400: kaput")
    assert send.body.get("ok") is False
    assert not spies["update"]


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
