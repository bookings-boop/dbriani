#!/usr/bin/env python3
"""routes.py — HTTP endpoint handlers extracted from server.py's
HTTPRequestHandler class (Phase G of the refactoring split).

Each handler is a standalone function with this exact signature:

    def handle_<name>(payload: dict, send: Callable) -> None

`payload` is the parsed JSON request body.
`send(status_code, dict)` is the JSON response writer — originally
the Handler's `self._send`, now passed in so handlers don't depend
on BaseHTTPRequestHandler.

server.py's `do_POST` dispatches each path to the matching
`handle_*` function. Helpers that still live in server.py (DB CRUD,
Hermes wrappers) are imported inside each function from a generated
`from server import (...)` block — keeps dependencies explicit and
avoids module-load-time circular imports (server.py imports routes
at its module top, so server-side names are only accessed when
handlers run, after server.py is fully loaded).

Extracted mechanically via scripts/extract_routes.py."""
import json
import os  # noqa: F401 — used by endpoint bodies for env reads
import random
import re
import subprocess  # noqa: F401 — used by handler bodies via late deps
import time
import urllib.error  # noqa: F401
import urllib.request  # noqa: F401
import uuid  # noqa: F401 — used by handle_* bodies (Phase G regression
            # fix: was at server.py module top before the refactor;
            # routes.py inherited the use sites but not the import)

from db import _psql, _lit, _redis  # noqa: F401
from util import log  # noqa: F401


# --- on-demand re-analysis queue (R2, 2026-06-01) ---------------------------
# A fresh customer inbound extracts facts but does NOT re-analyze (only the
# hourly sweep writes verdict/score) → cards show stale facts + "not analyzed"
# until the next hourly window. _enqueue_reanalyze() queues the lead on each
# inbound; the queue is DRAINED by /pipeline-analyze {source:"reanalyze"}
# (reuses the sweep's SHARED lock + per-lead analysis + timeout handling, so
# it can never run concurrently with the hourly sweep on the 2-vCPU box).
# INERT until an n8n cron POSTs that endpoint — enqueue is cheap Redis only.
_REANALYZE_QUEUE = "hermes:reanalyze:queue"
_REANALYZE_QUEUED = "hermes:reanalyze:queued:"   # + cid (dedup marker)


def _enqueue_reanalyze(cid):
    """Queue a customer for on-demand re-analysis after a fresh inbound.
    Deduped (SET NX, 10 min) so repeated messages don't pile up; bounded
    (LTRIM) so the list can't grow unbounded if the cron is disabled.
    Fail-silent — never block the inbound/draft path on Redis."""
    cid = (cid or "").strip()
    if not cid:
        return
    try:
        added, _ = _redis(["SET", _REANALYZE_QUEUED + cid, "1", "NX", "EX", "600"])
        if (added or "").strip().upper() != "OK":
            return  # already queued in the last 10 min
        _redis(["RPUSH", _REANALYZE_QUEUE, cid])
        _redis(["LTRIM", _REANALYZE_QUEUE, "-500", "-1"])  # safety bound
    except Exception as e:
        log("enqueue_reanalyze err:", repr(e))


def _drain_reanalyze_queue(limit):
    """Pop up to `limit` UNIQUE cids off the reanalyze queue and clear their
    dedup markers. Returns a list (possibly empty). Fail-safe."""
    try:
        out, _ = _redis(["LPOP", _REANALYZE_QUEUE, str(int(limit))])
    except Exception as e:
        log("drain_reanalyze err:", repr(e))
        return []
    cids, seen = [], set()
    for ln in (out or "").splitlines():
        c = ln.strip()
        if c and c not in seen:
            seen.add(c)
            cids.append(c)
            try:
                _redis(["DEL", _REANALYZE_QUEUED + c])
            except Exception:
                pass
    return cids


def resolve_target(payload):
    """Return (customer_id, error_text). On success: ('cid…@lid', None).
    On any miss: ('', '⚠️ ...'). Handles either:
      - {customer_id: '…@lid'}   — direct
      - {name: 'Mark'}           — name lookup via resolve_customer_by_name

    Was self._resolve_target on Handler; lifted to module-level so any
    handler can call it as resolve_target(payload). resolve_customer_by_name
    is imported late to avoid circular at module load."""
    from server import resolve_customer_by_name

    cid = (payload.get("customer_id") or "").strip()
    if cid:
        return cid, None
    name_query = (payload.get("name") or "").strip()
    if not name_query:
        return "", "⚠️ Provide a name or customer id."
    resolved, matches = resolve_customer_by_name(name_query)
    if resolved:
        return resolved, None
    if matches:
        lines = [f"⚠️ Multiple matches for {name_query!r}. Try one of:"]
        for m in matches[:5]:
            lines.append(f"  • {m['name']} — `{m['customer_id']}`")
        return "", "\n".join(lines)
    return "", f"⚠️ No customer found for {name_query!r}."


def update_last_analysis(customer_id, signal, confidence):
    """UPSERT conversation_state.last_analysis_{signal,confidence}.
    Idempotent. Called by handle_label_eval on every evaluation regardless
    of whether the label actually changed — keeps the analysis bookkeeping
    fresh for the hourly sweep's skip-if-unchanged logic."""
    cid = (customer_id or "").replace("'", "''")
    sig = _lit(signal or "")
    conf = float(confidence)
    sql = (
        "INSERT INTO conversation_state "
        "(customer_id, last_analysis_signal, last_analysis_confidence, "
        " last_analyzed_at, updated_at) "
        f"VALUES ('{cid}', {sig}, {conf:.4f}, now(), now()) "
        "ON CONFLICT (customer_id) DO UPDATE "
        f"SET last_analysis_signal = {sig}, "
        f"    last_analysis_confidence = {conf:.4f}, "
        "    last_analyzed_at = now(), updated_at = now()"
    )
    _psql(sql)


# ============================================================================
# Group: utilities — _debounce, _autonomous_log, _queue
# ============================================================================

def handle_debounce(payload, send):
    """BUG-1 fix: Redis-backed inbound debounce for FR-3.

    n8n staticData is loaded per-execution and is NOT shared across the
    concurrent executions one-per-inbound-message spawns, so the old
    staticData 'latest token wins' check was inert — every execution saw
    only its own token and flushed, yielding one draft per message.

    Redis is shared + atomic. Keys (TTL DEBOUNCE_TTL):
      debounce:buf:<phone>  list  — buffered {id,text} messages
      debounce:seq:<phone>  str   — token of the most recent message

    action=buffer : append the message, stamp a fresh token, return it.
    action=flush  : the caller passes the token it was given; if it still
                    equals debounce:seq it is the latest message — drain +
                    delete the buffer and return the combined text; if not,
                    a newer message arrived and the caller must suppress
                    its draft (latest=false).
    Fail-OPEN: any Redis error returns latest=true so a customer message
    is never dropped (a rare duplicate draft is acceptable; a lost message
    is not)."""
    from server import DEBOUNCE_TTL
    action = (payload.get("action") or "").strip().lower()
    phone = (payload.get("phone") or "").strip()
    if not phone:
        send(400, {"ok": False, "error": "phone is required"})
        return
    bufkey = "debounce:buf:" + phone
    seqkey = "debounce:seq:" + phone
    if action == "buffer":
        token = (str(int(time.time() * 1000)) + "_"
                 + "".join(random.choice("0123456789abcdef")
                           for _ in range(6)))
        msg = json.dumps({"id": str(payload.get("message_id") or ""),
                          "text": str(payload.get("text") or "")})
        _redis(["RPUSH", bufkey, msg])
        _redis(["EXPIRE", bufkey, str(DEBOUNCE_TTL)])
        _, err = _redis(["SET", seqkey, token, "EX", str(DEBOUNCE_TTL)])
        if err:
            log("debounce buffer failed:", err)
        log(f"debounce BUFFER {phone} token={token}")
        send(200, {"ok": True, "phone": phone, "token": token})
        return
    if action == "flush":
        token = (payload.get("token") or "").strip()
        cur, err = _redis(["GET", seqkey])
        if err:
            log("debounce flush GET failed (fail-open):", err)
            send(200, {"ok": True, "latest": True, "degraded": True,
                             "combined": "", "ids": [], "count": 0})
            return
        if (cur or "").strip() != token:
            # a newer message holds the token (or the window lapsed) —
            # this execution must not produce a draft
            send(200, {"ok": True, "latest": False})
            return
        rng, _ = _redis(["LRANGE", bufkey, "0", "-1"])
        _redis(["DEL", bufkey])
        _redis(["DEL", seqkey])
        msgs = []
        for ln in (rng or "").splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                msgs.append(json.loads(ln))
            except Exception:
                msgs.append({"id": "", "text": ln})
        texts = [str(m.get("text", "")) for m in msgs
                 if str(m.get("text", "")).strip()]
        ids = [m.get("id", "") for m in msgs]
        log(f"debounce FLUSH {phone} latest=true count={len(msgs)}")
        send(200, {"ok": True, "latest": True,
                         "combined": "\n".join(texts),
                         "ids": ids, "count": len(msgs)})
        return
    send(400, {"ok": False, "error": "action must be buffer|flush"})

def handle_autonomous_log(payload, send):
    """POST /autonomous-log — generic event row into autonomous_sends.
    Body: {customer_id, kind, notes?}. `kind` is the literal string
    (e.g. 'payment_link_sent', 'edit_link_sent') — NO prefix added
    (unlike /followup-action which prefixes 'proactive_followup_').
    `notes` is an arbitrary JSON object stored as jsonb. Fail-safe:
    always returns 200; logs but never raises."""
    cid = (payload.get("customer_id") or "").strip()
    kind = (payload.get("kind") or "").strip()
    notes = payload.get("notes") or {}
    if not cid or not kind:
        send(200, {"ok": False,
                         "error": "customer_id + kind required"})
        return
    # Whitelist character set on kind — keeps it INSERT-safe even though
    # we _lit-escape below.
    if not re.fullmatch(r"[A-Za-z0-9_]{1,64}", kind):
        send(200, {"ok": False,
                         "error": "kind must match [A-Za-z0-9_]{1,64}"})
        return
    if not isinstance(notes, dict):
        notes = {"raw": str(notes)}
    try:
        sql = (
            "INSERT INTO autonomous_sends (customer_id, kind, notes) "
            f"VALUES ({_lit(cid)}, {_lit(kind)}, "
            f"{_lit(json.dumps(notes))}::jsonb)"
        )
        _, err = _psql(sql)
        if err:
            log("autonomous_log insert err:", err)
            send(200, {"ok": False, "degraded": True,
                             "error": err[:200]})
            return
        log(f"autonomous-log cid={cid!r} kind={kind!r}")
        send(200, {"ok": True, "customer_id": cid, "kind": kind})
    except Exception as e:
        log("autonomous_log EXC:", repr(e))
        send(200, {"ok": False, "degraded": True, "error": str(e)})

def handle_queue(payload, send):
    """POST /queue — Redis-backed pendingQueue.
    See docs/pendingqueue-redis-migration-plan.md §3.
    Actions: save | get | update | mark | latest-for-customer | drop |
    regen-commit | claim-send | get-by-tgmsg | awaiting.
    Always 200; ok flag carries the outcome."""
    from server import (
        _draft_awaiting,
        _draft_by_tgmsg,
        _draft_drop,
        _draft_get,
        _draft_latest_for_customer,
        _draft_save,
        _draft_update,
    )
    action = (payload.get("action") or "").strip().lower()

    if action == "get-by-tgmsg":
        # Resolve a Telegram message_id (the card the operator replied
        # to) back to its draft. Redis-migration 2026-05-28 — replaces
        # staticData.pendingQueue.find(d.telegram_message_id === mid).
        mid = payload.get("message_id")
        d, err = _draft_by_tgmsg(mid)
        send(200, {"ok": True, "draft": d,
                         "found": d is not None, "error": err})
        return

    if action == "awaiting":
        # Return the draft blocking on operator input for a given
        # status ('awaiting_edit' or 'awaiting_amount'). Replaces
        # staticData's queue.find(x => x.status === '...').
        status = (payload.get("status") or "awaiting_edit").strip()
        chat = payload.get("chat_id")
        d, err = _draft_awaiting(status, chat)
        send(200, {"ok": True, "draft": d,
                         "found": d is not None, "error": err})
        return

    if action == "awaiting-info":
        # Pillar C: is this chat waiting on an answer to a Hermes
        # ask-before-guess question? (set by /ask-operator). Returns the
        # stored {customer_id, question, ...} so the operator's reply is
        # routed to /answer-info instead of being treated as a draft edit.
        # NOTE: _redis is the module-level import (routes.py top). Do NOT
        # add a local `from db import _redis` here — a function-local import
        # shadows _redis for ALL of handle_queue and makes the claim-send
        # path UnboundLocalError on every send (2026-06-01 incident: literal
        # `false` sent to customers + dup draft cards). See test_claim_send.py.
        chat = str(payload.get("chat_id") or "").strip()
        out, _e = _redis(["GET", "hermes:awaiting_info:" + chat])
        out = (out or "").strip()
        info = None
        if out:
            try:
                info = json.loads(out)
            except Exception:
                info = None
        send(200, {"ok": True, "found": info is not None, "info": info})
        return

    if action == "save":
        draft = payload.get("draft") or {}
        ok, err = _draft_save(draft)
        send(200, {"ok": ok, "error": err,
                         "draft_id": (draft.get("id") if isinstance(draft, dict) else None)})
        return

    if action == "get":
        did = (payload.get("draft_id") or "").strip()
        if not did:
            send(200, {"ok": False, "error": "draft_id required",
                             "draft": None, "found": False})
            return
        d, err = _draft_get(did)
        # send_claimed: a send was CLAIMED for this draft (claim key set at
        # send-start, before WAHA + before status flips to 'sent'). The
        # show-then-upgrade branch uses this to avoid re-opening a draft whose
        # send is in flight — closing the race where status is still 'pending'
        # at recheck time (2026-06-02).
        _clm, _ = _redis(["EXISTS", f"draft:send_claim:{did}"])
        send(200, {"ok": True, "draft": d,
                         "found": d is not None, "error": err,
                         "send_claimed": str(_clm or "0").strip() == "1"})
        return

    if action == "update":
        did = (payload.get("draft_id") or "").strip()
        d, err = _draft_update(did, payload.get("fields") or {})
        send(200, {"ok": d is not None, "draft": d, "error": err})
        return

    if action == "regen-commit":
        # Atomic regen apply. Edit the Telegram card FIRST, then write
        # the new content to Redis — but ONLY if the card edit landed.
        #
        # Production bug 2026-05-28 ("I pressed send and the message
        # sent was completely different than what I saw; regenerate
        # isn't working"): regen used two separate n8n nodes — one to
        # write Redis, one to edit the card. When the n8n execution
        # crashed between them (box under load), Redis ended up ahead of
        # the card, so Send (which reads Redis) pushed text the operator
        # never saw. The classic-regen branch was worse: it wrote only
        # staticData (a casualty of the Redis migration) and Redis was
        # never updated at all → Send pushed the PRE-regen text.
        #
        # Doing both writes inside one bridge call makes them atomic from
        # n8n's view (one node = one HTTP call). Card-FIRST ordering
        # means a Telegram failure leaves BOTH stores on the old content
        # (regen visibly "didn't take") — never the dangerous direction
        # (Redis ahead of an unseen card). Invariant: Redis is updated
        # iff the card shows the new draft, so Send == what's on screen.
        from server import _tg_post
        did = (payload.get("draft_id") or "").strip()
        fields = payload.get("fields") or {}
        card = payload.get("card") or {}
        if not did:
            send(200, {"ok": False, "error": "draft_id required"})
            return
        chat_id = card.get("chat_id")
        message_id = card.get("message_id")
        if not (chat_id and message_id):
            send(200, {"ok": False,
                       "error": "card.chat_id/message_id required"})
            return
        tg_body = {"chat_id": chat_id,
                   "message_id": int(message_id),
                   "text": (card.get("text") or "")[:4000]}
        if card.get("parse_mode"):
            tg_body["parse_mode"] = card["parse_mode"]
        if card.get("reply_markup"):
            tg_body["reply_markup"] = card["reply_markup"]
        r, err = _tg_post("editMessageText", tg_body)
        # Telegram returns 400 "message is not modified" when the card
        # already shows this exact text — that means the card IS in the
        # desired state, so a retry (n8n timed out then re-fired) is a
        # success, not a failure. Treat it as ok and fall through to the
        # idempotent Redis write.
        not_modified = bool(err and "not modified" in err.lower())
        if r is None and not not_modified:
            log(f"REGEN_COMMIT id={did} card-edit FAILED err={err} "
                f"— Redis left unchanged (card+Redis stay consistent)")
            send(200, {"ok": False, "stage": "telegram",
                       "error": "card edit failed: " + str(err)})
            return
        d, uerr = _draft_update(did, fields)
        if d is None:
            log(f"REGEN_COMMIT id={did} card-edited OK but Redis "
                f"update FAILED err={uerr}")
            send(200, {"ok": False, "stage": "redis",
                       "error": "redis update failed: " + str(uerr)})
            return
        log(f"REGEN_COMMIT id={did} OK card+redis in sync "
            f"msgs={len(d.get('messages') or [])} "
            f"{'(not-modified)' if not_modified else ''}".strip())
        send(200, {"ok": True, "draft": d})
        return

    if action == "mark":
        did = (payload.get("draft_id") or "").strip()
        status = (payload.get("status") or "").strip()
        if not status:
            send(200, {"ok": False, "error": "status required",
                             "draft": None})
            return
        d, err = _draft_update(did, {"status": status})
        send(200, {"ok": d is not None, "draft": d, "error": err})
        return

    if action == "latest-for-customer":
        cid = (payload.get("customer_id") or "").strip()
        want_status = (payload.get("status") or "").strip() or None
        d, err = _draft_latest_for_customer(cid, want_status)
        send(200, {"ok": True, "draft": d,
                         "found": d is not None, "error": err})
        return

    if action == "drop":
        did = (payload.get("draft_id") or "").strip()
        ok, err = _draft_drop(did)
        send(200, {"ok": ok, "error": err})
        return

    if action == "claim-send":
        # Atomic send-claim. Use Redis SET NX EX so the FIRST caller
        # wins; subsequent concurrent callers get ok=False and must
        # skip the WAHA send. Production bug 2026-05-27: operator
        # double-tapped [✅ Send] OR a parallel Telegram-callback
        # retry fired two execs against the same draft_id. Both read
        # draft.messages_sent_count = 0 before either incremented,
        # so both passed the check-then-set guard in Prepare Send →
        # customer got 2 sends. This claim closes the race at
        # workflow start — Prepare Send calls /queue?action=claim-send
        # before fanning items.
        did = (payload.get("draft_id") or "").strip()
        ttl = int(payload.get("ttl") or 60)
        if not did:
            send(200, {"ok": False, "error": "draft_id required"})
            return
        # WAHA-degraded HARD send-block (2026-06-02): approving a draft while
        # the WAHA session is in a definitive non-sending state would silently
        # fail (operator thinks it sent, customer gets nothing). Block by
        # returning ok=False (Prepare Send already skips the WAHA send when the
        # claim isn't won) WITHOUT consuming the claim key — so a retry after
        # the session recovers isn't wedged — and tell the operator directly.
        # Only DEFINITIVE bad states block (labels._waha_send_blocked); WORKING
        # + ambiguous probes still send, and we fail OPEN on any guard error so
        # a probe bug can never block every send.
        try:
            from waha import waha_session_ok
            from labels import _waha_send_blocked
            _wok, _wstatus = waha_session_ok()
            _block = _waha_send_blocked(_wok, _wstatus)
        except Exception:
            _block, _wstatus = False, ""
        if _block:
            log(f"send-claim WAHA-BLOCKED did={did} status={_wstatus}")
            try:
                from server import _tg_post, DEFAULT_ADMIN_CHAT
                _tg_post("sendMessage", {
                    "chat_id": DEFAULT_ADMIN_CHAT,
                    "text": (f"🚫 Send blocked — WhatsApp session is {_wstatus}, "
                             f"so the message was NOT sent (it would have "
                             f"silently failed). The watchdog restarts a STOPPED "
                             f"session within ~3 min; once it's WORKING again, "
                             f"tap ✅ Send.")})
            except Exception as _be:
                log("send-claim waha-block notify err:", repr(_be))
            send(200, {"ok": False, "blocked": "waha_degraded",
                       "status": _wstatus, "draft_id": did})
            return
        # Status guard (2026-06-02 double-send): never send a draft that is
        # already terminal. A second/stale card for the same customer (e.g. an
        # unmerged @lid vs @c.us identity split, or an already-sent draft) must
        # not re-send. Fail OPEN on a lookup error — only an UNAMBIGUOUS
        # terminal status blocks, so a transient Redis/DB hiccup can never wedge
        # a legitimate first send.
        try:
            _sd, _sderr = _draft_get(did)
            _sdstatus = (_sd or {}).get("status") if not _sderr else None
        except Exception:
            _sdstatus = None
        from labels import _send_blocked_by_status
        if _sdstatus is not None and _send_blocked_by_status(_sdstatus):
            log(f"send-claim STATUS-BLOCKED did={did} status={_sdstatus}")
            try:
                from server import _tg_post, DEFAULT_ADMIN_CHAT
                _tg_post("sendMessage", {
                    "chat_id": DEFAULT_ADMIN_CHAT,
                    "text": (f"🚫 Not sent — this draft is already "
                             f"<b>{_sdstatus}</b>. Guarding against a "
                             f"double-send / stale duplicate card."),
                    "parse_mode": "HTML"})
            except Exception as _be:
                log("send-claim status-block notify err:", repr(_be))
            send(200, {"ok": False, "blocked": f"already_{_sdstatus}",
                       "status": _sdstatus, "draft_id": did})
            return
        # Customer-level recent-send guard (2026-06-02): blocks a second
        # claim for the same customer's phone within the claim window.
        # Prevents the pre-merge sibling double-send (two pending cards for
        # the same human due to @lid/@c.us identity split). Fail OPEN —
        # only an affirmative GET blocks; any Redis/lookup error passes.
        # Override: the ✅ Force Send inline button in the block message
        # (n8n callback route for forcesend: wires the bypass; see item 6).
        _force = bool(payload.get("force"))
        _phone = ((_sd or {}).get("customer_phone") or "").strip()
        _cname = ((_sd or {}).get("customer_name") or _phone or "this customer").strip()
        if _phone and not _force:
            _ckey = f"csent:{_phone}"
            try:
                _cr, _crerr = _redis(["GET", _ckey])
                # Only a DIFFERENT draft_id is a genuine sibling card. The same
                # card re-tapped (its own send still in flight) must NOT show the
                # "sibling/duplicate cards" message — fall through to the
                # per-draft claim + status guards (2026-06-02 false-positive:
                # 194905951437046@lid, a single un-merged card).
                from labels import _sibling_send_blocked
                _cblocked = (not _crerr) and _sibling_send_blocked(_cr, did)
            except Exception:
                _cblocked = False
            if _cblocked:
                log(f"send-claim CUSTOMER-BLOCKED did={did} phone={_phone}")
                try:
                    import json as _cjson
                    from server import _tg_post, DEFAULT_ADMIN_CHAT
                    _tg_post("sendMessage", {
                        "chat_id": DEFAULT_ADMIN_CHAT,
                        "text": (f"⚠️ <b>{_cname}</b> — skipped: already sent "
                                 f"to this customer in the last minute (sibling-"
                                 f"card guard). Tap Force Send to override, or "
                                 f"run <code>/reconcile-identities</code> to "
                                 f"merge the duplicate cards."),
                        "parse_mode": "HTML",
                        "reply_markup": _cjson.dumps({
                            "inline_keyboard": [[
                                {"text": "✅ Force Send",
                                 "callback_data": f"forcesend:{did}"}
                            ]]
                        })
                    })
                except Exception as _cbe:
                    log("send-claim customer-block notify err:", repr(_cbe))
                send(200, {"ok": False, "blocked": "customer_recent_send",
                           "draft_id": did})
                return
        key = f"draft:send_claim:{did}"
        out, err = _redis(["SET", key, "1", "NX", "EX", str(ttl)])
        # _redis returns the raw text "OK" on success, "" on
        # already-set (NX). On error, err is set.
        claimed = bool(out and out.strip().upper() == "OK")
        if claimed:
            log(f"send-claim WON did={did} ttl={ttl}s")
            # Mark this phone so a sibling card's claim-send is blocked
            # within the same window. NX: first WON claim wins; the
            # phone key expires with the draft claim TTL.
            if _phone:
                try:
                    _redis(["SET", f"csent:{_phone}", did, "NX",
                            "EX", str(ttl)])
                except Exception:
                    pass
        else:
            log(f"send-claim BLOCKED did={did} "
                f"(another exec is already sending)")
        send(200, {"ok": claimed, "error": err,
                         "draft_id": did, "ttl": ttl})
        return

    send(200, {"ok": False,
                     "error": ("action must be save|get|update|mark|"
                               "latest-for-customer|drop|claim-send|"
                               "get-by-tgmsg|awaiting")})


# ============================================================================
# Group: customer facts
# ============================================================================

def handle_customer_facts(payload, send):
    """feature-header: maintain customer_facts + return a context header.
    Fail-safe — ANY error degrades to cached facts and still returns 200,
    so the workflow's draft card is never blocked by header logic.

    Production bug 2026-05-27: operator's behavioral feedback was being
    saved (49 rules in 48h via /feedback) but NEVER reaching Hermes —
    invocation_count = 0 on every rule. Operator was re-typing the
    same feedback ("no emoji", "include URLs", "AED 450 flowers")
    repeatedly because the n8n Build Prompt wasn't pulling
    behavioral-context. Fix: handle_customer_facts now appends the
    deduplicated active rules + customer notes to the returned
    customer_header. n8n's Build Prompt already uses customer_header
    in the system prompt, so this closes the feedback loop with NO
    n8n workflow change. Rules also surface in the Telegram card so
    the operator can see what's active."""
    from server import (
        _facts_extract_gate,
        _merge_facts,
        behavioral_context,
        build_customer_header,
        extract_customer_facts,
        get_customer_facts,
        upsert_customer_facts,
        waha_lookup_push_name,
    )

    def _build_behavioral_addendum(cid_arg):
        """Pull active rules + customer notes, dedupe, format for
        prompt+display. Returns ('' if nothing, else block of text)."""
        try:
            ctx = behavioral_context(cid_arg) or {}
            globals_ = ctx.get("global") or []
            scenario = ctx.get("scenario") or []
            notes = ctx.get("customer_notes") or []
            # Dedupe: lowercase fingerprint of first 40 chars catches
            # the "Always include URLs" 6-variants problem.
            seen = set()
            uniq_globals = []
            for r in globals_:
                fp = (r or "").lower().strip()[:40]
                if fp and fp not in seen:
                    seen.add(fp)
                    uniq_globals.append(r)
            if not (uniq_globals or scenario or notes):
                return ""
            lines = [
                "",
                "─" * 30,
                "🧠 ACTIVE BEHAVIORAL RULES "
                "(operator feedback — FOLLOW THESE):"
            ]
            for r in uniq_globals:
                lines.append(f"• {r}")
            if scenario:
                lines.append("")
                lines.append("SCENARIO RULES:")
                for r in scenario:
                    lines.append(f"• {r}")
            if notes:
                lines.append("")
                lines.append("NOTES FOR THIS CUSTOMER:")
                for n in notes:
                    lines.append(f"• {n}")
            return "\n".join(lines)
        except Exception as _e:
            log(f"behavioral addendum err: {_e!r}")
            return ""
    cid = (payload.get("customer_id") or "").strip()
    cname = (payload.get("customer_name") or "").strip()
    msg = payload.get("incoming_message") or ""
    history = payload.get("history") or ""
    is_refine = bool(payload.get("is_refine"))
    try:
        cached = get_customer_facts(cid)
        if is_refine:
            # a refine re-renders an existing draft — not a new inbound:
            # no extraction, no increment. Header from cached facts only.
            f = cached or {}
            mc = (cached or {}).get("message_count", 0)
            hdr = build_customer_header({
                "name": f.get("name") or cname,
                "dates": f.get("dates", ""), "yachts": f.get("yachts", ""),
                "party_size": f.get("party_size", ""),
                "message_count": mc})
            addendum = _build_behavioral_addendum(cid)
            send(200, {"ok": True, "extracted": False,
                             "message_count": mc,
                             "customer_header": hdr,
                             "behavioral_addendum": addendum})
            return
        do_extract = (cached is None) or _facts_extract_gate(msg)
        if do_extract:
            merged = _merge_facts(cached,
                                  extract_customer_facts(msg, history))
        else:
            merged = _merge_facts(cached, None)
        if not (merged.get("name") or "").strip():
            merged["name"] = cname
        # WAHA pushName fallback — the WAHA webhook payload doesn't carry
        # notifyName/pushName, so the workflow-supplied cname is empty for
        # virtually every customer. Result: /review cards showed "Unknown"
        # for 10 of 11 customers (operator-visible confusion). When neither
        # the LLM extraction nor the workflow gave us a name, look it up
        # in WAHA's chat list. Cached in-process for 5 minutes.
        if not (merged.get("name") or "").strip():
            try:
                pn = waha_lookup_push_name(cid)
                if pn:
                    merged["name"] = pn
                    log(f"customer_facts WAHA pushName fallback cid={cid!r} "
                        f"name={pn!r}")
            except Exception as _e:
                log("customer_facts WAHA pushName lookup err:", repr(_e))
        new_count, err = upsert_customer_facts(cid, merged["name"], merged)
        if err:
            log("customer_facts upsert failed:", err)
        mc = new_count if isinstance(new_count, int) \
            else ((cached or {}).get("message_count", 0) + 1)
        hdr = build_customer_header({**merged, "message_count": mc})
        addendum = _build_behavioral_addendum(cid)
        if do_extract:
            # R2: a fresh inbound that carried facts — queue a re-analysis so
            # the verdict/score + card facts refresh promptly (drained by the
            # reanalyze cron), instead of waiting for the next hourly sweep.
            _enqueue_reanalyze(cid)
        log(f"customer-facts cid={cid!r} extract={do_extract} msg#{mc} "
            f"behavioral_rules_attached={'yes' if addendum else 'no'}")
        send(200, {"ok": True, "extracted": bool(do_extract),
                         "message_count": mc,
                         "customer_header": hdr,
                         "behavioral_addendum": addendum})
    except Exception as e:
        log("customer_facts ERROR:", repr(e))
        cached = None
        try:
            cached = get_customer_facts(cid)
        except Exception:
            pass
        mc = (cached or {}).get("message_count", 0)
        hdr = build_customer_header({
            "name": (cached or {}).get("name") or cname,
            "dates": (cached or {}).get("dates", ""),
            "yachts": (cached or {}).get("yachts", ""),
            "party_size": (cached or {}).get("party_size", ""),
            "message_count": mc})
        addendum = _build_behavioral_addendum(cid)
        send(200, {"ok": True, "extracted": False, "degraded": True,
                         "message_count": mc,
                         "customer_header": hdr,
                         "behavioral_addendum": addendum})

# (moved to routes.py — handle_<name>(payload, self._send))

def handle_refresh_facts(payload, send):
    """POST /refresh-facts — pull a customer's WAHA history and re-run
    Hermes extraction over the FULL conversation (not just one message).
    Body: {customer_id|name|phone}. Returns the refreshed customer_facts
    row + WAHA history summary. Operator-on-demand command."""
    from server import (
        _merge_facts,
        extract_customer_facts,
        get_current_label_row,
        get_customer_facts,
        resolve_customer_by_name,
        resolve_customer_by_phone,
        upsert_customer_facts,
        waha_fetch_history,
    )
    cid = (payload.get("customer_id") or "").strip()
    name_query = (payload.get("name") or "").strip()
    phone_query = (payload.get("phone") or "").strip()
    if not cid and phone_query:
        resolved, matches = resolve_customer_by_phone(phone_query)
        if resolved:
            cid = resolved
        elif matches:
            # Ambiguous — show the operator the candidates so they can
            # /refresh @lid<id> directly.
            lines = ["⚠️ Multiple customers match that phone:"]
            for m in matches[:5]:
                nm = m.get("name") or "(no name)"
                lines.append(f"   `{m['customer_id']}` — {nm}")
            lines.append("Re-run /refresh with the @lid id.")
            send(200, {"ok": False, "error": "ambiguous phone",
                             "telegram_text": "\n".join(lines)})
            return
    if not cid and name_query:
        resolved, _ = resolve_customer_by_name(name_query)
        if resolved:
            cid = resolved
    if not cid:
        send(200, {"ok": False,
                         "error": "customer_id, name, or phone required",
                         "telegram_text": "⚠️ Customer not found."})
        return
    try:
        waha = waha_fetch_history(cid, limit=100)
        if waha.get("err"):
            send(200, {"ok": False, "error": waha["err"],
                             "telegram_text": f"⚠️ WAHA fetch failed: {waha['err']}"})
            return
        if waha["count"] == 0:
            send(200, {"ok": False,
                             "error": "no messages in WAHA",
                             "telegram_text": "⚠️ No chat history for that customer."})
            return
        # Re-extract via the same path /customer-facts uses, but feed full history
        extracted = extract_customer_facts(waha["last_message"], waha["history"])
        cached = get_customer_facts(cid)
        merged = _merge_facts(cached, extracted) if extracted else _merge_facts(cached, None)
        # Same phone-format pushName guard as the per-message path: never
        # store a phone-format WhatsApp display name as the customer's
        # name (it masks a real name and shows a number in /review).
        if not (merged.get("name") or "").strip():
            _pn = (waha.get("push_name") or "").strip()
            if _pn and re.search(r"[A-Za-z]{2,}", _pn) \
                    and not re.fullmatch(r"[+\d\s().\-]+", _pn):
                merged["name"] = _pn
        new_count, err = upsert_customer_facts(cid, merged.get("name") or "", merged)
        if err:
            log("refresh_facts upsert err:", err)
        row = get_current_label_row(cid) or {}
        name = row.get("name") or merged.get("name") or "(no name)"
        yachts = row.get("yachts") or merged.get("yachts") or ""
        dates = row.get("dates") or merged.get("dates") or ""
        party = merged.get("party_size", "") or ""
        mc = new_count if isinstance(new_count, int) else row.get("message_count", 0)
        text = (
            f"🔄 *Refreshed* `{cid}`\n"
            f"   *{name}*\n"
            f"   🛥 {yachts or '(none)'}\n"
            f"   📅 {dates or '(none)'}\n"
            f"   👥 {party or '(none)'}\n"
            f"   📊 label *{row.get('label','?')}* · msg #{mc} · "
            f"{waha['count']} msgs in WAHA history"
        )
        log(f"refresh-facts cid={cid!r} name={name!r} yachts={yachts!r} "
            f"waha_count={waha['count']}")
        send(200, {
            "ok": True, "customer_id": cid,
            "facts": merged, "label": row.get("label"),
            "waha_count": waha["count"], "telegram_text": text,
        })
    except Exception as e:
        log("refresh_facts ERROR:", repr(e))
        send(200, {"ok": False, "degraded": True, "error": str(e),
                         "telegram_text": f"⚠️ Refresh failed: {e}"})


# ============================================================================
# Group: payments
# ============================================================================

def handle_payment_link(payload, send):
    """Nomod minimal build: create a payment link for a confirmed booking.
    Fail-safe — ALWAYS returns 200 so the workflow's draft is never blocked;
    an ok:false response just means the card posts without a link."""
    from server import PAYMENTS_ENABLED, nomod_create_link
    cid = (payload.get("customer_id") or "").strip()
    cname = (payload.get("customer_name") or "").strip()
    summary = (payload.get("payment_summary") or "").strip()
    if not PAYMENTS_ENABLED:
        log("payment-link refused — PAYMENTS_ENABLED is off")
        send(200, {"ok": False, "error": "payments are disabled"})
        return
    try:
        amount = float(payload.get("amount"))
    except (TypeError, ValueError):
        amount = 0.0
    # basic input validation only — NOT a business cap (per plan)
    if amount <= 0:
        log("payment-link refused — invalid amount %r"
            % payload.get("amount"))
        send(200, {"ok": False,
                         "error": "invalid amount: %r"
                                  % payload.get("amount")})
        return
    url, lid, err = nomod_create_link(amount, summary, cname)
    if err:
        log("payment-link FAILED customer=%s amount=AED%.2f err=%s"
            % (cid, amount, err))
        send(200, {"ok": False, "error": err})
        return
    log("payment-link OK customer=%s amount=AED%.2f link_id=%s"
        % (cid, amount, lid))
    send(200, {"ok": True, "link_url": url, "link_id": lid,
                     "amount": amount})

def handle_poll_payments(payload, send):
    """POST /poll-payments — fetch recent Nomod charges, match each to
    a customer via 3-layer priority, log payment_received to
    autonomous_sends, promote matched customers to CONFIRMED, return
    match + unmatched lists for caller (n8n cron) to fire Telegram
    notifications. Redis-deduped on charge.id (TTL 30d).

    Body (all optional):
      window_hours: int — only consider charges with created >= now-N
                   hours. Default 24 (catches recent traffic on first
                   run; subsequent polls only need ~1h but the dedup
                   is the real safety).
      page_size: int — Nomod page_size. Default 50.

    Returns:
      { ok, scanned, dedup_skipped, matched: [...], unmatched: [...] }
    each match item = { charge_id, link_id, total, currency, method,
      created, customer_id, customer_name, payer_info, payer_mismatch,
      summary, matched_via }
    each unmatched item = { charge_id, total, currency, method, created,
      payer_info, summary }
    """
    from server import (
        _normalize_phone_digits,
        _payer_mismatch,
        _waha_get,
        apply_label_transition,
        get_current_label_row,
        nomod_list_recent_charges,
    )
    import datetime as _dt
    window_hours = int(payload.get("window_hours") or 24)
    page_size = int(payload.get("page_size") or 50)

    charges, err = nomod_list_recent_charges(page_size=page_size)
    if err:
        log("poll-payments fetch err:", err)
        send(200, {"ok": False, "error": err, "scanned": 0,
                         "matched": [], "unmatched": []})
        return

    cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(
        hours=window_hours)
    matched = []
    unmatched = []
    dedup_skipped = 0
    scanned = 0

    # Cache WAHA chats once for both phone-lookup (layer 2) and
    # payer-mismatch detection. Avoids N+1 across multiple charges.
    waha_chats_cache = None
    try:
        chats, _werr = _waha_get("/api/default/chats?limit=200")
        if isinstance(chats, list):
            waha_chats_cache = chats
    except Exception as _e:
        log("poll-payments WAHA cache err:", repr(_e))

    for c in (charges or []):
        if (c.get("status") or "").lower() != "paid":
            continue
        # Window filter
        created_str = c.get("created") or ""
        try:
            created_dt = _dt.datetime.fromisoformat(
                created_str.replace("Z", "+00:00"))
        except ValueError:
            continue
        if created_dt < cutoff:
            continue
        scanned += 1
        charge_id = c.get("id") or ""
        if not charge_id:
            continue

        # Redis dedup — SET-before-notify per operator spec.
        # NX = only set if not exists; if EXISTS, this is a re-poll
        # of a charge we already handled.
        redis_key = f"nomod_seen:{charge_id}"
        _o, _e = _redis(["SET", redis_key, "1",
                         "EX", str(30 * 24 * 3600), "NX"])
        # Redis returns "OK" on success, "" (empty/null) on NX-failed.
        # Be tolerant of either return shape.
        already_seen = (not _o or "OK" not in str(_o))
        if already_seen:
            dedup_skipped += 1
            continue

        # Extract data we want regardless of match outcome.
        link_obj = c.get("link") or {}
        link_id = link_obj.get("id") if isinstance(link_obj, dict) else ""
        total = c.get("total") or "0"
        try:
            total_f = float(total)
        except ValueError:
            total_f = 0.0
        method = c.get("payment_method") or "unknown"
        currency = c.get("currency") or "AED"
        payer = c.get("customer_info") or {}
        items = c.get("items") or []
        summary = ""
        if items and isinstance(items[0], dict):
            summary = items[0].get("name", "") or ""

        # ── LAYER 1: link.id → autonomous_sends row we logged ──
        customer_id = ""
        customer_name = ""
        matched_via = ""
        log_row = None
        if link_id:
            lid_esc = link_id.replace("'", "''")
            sql = (
                "SELECT customer_id, notes::text FROM autonomous_sends "
                "WHERE kind = 'payment_link_sent' "
                f"AND notes->>'link_id' = '{lid_esc}' "
                "ORDER BY id DESC LIMIT 1"
            )
            out, _err = _psql(sql)
            row_line = (out or "").strip().splitlines()
            if row_line and "|" in row_line[0]:
                parts = row_line[0].split("|", 1)
                customer_id = parts[0].strip()
                matched_via = "link_id"
                try:
                    log_row = json.loads(parts[1].strip()) \
                        if len(parts) > 1 else None
                except (ValueError, IndexError):
                    log_row = None

        # ── LAYER 2: phone → customer_id (prefix-anchored, ambiguity-safe) ──
        # A2 (2026-06-02): the old inline match used customer_id LIKE '%<tail>@%'
        # which a hashed @lid can satisfy anywhere in the hash -> a wrong-customer
        # payment CONFIRM. Use the shared resolver, which prefix-anchors on the
        # full phone (`<digits>@%`) and returns None on an ambiguous multi-row
        # hit — so it fails toward 'unmatched', never a wrong confirm.
        if not customer_id:
            from server import resolve_customer_by_phone
            resolved, _matches = resolve_customer_by_phone(
                payer.get("phone_number"))
            if resolved:
                customer_id = resolved
                customer_name = (_matches[0].get("name", "")
                                 if _matches else "")
                matched_via = "phone_resolved"

        # ── LAYER 3: amount + time fuzzy match ──
        if not customer_id:
            # Same currency assumed (AED). Match autonomous_sends row
            # within ±30min and ±1 AED of charge.total.
            created_iso = created_dt.isoformat()
            sql = (
                "SELECT customer_id, notes::text FROM autonomous_sends "
                "WHERE kind = 'payment_link_sent' "
                f"AND ABS((notes->>'amount')::numeric - {total_f}) < 1 "
                f"AND sent_at BETWEEN "
                f"  '{created_iso}'::timestamptz - interval '30 minutes' "
                f"AND '{created_iso}'::timestamptz + interval '5 minutes' "
                "LIMIT 2"
            )
            out, _err = _psql(sql)
            lines = (out or "").strip().splitlines()
            if len(lines) == 1 and "|" in lines[0]:
                parts = lines[0].split("|", 1)
                customer_id = parts[0].strip()
                matched_via = "amount_time"

        # Get customer name from customer_facts if we have a match.
        row = None
        if customer_id and not customer_name:
            row = get_current_label_row(customer_id)
            customer_name = (row or {}).get("name", "") or ""

        # Payer-vs-customer discrepancy check. Only meaningful when we
        # matched via link_id or amount_time (phone matches already
        # confirm identity).
        pay_mismatch = False
        if customer_id and matched_via in ("link_id", "amount_time"):
            row = row or get_current_label_row(customer_id)
            pay_mismatch = _payer_mismatch(payer, row, waha_chats_cache)

        # Build payment_received audit row.
        notes_obj = {
            "charge_id": charge_id,
            "link_id": link_id,
            "matched_via": matched_via or "unmatched",
            "total": total_f,
            "currency": currency,
            "payment_method": method,
            "created_at": created_str,
            "payer_info": {
                "first_name": (payer.get("first_name") or "").strip(),
                "last_name": (payer.get("last_name") or "").strip(),
                "email": (payer.get("email") or "").strip(),
                "phone_number": (payer.get("phone_number") or "").strip(),
            },
            "payer_mismatch": pay_mismatch,
            "summary": summary,
        }
        try:
            kind = "payment_received" if customer_id else \
                "payment_received_unmatched"
            cid_for_log = customer_id or "unknown"
            sql = (
                "INSERT INTO autonomous_sends (customer_id, kind, notes) "
                f"VALUES ({_lit(cid_for_log)}, {_lit(kind)}, "
                f"{_lit(json.dumps(notes_obj))}::jsonb)"
            )
            _psql(sql)
        except Exception as e:
            log("poll-payments log insert err:", repr(e))

        entry = dict(notes_obj)
        entry["customer_id"] = customer_id
        entry["customer_name"] = customer_name

        if customer_id:
            # Promote to CONFIRMED only if the payment is a REAL
            # deposit (≥ CONFIRM_PROMOTION_MIN_AED). 1 AED tests get
            # logged + notified but don't flip the label — production
            # bug 2026-05-26: Qurbani's friend sent a 1 AED test via
            # the forwarded link → bot saw label=CONFIRMED → treated
            # him as paid → trust-killing confusion.
            try:
                from server import is_real_deposit as _is_real_deposit
                cid_e = customer_id.replace("'", "''")
                prev_row = row or get_current_label_row(customer_id)
                prev_label = (prev_row or {}).get("label") or "NEW"
                if not _is_real_deposit(total_f):
                    log(f"poll-payments cid={customer_id!r} "
                        f"BELOW deposit threshold "
                        f"AED {total_f:.2f} — logged only, "
                        f"label unchanged ({prev_label})")
                elif prev_label != "CONFIRMED":
                    apply_label_transition(
                        customer_id, prev_label, "CONFIRMED",
                        "poll-payments:payment_received",
                        f"charge {charge_id[:8]} matched_via={matched_via} "
                        f"total=AED{total_f:.0f}",
                        (prev_row or {}).get("message_count", 0),
                        created_by="system")
                    log(f"poll-payments cid={customer_id!r} "
                        f"{prev_label} -> CONFIRMED matched_via={matched_via}")
            except Exception as e:
                log("poll-payments promote err:", repr(e))
            matched.append(entry)
        else:
            unmatched.append(entry)

    # Safety net: promote any paid-but-not-CONFIRMED stragglers the live
    # webhook/poll promotion missed (merge race, dedup, payer-mismatch
    # hold, manual mislabel). Runs every poll so stuck-paid self-heals.
    reconciled = _reconcile_paid_unconfirmed()

    log(f"poll-payments scanned={scanned} matched={len(matched)} "
        f"unmatched={len(unmatched)} dedup_skipped={dedup_skipped} "
        f"reconciled={reconciled}")
    send(200, {
        "ok": True,
        "scanned": scanned,
        "dedup_skipped": dedup_skipped,
        "matched": matched,
        "unmatched": unmatched,
        "reconciled": reconciled,
    })


def _reconcile_paid_unconfirmed():
    """Safety net for stuck-paid customers: promote any CANONICAL
    customer with a real deposit (>= CONFIRM_PROMOTION_MIN_AED) on file
    but a non-CONFIRMED label → CONFIRMED. Catches what the live
    webhook/poll promotion missed (webhook/merge race, dedup, payer-
    mismatch hold, or a manual mislabel — Émilie stuck WAITING, Mohammed
    DISREGARDED despite paying AED 6426, 2026-05-29). Skips rows with a
    refund/chargeback on file. Returns count promoted."""
    from server import apply_label_transition
    from payments import CONFIRM_PROMOTION_MIN_AED
    try:
        out, err = _psql(
            "SELECT a.customer_id, cf.label FROM ("
            "  SELECT DISTINCT customer_id FROM autonomous_sends "
            "  WHERE kind='payment_received' "
            "  AND COALESCE((notes->>'total')::numeric,0) >= "
            + str(CONFIRM_PROMOTION_MIN_AED) + ") a "
            "JOIN customer_facts cf ON cf.customer_id=a.customer_id "
            "WHERE cf.label <> 'CONFIRMED' AND cf.merged_into IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM autonomous_sends r "
            "  WHERE r.customer_id=a.customer_id "
            "  AND r.kind IN ('refund','chargeback','payment_refunded'))")
    except Exception as e:
        log("reconcile paid-unconfirmed query err:", repr(e))
        return 0
    if err:
        return 0
    n = 0
    for line in (out or "").strip().splitlines():
        if not line.strip():
            continue
        parts = line.split("|")
        cid = parts[0].strip()
        prev = (parts[1].strip() if len(parts) > 1 else "") or "NEW"
        if not cid:
            continue
        try:
            apply_label_transition(
                cid, prev, "CONFIRMED", "reconcile:paid_not_confirmed",
                f"real deposit on file; label was {prev}", 0,
                created_by="system")
            log(f"reconcile cid={cid!r} {prev} -> CONFIRMED "
                f"(paid but was stuck)")
            n += 1
        except Exception as e:
            log(f"reconcile promote err cid={cid!r}: {e!r}")
    return n


# ============================================================================
# Group: label/state
# ============================================================================

def handle_label(payload, send):
    """POST /label — manual label override + record into label_corrections
    for self-improvement dampening."""
    from server import LABELS, apply_label_transition, get_current_label_row
    new_label = (payload.get("label") or "").strip().upper()
    reason = (payload.get("reason") or "").strip()
    if new_label not in LABELS:
        send(200, {"ok": False,
                         "error": f"label must be one of {sorted(LABELS)}",
                         "telegram_text": f"⚠️ Bad label: {new_label!r}.\n"
                         "Allowed: NEW, WARM, HOT, NEEDS_ATTENTION, COLD, "
                         "WAITING_FOR_PAYMENT, CONFIRMED, DISREGARDED, "
                         "PAUSED_SPAM, PAUSED_B2B, PAUSED_PERSONAL"})
        return
    cid, err = resolve_target(payload)
    if not cid:
        send(200, {"ok": False, "error": err,
                         "telegram_text": err})
        return
    try:
        row = get_current_label_row(cid)
        if row is None:
            send(200, {"ok": False,
                             "error": "customer not found",
                             "telegram_text": "⚠️ Customer not found."})
            return
        prev = row.get("label")
        cid_e = cid.replace("'", "''")
        # Read previous auto-signal + confidence for the correction row.
        out, _err = _psql(
            "SELECT COALESCE(last_analysis_signal,''), "
            "COALESCE(last_analysis_confidence::text,'') "
            "FROM conversation_state "
            f"WHERE customer_id = '{cid_e}'"
        )
        sig, conf_s = "manual_only", ""
        for line in (out or "").strip().splitlines():
            parts = line.split("|")
            if len(parts) >= 2:
                sig = parts[0].strip() or "manual_only"
                conf_s = parts[1].strip()
            break
        conf = None
        if conf_s:
            try:
                conf = float(conf_s)
            except ValueError:
                pass
        # Record the correction (drives self-improvement dampening).
        if prev and prev != new_label:
            lc_sql = (
                "INSERT INTO label_corrections "
                "(customer_id, auto_label, auto_signal, auto_confidence, "
                " manual_label, message_count) VALUES "
                f"('{cid_e}', {_lit(prev)}, {_lit(sig)}, "
                f"{'NULL' if conf is None else f'{conf:.4f}'}, "
                f"{_lit(new_label)}, {int(row.get('message_count') or 0)})"
            )
            _o, err = _psql(lc_sql)
            if err:
                log("label_corrections insert err:", err)
        # Apply transition.
        apply_label_transition(
            cid, prev, new_label, "manual:/label", reason or "operator override",
            row.get("message_count") or 0, created_by="operator")
        # Lock window for PAUSED_*.
        if new_label.startswith("PAUSED_"):
            # PAUSED_SPAM permanent; others 90 days.
            lock_expr = ("'9999-12-31'::timestamptz"
                         if new_label == "PAUSED_SPAM"
                         else "now() + interval '90 days'")
            _psql(
                f"UPDATE customer_facts SET label_locked_until = {lock_expr}, "
                f"label_locked_reason = 'manual:/label PAUSED' "
                f"WHERE customer_id = '{cid_e}'"
            )
        name = row.get("name") or cid
        send(200, {
            "ok": True, "customer_id": cid,
            "previous_label": prev,
            "label": new_label,
            "telegram_text": f"✅ {name} → *{new_label}* (manual)",
        })
    except Exception as e:
        log("label ERROR:", repr(e))
        send(200, {"ok": False, "degraded": True, "error": str(e),
                         "telegram_text": "⚠️ Label update failed."})

def handle_label_eval(payload, send):
    """POST /label-eval — silent per-message label updater + sameday
    interrupt detection. Always returns 200; fail-open."""
    from server import (
        CONFIDENCE_DEMOTE_THRESHOLD,
        _HARD_DEMOTE_SIGNALS,
        _LABEL_RANK,
        _TIER_BELOW,
        apply_label_transition,
        compute_confidence,
        compute_label,
        get_current_label_row,
        sameday_interrupt_check,
    )
    cid = (payload.get("customer_id") or "").strip()
    msg = (payload.get("latest_message") or "").strip()
    skip_if_locked = bool(payload.get("skip_if_locked", True))
    if not cid:
        send(200, {"ok": False, "error": "customer_id required"})
        return
    try:
        row = get_current_label_row(cid)
        if row is None:
            # No customer_facts row yet — surface NEW without writing.
            # customer_facts upsert happens in _customer_facts on this
            # same message; we don't race it here.
            send(200, {
                "ok": True, "customer_id": cid,
                "label": "NEW", "previous_label": None,
                "changed": False, "signal": "no_facts_row",
                "confidence": 1.0, "evidence": "",
                "interrupt_required": False, "alert_text": None,
            })
            return
        previous_label = row.get("label")

        # CONFIRMED is terminal — payment/booking won, never auto-demote
        # back to WARM/HOT/COLD on subsequent customer messages. Operator
        # can still override via `/label <name> WARM` if they need to.
        if previous_label == "CONFIRMED":
            send(200, {
                "ok": True, "customer_id": cid,
                "label": "CONFIRMED",
                "previous_label": "CONFIRMED",
                "changed": False, "signal": "confirmed_terminal",
                "confidence": 1.0,
                "evidence": "booking confirmed; auto-eval suppressed",
                "interrupt_required": False, "alert_text": None,
            })
            return

        # If locked (PAUSED via /label or /snooze), short-circuit.
        if skip_if_locked and row.get("label_locked_until"):
            cid_esc = cid.replace("'", "''")
            lock_out, _err = _psql(
                "SELECT label_locked_until > now() FROM customer_facts "
                f"WHERE customer_id = '{cid_esc}'"
            )
            if (lock_out or "").strip().startswith("t"):
                send(200, {
                    "ok": True, "customer_id": cid,
                    "label": previous_label,
                    "previous_label": previous_label,
                    "changed": False, "signal": "locked",
                    "confidence": 1.0,
                    "evidence": "label_locked_until in future",
                    "interrupt_required": False, "alert_text": None,
                })
                return

        # Compute target + confidence dampening.
        target, sig, ev = compute_label(msg, row)
        confidence = compute_confidence(sig)
        applied = target
        if confidence < CONFIDENCE_DEMOTE_THRESHOLD:
            applied = _TIER_BELOW.get(target, target)

        # STICKY-UPWARD guard. Prevents a single weak/dampened signal
        # from demoting a customer who was previously HOT (or higher)
        # all the way down. Scenario observed in production:
        #   customer was HOT (money_mentioned)
        #   next message contained only a date question
        #   compute_label returned WARM (date_asked_no_commit)
        #   dampening demoted WARM -> NEW (signal had been corrected
        #     by operator 4+ times -> confidence 0.20)
        #   transition fired HOT -> NEW (a 2-tier drop on one weak
        #     message) — wrong; the customer is still HOT, they
        #     just happened to ask a date question.
        # Fix: if the new label ranks BELOW the previous label AND
        # the signal isn't a HARD demote signal AND the confidence
        # is dampened, KEEP the previous label. Stronger signals
        # (HOT promotions, payment confirmations, past_date,
        # cold_decay, operator manual /label) still take effect.
        prev_rank = _LABEL_RANK.get(previous_label or "NEW", 0)
        new_rank = _LABEL_RANK.get(applied, 0)
        if (new_rank < prev_rank
                and sig not in _HARD_DEMOTE_SIGNALS
                and confidence < CONFIDENCE_DEMOTE_THRESHOLD):
            log(f"label-eval sticky-keep cid={cid!r} "
                f"prev={previous_label} would-apply={applied} "
                f"sig={sig} conf={confidence:.2f} — kept previous")
            applied = previous_label
            sig = "sticky_" + (previous_label or "new").lower()
            ev = (f"weak {target}/{confidence:.2f} signal not allowed "
                  f"to demote {previous_label}")

        # Sameday interrupt — only on same_day_booking signal.
        interrupt_required = False
        alert_text = None
        if sig == "same_day_booking":
            interrupt_required, alert_text = sameday_interrupt_check(
                cid, row.get("name", ""), row.get("dates", ""))

        changed = (applied != previous_label)
        if changed:
            _out, err = apply_label_transition(
                cid, previous_label, applied,
                f"auto:{sig}", ev, row.get("message_count", 0))
            if err:
                log("label_eval transition err:", err)

        # Bookkeep analysis state (fresh regardless of label change).
        update_last_analysis(cid, sig, confidence)

        log(f"label-eval cid={cid!r} {previous_label}→{applied} "
            f"signal={sig} conf={confidence:.2f} changed={changed} "
            f"interrupt={interrupt_required}")
        send(200, {
            "ok": True, "customer_id": cid,
            "label": applied,
            "previous_label": previous_label,
            "changed": changed,
            "signal": sig,
            "confidence": round(confidence, 3),
            "evidence": ev,
            "interrupt_required": interrupt_required,
            "alert_text": alert_text,
        })
    except Exception as e:
        log("label_eval ERROR:", repr(e))
        send(200, {"ok": False, "degraded": True, "error": str(e),
                         "label": None, "interrupt_required": False,
                         "alert_text": None})

def handle_snooze(payload, send):
    """POST /snooze — set label_locked_until for a customer; don't change
    the label. Duration like '4h', '2d', '30m'."""
    from server import apply_label_transition, get_current_label_row
    duration = (payload.get("duration") or "").strip().lower()
    m = re.match(r"^(\d+)([mhd])$", duration)
    if not m:
        send(200, {"ok": False,
                         "error": "duration must match \\d+[mhd] (e.g. 4h, 2d, 30m)",
                         "telegram_text": f"⚠️ Bad duration: {duration!r}.  "
                         "Use 4h / 2d / 30m."})
        return
    cid, err = resolve_target(payload)
    if not cid:
        send(200, {"ok": False, "error": err,
                         "telegram_text": err})
        return
    try:
        n_val = int(m.group(1))
        unit = m.group(2)
        interval = {"m": "minutes", "h": "hours", "d": "days"}[unit]
        cid_e = cid.replace("'", "''")
        row = get_current_label_row(cid)
        if row is None:
            send(200, {"ok": False,
                             "telegram_text": "⚠️ Customer not found."})
            return
        _psql(
            f"UPDATE customer_facts "
            f"SET label_locked_until = now() + interval '{n_val} {interval}', "
            f"    label_locked_reason = 'manual:/snooze' "
            f"WHERE customer_id = '{cid_e}'"
        )
        # Log a history row too so operator can see the snooze.
        apply_label_transition(
            cid, row.get("label"), row.get("label"),
            "manual:/snooze", f"snoozed {duration}",
            row.get("message_count") or 0, created_by="operator")
        name = row.get("name") or cid
        send(200, {
            "ok": True, "customer_id": cid,
            "duration": duration,
            "telegram_text": f"💤 {name} snoozed for {duration}.",
        })
    except Exception as e:
        log("snooze ERROR:", repr(e))
        send(200, {"ok": False, "degraded": True, "error": str(e),
                         "telegram_text": "⚠️ Snooze failed."})

# (moved to routes.py — handle_<name>(payload, self._send))

def handle_conversation_state(payload, send):
    """POST /conversation-state — silent timestamp updater."""
    from server import upsert_conversation_state
    cid = (payload.get("customer_id") or "").strip()
    event = (payload.get("event") or "").strip()
    if not cid:
        send(200, {"ok": False, "error": "customer_id required"})
        return
    if event not in ("customer_message", "operator_reply",
                     "nudge_drafted", "draft_posted"):
        send(200, {"ok": False,
                         "error": "event must be customer_message|"
                                  "operator_reply|nudge_drafted|"
                                  "draft_posted"})
        return
    try:
        _out, err = upsert_conversation_state(cid, event)
        if err:
            log("conversation_state err:", err)
            send(200, {"ok": False, "degraded": True,
                             "error": err[:200]})
            return
        send(200, {"ok": True, "customer_id": cid, "event": event})
    except Exception as e:
        log("conversation_state EXC:", repr(e))
        send(200, {"ok": False, "degraded": True, "error": str(e)})


# ============================================================================
# Group: pipeline
# ============================================================================

def handle_review(payload, send):
    """POST /review — read v_lead_summary, score, render Telegram report
    + inline keyboards. Always returns 200; fail-open.

    Auto-heal pass: customers with no name AND no yacht (broken
    first-message intake — e.g. customer first sent media) get
    refreshed from WAHA in parallel before render. Capped at
    REVIEW_INLINE_REFRESH_CAP to bound /review latency."""
    from server import (
        REVIEW_INLINE_REFRESH_CAP,
        mark_review_seen,
        read_lead_summary,
        refresh_customer_facts_from_waha,
        render_review,
        score_lead,
    )
    mode = (payload.get("mode") or "ondemand").strip()
    filter_label = (payload.get("filter") or "all").strip().lower()
    try:
        rows = read_lead_summary(
            filter_label if filter_label in ("hot", "warm", "cold") else None)

        # Auto-heal: refresh customers whose facts are likely stale or
        # missing. Two triggers:
        #   (a) ACTIVE labels with empty name AND empty yacht — the
        #       broken-intake case (webhook miss / first-msg-was-media).
        #   (b) CONFIRMED + WAITING_FOR_PAYMENT + HOT with stale facts
        #       (>30 min since last update). These can change ACTIVELY
        #       post-confirm (yacht upgrades, addon changes, last-minute
        #       date moves) and the operator needs the current booking.
        ACTIVE_LABELS = ("NEW", "WARM", "HOT", "NEEDS_ATTENTION", "COLD",
                         "WAITING_FOR_PAYMENT")
        STALE_RECHECK_LABELS = ("CONFIRMED", "WAITING_FOR_PAYMENT", "HOT")
        STALE_RECHECK_AFTER_SECS = int(
            os.environ.get("REVIEW_STALE_AFTER_SECS", "1800"))

        def _needs_refresh(r):
            lab = r.get("label") or ""
            # (a) broken-intake
            if (lab in ACTIVE_LABELS
                    and not (r.get("name") or "").strip()
                    and not (r.get("yachts") or "").strip()):
                return True
            # (b) high-value stale (use facts_updated_at proxy via
            # last_review_seen_at fallback to importance_analyzed_at)
            if lab in STALE_RECHECK_LABELS:
                stale_secs = r.get("importance_analyzed_at_seconds")
                if stale_secs is None or stale_secs > STALE_RECHECK_AFTER_SECS:
                    return True
            return False

        needs_refresh = [
            r["customer_id"] for r in rows
            if _needs_refresh(r)
        ][:REVIEW_INLINE_REFRESH_CAP]
        refreshed_count = 0
        if needs_refresh:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=5) as ex:
                results = list(ex.map(
                    refresh_customer_facts_from_waha, needs_refresh))
            refreshed_count = sum(1 for r in results if r)
            if refreshed_count:
                # Re-read summary after refresh so the render uses fresh
                # name/yachts/dates pulled from WAHA.
                rows = read_lead_summary(
                    filter_label if filter_label in
                    ("hot", "warm", "cold") else None)
                log(f"/review auto-healed {refreshed_count}/"
                    f"{len(needs_refresh)} customers")

        scored = sorted(((score_lead(r, None), r) for r in rows),
                        key=lambda t: t[0], reverse=True)
        totals = {"total": len(rows), "HOT": 0, "WARM": 0, "COLD": 0,
                  "NEW": 0, "NEEDS_ATTENTION": 0,
                  "WAITING_FOR_PAYMENT": 0, "CONFIRMED": 0,
                  "PAUSED": 0}
        for r in rows:
            lab = r.get("label") or "NEW"
            if lab.startswith("PAUSED_"):
                totals["PAUSED"] += 1
            elif lab in totals:
                totals[lab] += 1
        rendered = render_review(scored, totals, mode=mode)
        # Phase 4: monthly edit-learning digest — prepend to the report header
        # on the 1st of the month (the cron's monthly summary) or on demand
        # (payload.digest=true, used for testing).
        try:
            import datetime as _dt
            if _dt.datetime.utcnow().day == 1 or payload.get("digest"):
                from server import edit_learning_digest
                _dg = edit_learning_digest(30)
                if _dg:
                    rendered["header_text"] = _dg + "\n\n" + rendered.get("header_text", "")
                    rendered["telegram_text"] = _dg + "\n\n" + rendered.get("telegram_text", "")
        except Exception as _e:
            log("review digest err:", repr(_e))
        # Mark seen so the damping picks them up on the next /review.
        mark_review_seen(rendered.get("mark_seen_ids") or [])
        send(200, {
            "ok": True,
            "totals": totals,
            # Backward-compat: full single-message render + stacked kb.
            "telegram_text": rendered["telegram_text"],
            "inline_keyboards": rendered["inline_keyboards"],
            # Preferred: post header first, then loop per_lead_messages.
            "header_text": rendered.get("header_text", ""),
            "per_lead_messages": rendered.get("per_lead_messages", []),
        })
    except Exception as e:
        log("review ERROR:", repr(e))
        send(200, {"ok": False, "degraded": True, "error": str(e),
                         "telegram_text": "⚠️ Review failed — bridge error.",
                         "inline_keyboards": []})

def handle_info(payload, send):
    """POST /info — operator-readable single-lead DOSSIER. Returns the
    most detailed customer view we can construct without sending
    WhatsApp messages. Expanded 2026-05-27 from a thin "last 5
    customer messages" view to a full dossier (status, request,
    Hermes score+reasoning, recent quotes, paylinks, drafts,
    behavior rules, full timeline). Fail-open, always 200.

    Output is multi-block; if total exceeds Telegram's 4096-char
    limit, response also includes `telegram_chunks` (array of strings)
    so n8n can post sequential messages."""
    from server import (
        _fmt_dur,
        _humanize_signal,
        _name_fallback,
        behavioral_context,
        get_current_label_row,
        waha_fetch_history,
    )
    from util import _md_escape
    cid, err = resolve_target(payload)
    if not cid:
        send(200, {"ok": False, "error": err,
                         "telegram_text": err})
        return
    try:
        row = get_current_label_row(cid)
        if row is None:
            send(200, {
                "ok": True, "customer_id": cid,
                "telegram_text": f"🔎 No record for that customer yet.",
            })
            return
        cid_e = cid.replace("'", "''")
        # Notes — keep as-is, they're already human-authored.
        out, _err = _psql(
            "SELECT note_text, to_char(created_at,'YYYY-MM-DD') "
            "FROM customer_notes "
            f"WHERE customer_id = '{cid_e}' AND active = true "
            "ORDER BY id DESC LIMIT 3"
        )
        notes = []
        for line in (out or "").strip().splitlines():
            parts = line.split("|")
            if len(parts) >= 2:
                notes.append((parts[0].strip(), parts[1].strip()))
        # History — translate signals into human sentences.
        out, _err = _psql(
            "SELECT signal, COALESCE(evidence,''), "
            "to_char(created_at,'YYYY-MM-DD HH24:MI'), "
            "COALESCE(created_by,'system') "
            "FROM customer_label_history "
            f"WHERE customer_id = '{cid_e}' "
            "ORDER BY id DESC LIMIT 5"
        )
        timeline = []
        for line in (out or "").strip().splitlines():
            parts = line.split("|")
            if len(parts) >= 4:
                timeline.append((parts[0].strip(), parts[1].strip(),
                                 parts[2].strip(), parts[3].strip()))
        # Conversation timing.
        out, _err = _psql(
            "SELECT "
            "COALESCE(to_char(last_customer_message_at,'YYYY-MM-DD HH24:MI'),''), "
            "COALESCE(to_char(last_operator_reply_at,'YYYY-MM-DD HH24:MI'),''), "
            "EXTRACT(EPOCH FROM (now() - last_customer_message_at)) "
            "FROM conversation_state "
            f"WHERE customer_id = '{cid_e}'"
        )
        silent_secs = None
        last_cust_dt = ""
        last_op_dt = ""
        for line in (out or "").strip().splitlines():
            parts = line.split("|")
            if len(parts) >= 3:
                last_cust_dt = parts[0].strip()
                last_op_dt = parts[1].strip()
                try:
                    silent_secs = int(float(parts[2].strip()))
                except (ValueError, IndexError):
                    silent_secs = None
            break

        label = row.get("label") or "NEW"
        label_emoji = {
            "HOT": "🔥", "NEEDS_ATTENTION": "⚠️", "WARM": "♨️",
            "NEW": "🌱", "COLD": "❄️", "PAUSED_SPAM": "🚫",
            "PAUSED_B2B": "💼", "PAUSED_PERSONAL": "👤",
            "WAITING_FOR_PAYMENT": "⏳", "CONFIRMED": "✅",
            "DISREGARDED": "🛑",
        }.get(label, "•")
        # Use the last-4-digits fallback ("…4557") for unnamed customers
        # instead of "Unknown" — the operator can match the digits to the
        # WhatsApp display number when no real name has been captured.
        name = (row.get("name") or "").strip() \
            or _name_fallback(row.get("customer_id"))

        lines = [f"🔎 *{name}*"]
        # status line
        status_bits = [f"{label_emoji} *{label}*"]
        if row.get("label_locked_until"):
            lock_iso = row["label_locked_until"]
            if "9999" in lock_iso:
                status_bits.append("_locked permanently_")
            else:
                # parse to short form
                short = lock_iso[:16].replace("T", " ")
                status_bits.append(f"_locked until {short}_")
        lines.append("   " + " · ".join(status_bits))

        # facts
        facts_bits = []
        if row.get("yachts"):
            facts_bits.append(f"Looking at: *{row['yachts']}*")
        if row.get("dates"):
            facts_bits.append(f"Date: *{row['dates']}*")
        # Pull party_size too — not in label_row but on customer_facts
        ps_out, _err = _psql(
            "SELECT COALESCE(party_size,'') FROM customer_facts "
            f"WHERE customer_id = '{cid_e}'"
        )
        party_size = (ps_out or "").strip().splitlines()
        party_size = party_size[0].strip() if party_size else ""
        if party_size:
            facts_bits.append(f"Party: *{party_size}*")
        if facts_bits:
            lines.append("   📋 *Request:* " + " · ".join(facts_bits))
        else:
            lines.append("   📋 *Request:* _not captured yet_")
        lines.append(f"   {row.get('message_count', 0)} message(s) in this conversation")

        # silence + last reply
        if silent_secs is not None:
            lines.append(
                f"   ⏱ Last customer message: {_fmt_dur(silent_secs)} ago "
                f"({last_cust_dt})")
        if last_op_dt:
            lines.append(f"   ↳ Last operator reply: {last_op_dt}")

        # ━━━ Pipeline-analyze score + Hermes reasoning + suggested action
        out, _err = _psql(
            "SELECT COALESCE(importance_score,0), "
            "COALESCE(importance_reasoning,''), "
            "COALESCE(suggested_action,''), "
            "COALESCE(to_char(importance_analyzed_at,"
            "'YYYY-MM-DD HH24:MI'),'') "
            "FROM customer_facts "
            f"WHERE customer_id = '{cid_e}'"
        )
        importance_score = 0
        importance_reasoning = ""
        suggested_action = ""
        importance_at = ""
        for ln in (out or "").strip().splitlines():
            parts = ln.split("|")
            if len(parts) >= 4:
                try:
                    importance_score = int(parts[0].strip())
                except (ValueError, IndexError):
                    importance_score = 0
                importance_reasoning = parts[1].strip()
                suggested_action = parts[2].strip()
                importance_at = parts[3].strip()
                break
        if importance_score or importance_reasoning or suggested_action:
            lines.append("")
            # Honest verdict surfacing (safe-render 2026-05-30): a score of
            # 0 means the analyzer judged the lead not worth pursuing
            # (verdict 'close' — lost / spam / not converting). Flag it even
            # when the sticky label still reads HOT, so /review states the
            # facts instead of a misleading status.
            if importance_score == 0:
                lines.append("   ⚠️ *Analyzer verdict: likely LOST / not "
                             "converting* — label may be stale; see reasoning.")
            lines.append(f"   🧠 *Hermes lead-analysis*"
                         f" (score {importance_score}/100)"
                         + (f" _as of {importance_at}_" if importance_at
                            else ""))
            if importance_reasoning:
                lines.append(f"      _Reasoning:_ {_md_escape(importance_reasoning)}")
            if suggested_action:
                lines.append(f"      _Suggested:_ {_md_escape(suggested_action)}")
        else:
            # #7 safe-render: unscored lead — say so rather than showing a
            # blank HOT card (e.g. EnjoyBoat spam). Operator can use the
            # buttons to assess or close.
            lines.append("")
            lines.append("   ⚠️ _Not yet analyzed by Hermes — verdict "
                         "pending. Use the buttons to assess or close._")

        # ━━━ Recent paylinks (autonomous_sends, last 14d)
        out, _err = _psql(
            "SELECT to_char(sent_at,'YYYY-MM-DD HH24:MI'), "
            "COALESCE(detail,'') "
            "FROM autonomous_sends "
            f"WHERE customer_id = '{cid_e}' "
            "AND kind = 'payment_link_sent' "
            "AND sent_at > now() - interval '14 days' "
            "ORDER BY sent_at DESC LIMIT 5"
        )
        paylinks = []
        for ln in (out or "").strip().splitlines():
            parts = ln.split("|", 1)
            if len(parts) >= 2:
                paylinks.append((parts[0].strip(), parts[1].strip()))
        if paylinks:
            lines.append("")
            lines.append("   💳 *Payment links sent:*")
            for dt, detail in paylinks:
                # Extract just AED amount if present, drop link tokens
                shown = detail[:120] if detail else ""
                lines.append(f"      • _{dt}_ — {shown}")

        # ━━━ Recent drafts (Redis bycustomer ZSET — last 5)
        try:
            byc_out, _e = _redis(["ZREVRANGE",
                                  f"drafts:bycustomer:{cid}",
                                  "0", "4"])
            draft_ids = [x.strip() for x in (byc_out or "").splitlines()
                         if x.strip()]
            draft_lines = []
            for did in draft_ids:
                d, _ = _redis(["GET", f"draft:{did}"])
                if not d:
                    continue
                try:
                    dj = json.loads(d)
                except Exception:
                    continue
                status_ = dj.get("status", "?")
                msgs_ = dj.get("messages") or []
                body_ = (msgs_[0] if msgs_ else
                         dj.get("draft_text", ""))[:120]
                # ts portion of draft_id is ms-since-epoch
                try:
                    ts_ms = int(did.split("_", 1)[0])
                    import datetime as _dt
                    when = _dt.datetime.fromtimestamp(
                        ts_ms / 1000).strftime("%H:%M")
                except (ValueError, AttributeError):
                    when = "?"
                stale = "⚠️" if dj.get("stale_risk") else ""
                draft_lines.append(
                    f"      • [{status_}] _{when}_ {stale} "
                    f"\"{_md_escape(body_)}\"")
            if draft_lines:
                lines.append("")
                lines.append("   📝 *Recent drafts:*")
                lines.extend(draft_lines)
        except Exception as _de:
            log("info drafts err:", repr(_de))

        # ━━━ Full conversation tail — last 10 messages, BOTH sides
        # (operator wants to see Dubriani's own replies, not just
        # customer messages)
        try:
            waha = waha_fetch_history(cid, limit=30)
            hist_lines = (waha.get("history") or "").split("\n")
            tail = [ln for ln in hist_lines if ln.strip()][-10:]
            if tail:
                lines.append("")
                lines.append("   💬 *Last 10 messages (both sides):*")
                for ln in tail:
                    try:
                        if ln.startswith("Customer ") \
                                or ln.startswith("Dubriani "):
                            who = "👤" if ln.startswith("Customer ") \
                                else "🛥️"
                            ts = ln.split("(", 1)[1].split(")", 1)[0]
                            body = ln.split('"', 1)[1].rstrip('"')
                            lines.append(
                                f"      {who} _({ts})_ "
                                f"\"{_md_escape(body[:200])}\"")
                        else:
                            lines.append(f"      • {_md_escape(ln[:240])}")
                    except (IndexError, ValueError):
                        lines.append(f"      • {_md_escape(ln[:240])}")
        except Exception as _e:
            log("info WAHA history err:", repr(_e))

        # notes (per-customer)
        if notes:
            lines.append("")
            lines.append("   📌 *Per-customer notes:*")
            for note_text, dt in notes:
                lines.append(f"      • {_md_escape(note_text)}  _({dt})_")

        # ━━━ Behavior rules — global active + this customer's notes
        try:
            ctx = behavioral_context(cid) or {}
            gl = ctx.get("global") or []
            sc = ctx.get("scenario") or []
            if gl or sc:
                lines.append("")
                lines.append(f"   📐 *Active behavior rules: "
                             f"{len(gl)} global, {len(sc)} scenario*"
                             f" (in Hermes prompt)")
                # Top 5 global rules shown (rest in Hermes prompt only)
                for r in gl[:5]:
                    lines.append(f"      • {_md_escape(r[:140])}")
                if len(gl) > 5:
                    lines.append(f"      • _+ {len(gl)-5} more rules_")
        except Exception as _re:
            log("info behavioral err:", repr(_re))

        # full timeline (expanded from 5 to 10)
        out, _err = _psql(
            "SELECT signal, COALESCE(evidence,''), "
            "to_char(created_at,'YYYY-MM-DD HH24:MI'), "
            "COALESCE(created_by,'system') "
            "FROM customer_label_history "
            f"WHERE customer_id = '{cid_e}' "
            "ORDER BY id DESC LIMIT 10"
        )
        full_timeline = []
        for line in (out or "").strip().splitlines():
            parts = line.split("|")
            if len(parts) >= 4:
                full_timeline.append((parts[0].strip(), parts[1].strip(),
                                      parts[2].strip(), parts[3].strip()))
        if full_timeline:
            lines.append("")
            lines.append("   📅 *Label history (last 10):*")
            for sig, ev, dt, by in full_timeline:
                pretty = _humanize_signal(sig, ev, by) or sig
                lines.append(f"      • {_md_escape(pretty)}  _({dt})_")

        # ━━━ Identity layer — show merged_into if this is a non-canonical
        # cid (e.g., operator landed on @c.us but the canonical is @lid)
        out, _err = _psql(
            "SELECT merged_into FROM customer_facts "
            f"WHERE customer_id = '{cid_e}' AND merged_into IS NOT NULL"
        )
        merged_into = (out or "").strip().splitlines()
        if merged_into and merged_into[0].strip():
            lines.append("")
            lines.append(f"   🔗 _This customer was merged into_ "
                         f"`{merged_into[0].strip()}`")

        # Single payload — split into <4000 char chunks if too long for
        # one Telegram message (Telegram limit is 4096, leave headroom).
        full_text = "\n".join(lines)
        chunks = []
        if len(full_text) <= 4000:
            chunks = [full_text]
        else:
            buf = []
            cur_len = 0
            for line in lines:
                add = len(line) + 1
                if cur_len + add > 3800 and buf:
                    chunks.append("\n".join(buf))
                    buf = [line]
                    cur_len = add
                else:
                    buf.append(line)
                    cur_len += add
            if buf:
                chunks.append("\n".join(buf))

        # /info pagination (2026-06-02): n8n only sends telegram_text (ONE
        # message), so a dossier longer than one chunk was silently truncated
        # to the FIRST chunk — the rest of telegram_chunks was dropped. Fix
        # bridge-side (no n8n change, order-preserving): self-post the overflow
        # chunks[:-1] in order via our own Telegram, then return the LAST chunk
        # as telegram_text for n8n to send — so the operator sees
        # chunk0..chunkN in order. Best-effort: a Markdown parse error retries
        # plain so the chunk still arrives; never blocks the /info response.
        if len(chunks) > 1:
            from server import _tg_post, DEFAULT_ADMIN_CHAT
            for c in chunks[:-1]:
                try:
                    r, e = _tg_post("sendMessage", {
                        "chat_id": DEFAULT_ADMIN_CHAT, "text": c,
                        "parse_mode": "Markdown"})
                    if e or (isinstance(r, dict) and r.get("ok") is False):
                        _tg_post("sendMessage", {
                            "chat_id": DEFAULT_ADMIN_CHAT, "text": c})
                except Exception as _ce:
                    log("info chunk post err:", repr(_ce))
        send(200, {
            "ok": True, "customer_id": cid,
            "telegram_text": chunks[-1] if chunks else full_text,
            "telegram_chunks": chunks,
        })
    except Exception as e:
        log("info ERROR:", repr(e))
        send(200, {"ok": False, "degraded": True, "error": str(e),
                         "telegram_text": "⚠️ Info failed — bridge error."})

# (moved to routes.py — handle_<name>(payload, self._send))


def handle_assist(payload, send):
    """POST /assist — free-text operator assistant. Operator types a
    natural-language question/command in Telegram (no slash prefix);
    n8n routes it here. Hermes classifies the intent + extracts the
    customer name; we resolve the customer and execute.

    Body: {text: "what is the status of William?"}
    Returns: {ok, intent, customer_id?, telegram_text, action_taken}

    Supported intents:
      - status_query: "what is the status of X?" / "tell me about X"
        → returns /info-style dossier
      - draft_nudge: "draft a nudge to X" / "follow up with X"
        → returns Hermes-drafted follow-up text (operator can
          approve in Telegram before send)
      - send_paylink: "send a payment link to X for AED 5000"
        → creates Nomod link, returns URL (operator pastes manually
          OR a future flow can auto-send)
      - find_customer: "show me all paid customers this week"
        / "who's HOT right now?" → database query
      - help: anything else → suggest the supported intents
    """
    from hermes_calls import run_hermes, extract_json
    from server import (
        canonicalize_cid,
        get_current_label_row,
        get_customer_facts,
        resolve_customer_by_name,
    )
    text = (payload.get("text") or "").strip()
    if not text:
        send(200, {"ok": False, "error": "text required",
                         "telegram_text": "⚠️ I didn't catch your "
                         "message — try again."})
        return

    # ━━━ 1. Hermes intent classifier
    q = (
        "You are an operator-assistant intent classifier for "
        "Dubriani Yachts. The operator just typed a free-text "
        "message in Telegram. Classify it into exactly ONE intent "
        "and extract any parameters. Return ONLY a JSON object — "
        "no preamble, no code fences.\n\n"
        "INTENTS:\n"
        "- status_query: operator wants to know the current state "
        "of a customer (label, recent activity, what was last said).\n"
        "  Examples: 'what is the status of William?', 'tell me "
        "about Luke', 'where are we with Madawi?', 'any update "
        "on Qurbani?'\n"
        "- draft_nudge: operator wants a follow-up / message drafted "
        "for a customer.\n"
        "  Examples: 'draft a nudge to William', 'follow up with "
        "Luke about the Bliss', 'send a check-in to Madawi', "
        "'draft a nudge to Madawi and tell him we cant call now, "
        "sunday before 4 or after 8pm — which is better?'.\n"
        "  IMPORTANT: put the operator's FULL instruction of WHAT to "
        "say into `detail` verbatim — everything after the customer "
        "name (e.g. 'tell him we cant call now, sunday before 4 or "
        "after 8pm, which is better?'). Do NOT shorten it to a topic; "
        "the drafter needs the complete instruction to write the "
        "right message.\n"
        "- send_paylink: operator wants a payment link created.\n"
        "  Examples: 'send a payment link to Luke for AED 5000', "
        "'create paylink for Madawi 3500'.\n"
        "- find_customer: operator wants a list/search.\n"
        "  Examples: 'who's HOT right now?', 'show paid customers "
        "this week', 'list pending payment links'.\n"
        "- send_file: operator wants to send a media file from the "
        "library to a customer's WhatsApp.\n"
        "  Examples: 'send the drinks menu to Luke', 'send food "
        "menu to Madawi', 'send dubai harbour map to William', "
        "'forward yacht brochure to Hassan'.\n"
        "  Extract: customer_name + file_key (a short snake_case "
        "slug matching the file's name, e.g. 'drinks_menu', "
        "'food_menu', 'dubai_harbour_map', 'yacht_brochure', "
        "'satoshi_70'). Best-guess the slug; if operator says 'the "
        "menu' default to 'drinks_menu' unless 'food' is mentioned.\n"
        "- list_files: operator wants to see what files are in the "
        "library.\n"
        "  Examples: 'what files do we have?', 'list files', 'show "
        "available menus', 'what can I send?'.\n"
        "- help: anything else, or unclear.\n\n"
        "Output JSON schema:\n"
        "{\"intent\": \"status_query\"|\"draft_nudge\"|"
        "\"send_paylink\"|\"send_file\"|\"list_files\"|"
        "\"find_customer\"|\"help\","
        " \"customer_name\": \"...\" (empty if N/A),"
        " \"amount_aed\": <number or null>,"
        " \"file_key\": \"...\" (snake_case slug for send_file, "
        "else empty),"
        " \"detail\": \"...\" (for draft_nudge: the operator's FULL "
        "verbatim instruction of what to say — everything after the "
        "customer name; for find_customer: the filter; for send_file: "
        "the caption if any),"
        " \"reasoning\": \"one short sentence explaining the "
        "classification\"}\n\n"
        f"OPERATOR MESSAGE: {text!r}\n\n"
        "Return JSON only."
    )
    try:
        rc, stdout, stderr, elapsed_ms = run_hermes(q, timeout=60)
        parsed, _raw = extract_json(stdout)
        verdict = parsed or {}
        log(f"/assist hermes rc={rc} elapsed={elapsed_ms}ms "
            f"intent={verdict.get('intent', '?')!r}")
    except Exception as e:
        log(f"/assist hermes err: {e!r}")
        send(200, {"ok": False, "error": "classifier failed",
                         "telegram_text": "⚠️ I couldn't understand "
                         "that — try `/info <name>` or "
                         "`/draft <name>`."})
        return

    intent = (verdict.get("intent") or "help").lower()
    cust_name = (verdict.get("customer_name") or "").strip()
    detail = (verdict.get("detail") or "").strip()
    amount = verdict.get("amount_aed")
    file_key = (verdict.get("file_key") or "").strip()

    # ━━━ 2. Resolve customer (if name was extracted)
    cid = None
    name_matches = []
    if cust_name and intent in (
            "status_query", "draft_nudge", "send_paylink",
            "send_file"):
        cid, matches = resolve_customer_by_name(cust_name)
        name_matches = matches or []
        # Phone fallback — the operator may identify the customer by
        # NUMBER ("send X to +971521892525"). resolve_customer_by_name
        # only does a name LIKE match and can't match a phone, so try a
        # phone lookup before giving up.
        if not cid:
            _digits = re.sub(r"[^\d]", "", cust_name or "")
            if len(_digits) < 7:  # phone may be elsewhere in the text
                _m = re.search(r"[+\d][\d\s().\-]{7,}\d", text)
                if _m:
                    _digits = re.sub(r"[^\d]", "", _m.group(0))
            if len(_digits) >= 7:
                from server import resolve_customer_by_phone
                _pc, _pm = resolve_customer_by_phone(_digits)
                if _pc:
                    cid, name_matches = _pc, []
        if not cid and not name_matches:
            # If a phone number was clearly intended but no record
            # exists, guide to the explicit send paths — never fuzzy-send.
            _ph = re.search(r"[+\d][\d\s().\-]{7,}\d", cust_name or text or "")
            if _ph:
                _d = re.sub(r"[^\d]", "", _ph.group(0))
                send(200, {
                    "ok": False, "intent": intent,
                    "telegram_text": (
                        f"📵 No customer record for *+{_d}* yet.\n"
                        f"To message a new number directly:\n"
                        f"  `/send {_d}@c.us <your message>`\n"
                        f"or log them first: `/lead {_d} <details>`.")})
                return
            send(200, {
                "ok": False, "intent": intent,
                "telegram_text": (
                    f"🤔 I couldn't find a customer matching "
                    f"*{cust_name}*. Try the full name as it "
                    f"appears on /review.")
            })
            return
        if not cid and len(name_matches) > 1:
            opts = "\n".join(
                f"   • {m.get('name') or m.get('customer_id')}"
                for m in name_matches[:6])
            send(200, {
                "ok": False, "intent": intent,
                "needs_disambiguation": True,
                "matches": name_matches,
                "telegram_text": (
                    f"🔎 Several customers match *{cust_name}*. "
                    f"Be more specific:\n{opts}")
            })
            return

    # Resolve the recipient's REAL phone for operator verification.
    # @lid customer_ids are opaque hashes — without the number, a wrong
    # fuzzy-name match (resolve_customer_by_name LIKE '%name%') can send
    # to the wrong customer with nothing on the card to catch it
    # (incident 2026-05-28: nudge meant for +971502351565 went to
    # +971588404401). Showing the number makes the operator verify WHO
    # before tapping Send.
    from waha import phone_for_cid
    cust_phone = phone_for_cid(cid) if cid else ""
    if cid:
        log(f"/assist resolve intent={intent} name={cust_name!r} "
            f"-> cid={cid} phone={cust_phone or '?'}")

    # ━━━ 3. Dispatch by intent
    if intent == "status_query":
        # Reuse handle_info by calling it with a captured-send closure.
        captured = {}

        def _cap(_status, body):
            captured["body"] = body

        handle_info({"customer_id": cid}, _cap)
        body = captured.get("body", {}) or {}
        text_out = body.get("telegram_text") or "(no info)"
        chunks = body.get("telegram_chunks") or [text_out]
        send(200, {
            "ok": True, "intent": "status_query",
            "customer_id": cid,
            "telegram_text": chunks[0],
            "telegram_chunks": chunks,
            "action_taken": "Pulled customer dossier."
        })
        return

    if intent == "draft_nudge":
        # Delegate to handle_draft_followup. Hint the directive with
        # operator's `detail` so Hermes can use it as a steering note.
        captured = {}

        def _cap(_status, body):
            captured["body"] = body

        df_payload = {"customer_id": cid}
        if detail:
            df_payload["operator_hint"] = detail
        handle_draft_followup(df_payload, _cap)
        body = captured.get("body", {}) or {}
        drafted = body.get("draft_text") \
            or body.get("telegram_text") or "(empty)"
        # Persist the nudge as a real pending draft + return a card with
        # action buttons. Production bug 2026-05-28: previously /assist
        # only SHOWED the text and said "reply with send" — but no draft
        # card existed, so typing "send" looped back to /assist and did
        # nothing. Now the operator taps [✅ Send] (callback send:<id>),
        # which the normal callback flow handles via Redis lookup.
        from server import _draft_save
        import time as _t
        import random as _r
        import string as _s
        draft_id = (str(int(_t.time() * 1000)) + "_"
                    + "".join(_r.choices(_s.ascii_lowercase + _s.digits,
                                         k=5)))
        cust_name = body.get("customer_name") or ""
        draft_obj = {
            "id": draft_id,
            "customer_phone": cid,
            "customer_name": cust_name,
            "customer_message": "",
            "conversation_history": "",
            "messages": [drafted],
            "draft_text": drafted,
            "messages_sent_count": 0,
            "notes": "Operator-directed nudge via /assist",
            "status": "pending",
            "telegram_chat_id": 5532831477,
            "telegram_message_id": None,
            "is_followup": True,
            "is_lead": False,
            "is_payment": False,
            "break_condition": {"hit": False},
        }
        _draft_save(draft_obj)
        reply_markup = {"inline_keyboard": [
            [{"text": "✅ Send", "callback_data": "send:" + draft_id},
             {"text": "✏️ Edit", "callback_data": "edit:" + draft_id}],
            [{"text": "🔁 Regen", "callback_data": "regen:" + draft_id},
             {"text": "❌ Skip", "callback_data": "skip:" + draft_id}],
        ]}
        # Recipient line shows the REAL phone so the operator can verify
        # the target before [✅ Send] — see phone_for_cid rationale.
        _who = " / ".join(p for p in [cust_name, cust_phone] if p) \
            or cid
        send(200, {
            "ok": True, "intent": "draft_nudge",
            "customer_id": cid, "draft_id": draft_id,
            "telegram_text": (
                f"📝 *Nudge draft* → *{_who}*\n"
                f"⚠️ _Confirm this is the right recipient before "
                f"sending._\n\n{drafted}"),
            "reply_markup": reply_markup,
            "draft_text": drafted,
            "action_taken": "Hermes drafted a follow-up + posted card."
        })
        return

    if intent == "send_paylink":
        if not amount or amount <= 0:
            send(200, {
                "ok": False, "intent": "send_paylink",
                "telegram_text": (
                    "💳 I need an amount. Try: "
                    "*'send paylink to Luke for AED 5000'*.")
            })
            return
        # Delegate to handle_payment_link
        captured = {}

        def _cap(_status, body):
            captured["body"] = body

        pl_payload = {"customer_id": cid, "amount": float(amount),
                      "summary": detail or "Yacht charter"}
        handle_payment_link(pl_payload, _cap)
        body = captured.get("body", {}) or {}
        url = body.get("link_url") or ""
        if url:
            send(200, {
                "ok": True, "intent": "send_paylink",
                "customer_id": cid, "amount_aed": float(amount),
                "link_url": url,
                "telegram_text": (
                    f"💳 *Paylink created* — "
                    f"AED {float(amount):,.0f}\n"
                    f"👤 Recipient: *{cust_name or cid}*"
                    + (f" — *{cust_phone}*" if cust_phone else "")
                    + f"\n{url}\n\n"
                    f"⚠️ _Confirm the recipient, then reply 'send' to "
                    f"push to WhatsApp._"),
                "action_taken": "Nomod paylink created."
            })
        else:
            err_msg = body.get("error") or "unknown error"
            send(200, {
                "ok": False, "intent": "send_paylink",
                "telegram_text": (
                    f"⚠️ Paylink creation failed: {err_msg}")
            })
        return

    if intent == "send_file":
        if not file_key:
            send(200, {
                "ok": False, "intent": "send_file",
                "telegram_text": (
                    "📁 I need to know which file. Try: "
                    "*'send the drinks menu to Luke'*. "
                    "Use *'list files'* to see what's available.")
            })
            return
        # Validate against the REAL registry handle_send_file uses
        # (_load_file_registry → {key: url}). NOTE: _list_library_files
        # returns empty on the box and must NOT be used here — it would
        # reject every file. The classifier emits snake_case slugs
        # (drinks_menu) while registry keys are hyphenated
        # (satoshi-onboard-drinks), so match on an alphanumeric-
        # normalized form; fail SAFE (show available) rather than guess.
        registry = _load_file_registry() or {}

        def _norm(s):
            return "".join(ch for ch in (s or "").lower() if ch.isalnum())

        real_key = file_key if file_key in registry else None
        if not real_key:
            nq = _norm(file_key)
            cand = [k for k in registry
                    if nq and (nq in _norm(k) or _norm(k) in nq)]
            real_key = cand[0] if len(cand) == 1 else None
        if not real_key:
            avail = ", ".join(f"`{k}`" for k in list(registry)[:12]) \
                or "(empty)"
            send(200, {
                "ok": False, "intent": "send_file",
                "telegram_text": (
                    f"📁 Couldn't match `{file_key}` to a library file.\n"
                    f"Use *'list files'* to see exact names.\n\n"
                    f"Available: {avail}")})
            return
        # CONFIRMATION GATE — incident 2026-05-28: /assist used to send
        # files INSTANTLY (handle_send_file ran here with no operator
        # confirmation), so a wrong fuzzy name match hit WhatsApp
        # immediately. Now persist a pending draft carrying the file +
        # post a card showing the resolved PHONE and a [📎 Send File]
        # button. The send only fires when the operator taps it
        # (callback file:<id> → Route Action → Prep Send File).
        from server import _draft_save
        import time as _t
        import random as _r
        import string as _s
        draft_id = (str(int(_t.time() * 1000)) + "_"
                    + "".join(_r.choices(_s.ascii_lowercase + _s.digits,
                                         k=5)))
        draft_obj = {
            "id": draft_id,
            "customer_phone": cid,
            "customer_name": cust_name or "",
            "customer_message": "",
            "conversation_history": "",
            "messages": [f"(file: {real_key})"],
            "draft_text": f"(file: {real_key})",
            "messages_sent_count": 0,
            "notes": "Operator-directed file send via /assist",
            "status": "pending",
            "telegram_chat_id": 5532831477,
            "telegram_message_id": None,
            "should_send_file": True,
            "file_key": real_key,
            "file_description": detail or "",
            "is_followup": False,
            "is_lead": False,
            "is_payment": False,
            "break_condition": {"hit": False},
        }
        _draft_save(draft_obj)
        _who = " / ".join(p for p in [cust_name, cust_phone] if p) or cid
        reply_markup = {"inline_keyboard": [
            [{"text": f"📎 Send File ({real_key})",
              "callback_data": "file:" + draft_id},
             {"text": "❌ Skip", "callback_data": "skip:" + draft_id}]]}
        send(200, {
            "ok": True, "intent": "send_file",
            "customer_id": cid, "draft_id": draft_id,
            "file_key": real_key,
            "telegram_text": (
                f"📎 *Send file* `{real_key}` → *{_who}*\n"
                f"⚠️ _Confirm the recipient, then tap Send File._"
                + (f"\n_Caption:_ {detail}" if detail else "")),
            "reply_markup": reply_markup,
            "action_taken": "Awaiting operator confirmation."
        })
        return

    if intent == "list_files":
        captured = {}

        def _cap(_status, body):
            captured["body"] = body

        handle_list_files({}, _cap)
        body = captured.get("body", {}) or {}
        send(200, {
            "ok": True, "intent": "list_files",
            "telegram_text": body.get("telegram_text"),
            "file_count": body.get("count"),
            "action_taken": "Listed file library.",
        })
        return

    if intent == "find_customer":
        # Lightweight DB query — return top 10 matching customers.
        # 'detail' is the filter language ("HOT", "paid this week", etc.)
        filter_clause = ""
        d_low = detail.lower()
        if "hot" in d_low:
            filter_clause = "label = 'HOT'"
        elif "paid" in d_low or "confirmed" in d_low:
            filter_clause = "label = 'CONFIRMED'"
        elif "waiting" in d_low or "payment" in d_low:
            filter_clause = "label = 'WAITING_FOR_PAYMENT'"
        elif "warm" in d_low:
            filter_clause = "label = 'WARM'"
        elif "cold" in d_low:
            filter_clause = "label = 'COLD'"
        elif "new" in d_low:
            filter_clause = "label = 'NEW'"
        else:
            filter_clause = ("label IN ('HOT','WARM','NEEDS_ATTENTION',"
                             "'WAITING_FOR_PAYMENT')")
        out, _err = _psql(
            "SELECT customer_id, COALESCE(name,'(unnamed)'), label, "
            "COALESCE(yachts,''), COALESCE(dates,'') "
            "FROM customer_facts "
            f"WHERE {filter_clause} "
            "AND (merged_into IS NULL) "
            "ORDER BY updated_at DESC LIMIT 15"
        )
        rows = [ln.split("|") for ln in (out or "").splitlines()
                if "|" in ln]
        if not rows:
            send(200, {
                "ok": True, "intent": "find_customer",
                "telegram_text": (
                    f"🔍 No customers matched filter "
                    f"_{detail or 'active leads'}_.")
            })
            return
        lines_out = [f"🔍 *Customers matching {detail or 'active'}*"
                     f" ({len(rows)} hits):"]
        for r in rows[:15]:
            if len(r) < 5:
                continue
            nm = r[1].strip()
            lab = r[2].strip()
            y = r[3].strip()[:40]
            d = r[4].strip()[:30]
            lines_out.append(
                f"   • *{nm}* [{lab}] — "
                + (f"{y} · " if y else "")
                + (f"{d}" if d else ""))
        send(200, {
            "ok": True, "intent": "find_customer",
            "telegram_text": "\n".join(lines_out),
            "match_count": len(rows),
            "action_taken": f"Listed {len(rows)} customers."
        })
        return

    # help / fallback
    send(200, {
        "ok": True, "intent": "help",
        "telegram_text": (
            "💬 I understand these patterns:\n\n"
            "• *Status*: _'what is the status of William?'_, "
            "_'tell me about Luke'_\n"
            "• *Nudge*: _'draft a nudge to Madawi'_, "
            "_'follow up with Luke'_\n"
            "• *Paylink*: _'send paylink to Luke for AED 5000'_\n"
            "• *Search*: _'who's HOT right now?'_, "
            "_'show waiting payment'_\n\n"
            "_I'm not sure what you wanted — try one of the above "
            "phrasings._")
    })


def _format_quality_badge(qp):
    """Render a draft scorecard badge from a quality result {score, flags}
    — mirrors the main draft's 'Apply Improvement' badge so the refine/regen/
    nudge cards show the same 🟢/🟡/🔴 N/10 scorecard (operator 2026-05-31)."""
    if not isinstance(qp, dict):
        return ""
    try:
        score = max(1, min(10, round(float(qp.get("score") or 0))))
    except (TypeError, ValueError):
        return ""
    flags = qp.get("flags") if isinstance(qp.get("flags"), list) else []
    reason = ", ".join(str(f).replace("_", " ") for f in flags[:3]).strip()
    if score >= 8:
        badge = f"🟢 {score}/10"
    elif score >= 5:
        badge = f"🟡 {score}/10" + (f" — {reason}" if reason else "")
    else:
        badge = (f"🔴 {score}/10" + (f" — {reason}" if reason else "")
                 + " · tap 🔁 Regen")
    # Prepend a WhatsApp-connection warning when the WAHA session isn't WORKING,
    # so the operator never approves a draft that will silently fail to send
    # (operator 2026-05-31). Cached health check → cheap on the draft path; the
    # message is status-accurate (the watchdog only auto-restarts STOPPED).
    try:
        from waha import waha_session_ok
        ok, status = waha_session_ok()
        if not ok:
            st = status or "UNREACHABLE"
            label_map = {
                "STOPPED": ("Stopped", "the auto-watchdog is restarting it"),
                "STARTING": ("Starting", "give it a moment, then retry"),
                "SCAN_QR_CODE": ("Logged out",
                                 "re-scan the QR in WAHA to reconnect"),
                "FAILED": ("Failed", "check the WhatsApp connection"),
                "UNREACHABLE": ("Unreachable",
                                "check the WhatsApp connection first"),
            }
            disp, tail = label_map.get(
                st, (st.title(), "check the WhatsApp connection first"))
            badge = (f"⚠️ WhatsApp connection issue ({disp}) — sending may "
                     f"fail; {tail}\n" + badge)
    except Exception:
        pass
    return badge


def handle_dormancy_sweep(payload, send):
    """POST /dormancy-sweep — #5-auto graceful close (2026-06-01). Marks
    passed-date leads (signal date_passed) DISREGARDED once they've had
    >= DORMANCY_MIN_ATTEMPTS re-engage drafts AND been silent
    >= DORMANCY_MIN_SILENT_DAYS days. Reversible via /label. Re-entrant
    (Redis lock); idempotent (only transitions active labels). Posts a
    reversible summary to the operator. payload {dry_run?: bool}.
    Returns {ok, dry_run, count, leads}."""
    from labels import _is_dormancy_eligible
    min_attempts = int(os.environ.get("DORMANCY_MIN_ATTEMPTS", "2"))
    min_silent = int(os.environ.get("DORMANCY_MIN_SILENT_DAYS", "7"))
    dry = bool(payload.get("dry_run"))
    lock = "lock:dormancy_sweep"
    if not dry:
        _lk, _ = _redis(["SET", lock, "1", "NX", "EX", "600"])
        if (_lk or "").strip() != "OK":
            send(200, {"ok": True, "skipped": True,
                       "skipped_reason": "previous_sweep_running",
                       "count": 0, "leads": []})
            return
    try:
        sql = (
            "SELECT cf.customer_id, COALESCE(cf.name,''), cf.label, "
            "COALESCE(cs.reengage_attempts,0), "
            "FLOOR(EXTRACT(epoch FROM "
            "  (now()-cs.last_customer_message_at))/86400)::int "
            "FROM customer_facts cf "
            "JOIN conversation_state cs ON cs.customer_id = cf.customer_id "
            "WHERE cf.merged_into IS NULL "
            "  AND cs.last_analysis_signal = 'date_passed' "
            "  AND COALESCE(cs.reengage_attempts,0) >= " + str(min_attempts) + " "
            "  AND cs.last_customer_message_at IS NOT NULL "
            "  AND cs.last_customer_message_at < now() - interval '"
            + str(min_silent) + " days' "
            "  AND cf.label IN ('NEW','WARM','HOT','NEEDS_ATTENTION','COLD')")
        out, err = _psql(sql, timeout=20)
        if err:
            log("dormancy-sweep query err:", err)
            send(200, {"ok": False, "error": str(err),
                       "count": 0, "leads": []})
            return
        leads = []
        for line in (out or "").strip().splitlines():
            if not line.strip():
                continue
            parts = line.split("|")
            if len(parts) < 5:
                continue
            cid, nm, lab = parts[0].strip(), parts[1].strip(), parts[2].strip()
            try:
                att = int(parts[3].strip())
            except ValueError:
                att = 0
            try:
                sd = int(parts[4].strip())
            except ValueError:
                sd = 0
            # defence in depth — re-check the pure gate the SQL approximates
            if not _is_dormancy_eligible(lab, "date_passed", att, sd,
                                         min_attempts, min_silent):
                continue
            leads.append({"customer_id": cid, "name": nm, "label": lab,
                          "attempts": att, "silent_days": sd})
            if not dry:
                from server import apply_label_transition
                apply_label_transition(
                    cid, lab, "DISREGARDED", "dormancy_after_reengage",
                    f"date passed; {att} re-engage attempts; silent {sd}d",
                    0, created_by="cron-dormancy")
        if leads and not dry:
            try:
                from server import _tg_post, DEFAULT_ADMIN_CHAT
                names = ", ".join((x["name"] or x["customer_id"][:8])
                                  for x in leads[:10])
                more = f" (+{len(leads) - 10} more)" if len(leads) > 10 else ""
                _tg_post("sendMessage", {
                    "chat_id": DEFAULT_ADMIN_CHAT,
                    "text": (f"💤 Auto-closed {len(leads)} passed-date lead(s) "
                             f"after {min_attempts}+ re-engage attempts + "
                             f"{min_silent}d silence: {names}{more}. "
                             "Reversible: /label <name> WARM")})
            except Exception as e:
                log("dormancy-sweep notify err:", repr(e))
        send(200, {"ok": True, "dry_run": dry, "count": len(leads),
                   "leads": leads})
    finally:
        if not dry:
            _redis(["DEL", lock])


def handle_daily_feedback_sweep(payload, send):
    """POST /daily-feedback-sweep — #6-auto-A (2026-06-01). Finds CONFIRMED
    bookings whose trip date JUST passed (1..FEEDBACK_WINDOW_DAYS ago) and
    posts a per-lead operator card with a [Draft feedback check-in] button
    (nudge:<cid> — the existing routed callback → the #6 feedback draft).
    Per-lead Redis dedup (fbcard:<cid>, 30d) → one card per booking.
    APPROVAL-FIRST: only posts a draft-trigger card; sends nothing to the
    customer. payload {dry_run?, window_days?}. Returns {ok, dry_run, count,
    leads}."""
    import datetime as _dt
    from labels import _is_feedback_due, _parse_booking_date
    window = int(payload.get("window_days")
                 or os.environ.get("FEEDBACK_WINDOW_DAYS", "3"))
    dry = bool(payload.get("dry_run"))
    lock = "lock:feedback_sweep"
    if not dry:
        _lk, _ = _redis(["SET", lock, "1", "NX", "EX", "600"])
        if (_lk or "").strip() != "OK":
            send(200, {"ok": True, "skipped": True,
                       "skipped_reason": "previous_sweep_running",
                       "count": 0, "leads": []})
            return
    try:
        out, err = _psql(
            "SELECT customer_id, COALESCE(name,''), COALESCE(dates,'') "
            "FROM customer_facts "
            "WHERE merged_into IS NULL AND label = 'CONFIRMED' "
            "  AND COALESCE(dates,'') <> ''", timeout=20)
        if err:
            log("feedback-sweep query err:", err)
            send(200, {"ok": False, "error": str(err),
                       "count": 0, "leads": []})
            return
        today = _dt.date.today()
        leads = []
        for line in (out or "").strip().splitlines():
            if not line.strip():
                continue
            parts = line.split("|")
            if len(parts) < 3:
                continue
            cid, nm, dates = parts[0].strip(), parts[1].strip(), parts[2].strip()
            if not _is_feedback_due(_parse_booking_date(dates), today, window):
                continue
            if not dry:
                # one card per booking — 30d dedup
                seen, _ = _redis(["SET", f"fbcard:{cid}", "1",
                                  "NX", "EX", "2592000"])
                if (seen or "").strip() != "OK":
                    continue
            leads.append({"customer_id": cid, "name": nm, "dates": dates})
            if not dry:
                try:
                    from server import _tg_post, DEFAULT_ADMIN_CHAT
                    disp = nm or ("WhatsApp lead ••" + cid[-4:])
                    _tg_post("sendMessage", {
                        "chat_id": DEFAULT_ADMIN_CHAT,
                        "text": (f"\U0001f6e5️ *{disp}* — trip ({dates}) "
                                 "just wrapped. Draft a feedback check-in?"),
                        "parse_mode": "Markdown",
                        "reply_markup": {"inline_keyboard": [[
                            {"text": "\U0001f4ac Draft feedback check-in",
                             "callback_data": f"nudge:{cid}"},
                            {"text": "ℹ️ Info",
                             "callback_data": f"inf:{cid}"},
                        ]]}})
                except Exception as e:
                    log("feedback-sweep post err:", repr(e))
        send(200, {"ok": True, "dry_run": dry, "count": len(leads),
                   "leads": leads})
    finally:
        if not dry:
            _redis(["DEL", lock])


def handle_draft_followup(payload, send):
    """POST /draft-followup — generate a follow-up draft via Hermes.
    Body: {customer_id, history?, customer_name?, silence_window?, silence_hours?}.
    Reuses build_query scaffold but injects a label-specific directive.

    If caller didn't supply history (proactive sweep + [Draft nudge]
    button both omit it), pull the last 10 messages from WAHA so
    Hermes always sees the live conversation context BEFORE drafting.
    Without this, follow-up drafts ignore in-progress negotiations
    and write off-context generic upsells."""
    from server import (
        GHOST_RECOVERY_PHRASES,
        GHOST_RECOVERY_WINDOWS,
        build_query,
        extract_json,
        extract_session,
        get_current_label_row,
        run_hermes,
        sanitize_draft_messages,
        upsert_conversation_state,
        waha_fetch_history,
    )
    cid = (payload.get("customer_id") or "").strip()
    history = payload.get("history") or ""
    if not cid:
        send(200, {"ok": False, "error": "customer_id required"})
        return
    row = None
    name = ""
    try:
        # Always pull live WAHA history if the caller didn't provide one
        # (or provided a stale/short one). The customer-message path's
        # /draft already gets history via the workflow's WAHA fetch —
        # this brings /draft-followup to parity.
        waha_used = False
        waha_count = 0
        if len(history.strip()) < 50:
            waha = waha_fetch_history(cid, limit=10)
            if not waha.get("err") and waha.get("history"):
                history = waha["history"]
                waha_used = True
                waha_count = waha.get("count", 0)
        row = get_current_label_row(cid)
        label = (row or {}).get("label", "WARM") if row else "WARM"
        name = (row or {}).get("name", "") if row else ""
        # Label-specific directive — short, append to incoming_message slot
        # so the existing build_query picks it up.
        silence_window = (payload.get("silence_window") or "").strip().lower()
        silence_hours = payload.get("silence_hours")
        if silence_window in GHOST_RECOVERY_WINDOWS:
            # Proactive engine path — anchor to the verified ghost-recovery
            # phrasing for this exact window. Hard-rules directive because
            # the prior soft directive was getting ignored: production bug
            # 2026-05-25 showed Hermes returning balloon decor tips +
            # jetski offers + Zenith 64 specs as "ghost-recovery" drafts.
            phrase = GHOST_RECOVERY_PHRASES[silence_window]
            shrs = (f"{silence_hours:.1f}"
                    if isinstance(silence_hours, (int, float)) else "a while")
            directive = (
                f"This is a ghost-recovery message only. The customer has "
                f"been silent for {shrs} hours (window: {silence_window}). "
                f"Use ONLY the verified ghost-recovery phrase for this "
                f"silence window from your system prompt — specifically: "
                f"\"{phrase}\". You MAY light-touch personalize the phrase "
                f"itself (e.g. use their name if known) but the structure "
                f"and intent must stay intact.\n\n"
                f"HARD RULES — VIOLATING ANY MAKES THE DRAFT UNUSABLE:\n"
                f"- Do NOT mention add-ons, features, upsells, perks, "
                f"yacht specs, capacity, decor, jetski, balloon, or any "
                f"new information about the product.\n"
                f"- Do NOT apologize for the silence.\n"
                f"- Do NOT pitch alternatives, dates, or pricing.\n"
                f"- Do NOT ask multiple questions.\n"
                f"- Send ONE short message. Max one sentence. That is all."
            )
        else:
            # Legacy fallback — generic label-aware follow-up, used by
            # the existing [Draft nudge] button in /review (no window).
            directive_map = {
                "HOT":  ("Send a single message that picks up where "
                         "they left off, references the specific "
                         "yacht/date, and reduces friction toward "
                         "booking. ≤ 2 sentences."),
                "WARM": ("Send one helpful follow-up that adds value — "
                         "answer a likely next question, suggest a date "
                         "alternative, or share a relevant detail. Not "
                         "'just checking in'. ≤ 2 sentences."),
                "COLD": ("This lead went cold ~7+ days ago. One soft "
                         "re-engagement — reference what they were "
                         "originally interested in, mention something "
                         "genuinely new. ≤ 2 sentences."),
                "NEEDS_ATTENTION": ("Same-day or hot lead with no reply "
                                    "yet. Confirm availability or ask "
                                    "the one specific detail needed to "
                                    "lock it in. ≤ 2 sentences."),
            }
            directive = directive_map.get(label, directive_map["WARM"])
            if label == "CONFIRMED":
                # Post-booking message (#6, operator 2026-06-01). If the analyzer
                # scored this 0 the trip is DONE (no open sale) → a warm post-trip
                # FEEDBACK check-in. Otherwise it's an upcoming/active booking →
                # confirm logistics / offer an upsell.
                _isc_out, _ = _psql(
                    "SELECT COALESCE(importance_score, -1) FROM customer_facts "
                    "WHERE customer_id = " + _lit(cid) + " AND merged_into IS NULL")
                try:
                    _isc = int((_isc_out or "-1").strip().splitlines()[0])
                except (ValueError, IndexError):
                    _isc = -1
                # Trip is COMPLETED when the analyzer zeroed the score OR the
                # booking date has already passed (#6-auto-A: the daily sweep
                # cards date-passed CONFIRMED bookings before the score is
                # re-zeroed, so date-passed must also route to feedback).
                from server import _is_past_booking_date
                _trip_done = _is_past_booking_date((row or {}).get("dates") or "")
                if _isc == 0 or _trip_done:
                    # #6-auto-B: first draft on a completed booking = the
                    # feedback check-in (+ mark fbasked); after that the SAME
                    # button (relabelled "⭐ Ask for review" in /review) drafts
                    # the Google-review request. Manual — the operator taps it
                    # only after a positive reply, never auto-sentiment.
                    _fb_asked, _ = _redis(["GET", "fbasked:" + cid])
                    if (_fb_asked or "").strip():
                        from labels import _review_ask_directive
                        directive = _review_ask_directive(
                            os.environ.get("GOOGLE_REVIEW_URL", ""))
                    else:
                        directive = (
                            "This customer's booking/trip is COMPLETED. Draft a "
                            "short, warm, genuine post-trip CHECK-IN asking how "
                            "their experience was — e.g. \"hi, just checking in "
                            "— how was your time on the <yacht>?\". This is a "
                            "relationship / feedback message, NOT a sale: do NOT "
                            "pitch anything, and do NOT ask for a review in "
                            "this message. One short message.")
                        _redis(["SET", "fbasked:" + cid, "1", "EX", "5184000"])
                else:
                    directive = (
                        "This is a CONFIRMED, upcoming booking. Confirm boarding "
                        "or logistics, or offer one relevant add-on (extra hour, "
                        "catering) — warm and brief. ≤ 2 sentences.")
            # Passed-date re-engage (#5, operator 2026-06-01): a lead whose
            # booking DATE HAS PASSED (analyzer signal date_passed; renders in
            # the NO ACTIVE SALE bucket) gets a warm, low-pressure re-engagement
            # check-in for approval — never a hard-sell, never a silent
            # disregard. The operator closes it via [🛑 Disregard] only after a
            # decline/silence (reengage_attempts already tracks the attempt).
            from labels import _is_reengage_followup
            _sig_out, _ = _psql(
                "SELECT COALESCE(last_analysis_signal,'') FROM "
                "conversation_state WHERE customer_id = " + _lit(cid))
            _last_sig = ((_sig_out or "").strip().splitlines()[0].strip()
                         if (_sig_out or "").strip() else "")
            if _is_reengage_followup(_last_sig):
                directive = (
                    "This lead's booking DATE HAS ALREADY PASSED — there is no "
                    "active sale. Draft a warm, low-pressure RE-ENGAGEMENT "
                    "check-in: acknowledge the date has gone by, hope their "
                    "plans/trip worked out, and gently leave the door open for "
                    "a future booking whenever they're ready. Do NOT hard-sell, "
                    "do NOT push a specific date or price, and do NOT ask for a "
                    "deposit. Mirror their tone; one short, genuine message. "
                    "≤ 2 sentences.")
            # Owe-reply override (operator 2026-06-01): if the customer's last
            # message is UNANSWERED, this is a direct REPLY, not a proactive
            # nudge. Without this, the "proactive follow-up" framing combined
            # with an analyzer note like "response overdue, no nudge needed"
            # made Hermes return notes only (no message) → "Hermes returned no
            # draft", and the failed attempt still hid the lead from /review.
            _owe = False
            try:
                _ot, _ = _psql(
                    "SELECT (last_customer_message_at > "
                    "COALESCE(last_operator_reply_at,'epoch'::timestamptz))::text"
                    " FROM conversation_state WHERE customer_id = " + _lit(cid))
                _owe = (_ot or "").strip().lower().startswith("t")
            except Exception:
                _owe = False
            if _owe:
                directive = (
                    "DIRECT REPLY — the customer's most recent message is "
                    "UNANSWERED and they are waiting on you. Write the reply that "
                    "answers their last message directly: address their question "
                    "or request, anchor to the yacht/date/price already discussed, "
                    "and move toward booking. This is NOT a proactive nudge — you "
                    "MUST output a customer-facing message in `messages`; never "
                    "decline with 'no nudge needed' or return notes only. "
                    "≤ 3 sentences.")
            # Inject cached customer_facts so Hermes anchors on the
            # yacht/date/party-size it already knows about, instead of
            # asking the customer to repeat themselves or going generic.
            # Operator complaint: nudge drafts felt unanchored / generic
            # because Hermes only saw chat history but never the
            # extracted facts directly.
            facts_bits = []
            if (row or {}).get("yachts"):
                facts_bits.append(f"yacht: {row['yachts']}")
            if (row or {}).get("dates"):
                facts_bits.append(f"date: {row['dates']}")
            # party_size is on customer_facts but not in label_row; fetch
            ps_out, _err = _psql(
                "SELECT COALESCE(party_size,'') FROM customer_facts "
                f"WHERE customer_id = '{cid.replace(chr(39), chr(39)+chr(39))}'"
            )
            ps_val = (ps_out or "").strip().splitlines()
            if ps_val and ps_val[0].strip():
                facts_bits.append(f"party: {ps_val[0].strip()}")
            if facts_bits:
                directive += (" KNOWN FACTS to anchor on (do NOT ask the "
                              "customer to repeat these): "
                              + " · ".join(facts_bits) + ".")
            # Awaiting-reply priority: if the customer's last message is an
            # unanswered question/request, the draft must ANSWER it, not nudge.
            # Conditional clause → no-op when nothing is pending, so it is safe
            # for every follow-up draft (operator 2026-05-31).
            directive += (
                " If their most recent message contains a question or request "
                "you have not answered yet, ANSWER it directly and "
                "specifically first — do not deflect to a generic check-in.")
            # operator 2026-06-01 ("always draft"): the operator TAPPED Draft
            # reply, so ALWAYS produce a message — never notes-only. Think first,
            # then write, and put the reasoning in notes so the operator and the
            # edit-feedback loop can see WHY this reply was chosen.
            directive += (
                " ALWAYS produce the draft now: you MUST output at least one "
                "customer-facing message in `messages`. The operator explicitly "
                "asked you to draft a reply, so NEVER return notes-only, decline, "
                "or say 'no nudge needed' — even if you think now isn't ideal, "
                "give the best possible reply and note your caution. Think "
                "through the best response first, THEN write it. In "
                "`notes_for_zayn`, briefly explain WHY this is the best reply at "
                "this moment (your reasoning, and any concern the operator "
                "should know).")
        # Operator-directed override — production bug 2026-05-28:
        # "draft a nudge to Madawi and tell him we can't call now,
        # sunday before 4 or after 8pm — what's convenient?" produced
        # a generic "someone will reach out shortly" because the
        # operator's actual instruction (passed as operator_hint by
        # /assist) was never used. When present, it REPLACES the
        # generic label directive — the operator is telling us exactly
        # what to say, so say it.
        operator_hint = (payload.get("operator_hint") or "").strip()
        if operator_hint:
            directive = (
                "OPERATOR-DIRECTED MESSAGE. The operator has told you "
                "exactly what to communicate to this customer. Write the "
                "actual customer-facing WhatsApp message that delivers "
                "this, in Maria's warm lowercase voice, anchored to the "
                "conversation context above. Follow the instruction "
                "precisely — do NOT substitute a generic 'someone will "
                "reach out' or 'just checking in' message, and do NOT "
                "add unrelated upsells.\n\n"
                f"OPERATOR'S INSTRUCTION: \"{operator_hint}\"\n\n"
                "If the instruction contains a question (e.g. preferred "
                "timings), ask it naturally in the message. Keep it "
                "concise and human.")
        # Build the prompt — mirrors _draft() but with the directive
        # injected as the incoming_message context.
        tag = "OPERATOR-DIRECTED" if operator_hint \
            else f"PROACTIVE FOLLOW-UP — label={label}"
        inner = {
            "customer_id": cid,
            "customer_name": payload.get("customer_name") or name,
            "incoming_message": f"[{tag}] {directive}",
            "history": history,
            "session_id": payload.get("session_id"),
        }
        # FAST + GATED (operator 2026-06-01): the slow part was the local-Hermes
        # draft. Draft via the Anthropic cloud path and loop to >=8 feeding the
        # scorer's flags back — same engine as the main-draft gate. Replaces the
        # run_hermes draft AND the separate quality-check below. operator_hint =
        # operator-directed exact wording → override (one attempt, don't reshape).
        from server import load_system_prompt, behavioral_context
        _sys = load_system_prompt()
        try:
            _bctx = (behavioral_context(cid) or {}).get("formatted", "")
        except Exception:
            _bctx = ""
        _best = _gate_loop(
            _sys + (("\n\n" + _bctx) if _bctx else ""), history, name,
            payload.get("customer_phone") or "", inner["incoming_message"],
            cid, _sys, threshold=8, max_attempts=3,
            override=(operator_hint if operator_hint else ""))
        out, err, elapsed = "", "", 0
        _gate_score, _gate_flags = None, []
        _degraded = False
        if _best and _best.get("messages"):
            parsed = {"messages": _best["messages"],
                      "notes_for_zayn": _best.get("notes", "")}
            _gate_score, _gate_flags = _best.get("score"), (_best.get("flags") or [])
        else:
            # Anthropic gate unavailable (e.g. out of credits / 400) — fall back
            # to the local Hermes draft so the button still works (slower). The
            # button is never fully dead just because the cloud path is down.
            log(f"draft_followup gate empty cid={cid!r} — Hermes fallback")
            rc, out, err, elapsed = run_hermes(build_query(inner))
            parsed, _ = extract_json(out) if rc == 0 else ({}, "")
            if not (isinstance(parsed, dict) and parsed.get("messages")):
                # R4 (2026-06-02): gate AND local Hermes both produced nothing.
                # Don't dead-end with "no draft" — fall through to the
                # fact-anchored fallback below so the operator still gets an
                # editable, anchored placeholder (flagged degraded +
                # fallback_used), never an empty "Hermes returned no draft".
                _degraded = True
                if not isinstance(parsed, dict):
                    parsed = {}
        draft_text = ""
        notes_for_zayn = ""
        if isinstance(parsed, dict):
            notes_for_zayn = (parsed.get("notes_for_zayn") or "").strip()
        if isinstance(parsed, dict):
            # Hermes shape: {messages: ["hey Mark...", "..."]} —
            # strings, not dicts. Be tolerant of both.
            msgs = parsed.get("messages") or []
            if msgs and isinstance(msgs, list):
                from labels import _join_draft_parts
                parts = _join_draft_parts(msgs)
                # Deterministic guard against hallucinated payment URLs —
                # the system prompt forbids the LLM from pasting
                # `pay.nomodapp.com`, but it still occasionally does it,
                # and that text is operator-visible if the customer-msg
                # path or autosend ever picks it up. Strip here so the
                # nudge draft is always clean.
                parts, payment_stripped = sanitize_draft_messages(parts)
                if payment_stripped:
                    log(f"draft_followup payment-url scrubbed "
                        f"cid={cid!r}")
                draft_text = "\n".join(p for p in parts if p).strip()
            if not draft_text:
                draft_text = (parsed.get("text") or "").strip()
                # Same guard for the single-string `text` shape.
                if draft_text:
                    cleaned_one, stripped_one = sanitize_draft_messages(
                        [draft_text])
                    if stripped_one:
                        log(f"draft_followup payment-url scrubbed "
                            f"(text-shape) cid={cid!r}")
                    draft_text = cleaned_one[0] if cleaned_one else ""
        fallback_used = False
        if not draft_text:
            log(f"draft_followup empty draft — parsed_keys="
                f"{list(parsed.keys()) if isinstance(parsed, dict) else None}"
                f"  raw[:200]={(out or '')[:200]!r}")
            # R4: the drafter returned no usable message (commonly textless
            # message dicts). Build a fact-anchored placeholder from the facts
            # we already know so the operator always has an editable draft,
            # rather than "Hermes returned no draft".
            from labels import _fact_anchored_fallback
            _pf = ""
            try:
                _pfo, _ = _psql(
                    "SELECT COALESCE(party_size,'') FROM customer_facts WHERE "
                    "customer_id = " + _lit(cid) + " AND merged_into IS NULL")
                _pf = ((_pfo or "").strip().splitlines() or [""])[0].strip()
            except Exception:
                _pf = ""
            draft_text = _fact_anchored_fallback(
                name, (row or {}).get("yachts", ""),
                (row or {}).get("dates", ""), _pf)
            fallback_used = bool(draft_text)
            _degraded = True
            log(f"draft_followup R4 fallback cid={cid!r} len={len(draft_text)} "
                f"facts=yacht:{bool((row or {}).get('yachts'))},"
                f"date:{bool((row or {}).get('dates'))},party:{bool(_pf)}")
        # Mark the nudge so the report damps + reengage_attempts increments.
        try:
            upsert_conversation_state(cid, "nudge_drafted")
        except Exception as _e:
            log("draft_followup nudge_drafted err:", repr(_e))
        log(f"draft-followup cid={cid!r} label={label} "
            f"waha_used={waha_used} waha_count={waha_count} "
            f"draft_len={len(draft_text)} elapsed={elapsed}s")
        # Quality badge so the nudge card shows a scorecard (operator
        # 2026-05-31: the nudge path had no scorecard). Analysis-aware via
        # build_quality_query. Best-effort — never blocks the nudge.
        # Badge from the gate's score (already computed in the loop above) —
        # no extra Hermes call, so the button is fast.
        quality_badge = ""
        if draft_text and isinstance(_gate_score, int):
            try:
                quality_badge = _format_quality_badge(
                    {"score": _gate_score, "flags": _gate_flags})
            except Exception:
                quality_badge = ""
        if fallback_used:
            # R4: warn inline — piggybacks the badge n8n already prepends to
            # every nudge card (zero n8n change) so the operator knows this is
            # a placeholder, not a scored draft.
            quality_badge = (
                "⚠️ Auto-fallback draft — the drafter returned no message, so "
                "this is a fact-anchored placeholder. Review/edit before sending.")
            if not notes_for_zayn:
                notes_for_zayn = (
                    "Auto-fallback: the drafter returned no usable message; this "
                    "placeholder is anchored to the known yacht/date/party.")
        send(200, {
            "ok": True, "customer_id": cid, "label": label,
            "draft_text": draft_text,
            "notes_for_zayn": notes_for_zayn,
            "customer_name": name,
            "quality_badge": quality_badge,
            "fallback_used": fallback_used,
            "degraded": _degraded,
            "approval_card_header": (
                f"🔔 PROACTIVE FOLLOW-UP — {label.lower()}"),
            "session_id": extract_session(out, err),
        })
    except Exception as e:
        log("draft_followup ERROR:", repr(e))
        # R4 even on exception: never dead-end with an empty draft. A gate /
        # Hermes / db / transport error must still hand the operator an
        # editable fact-anchored placeholder, not "Hermes returned no draft".
        try:
            from labels import _fact_anchored_fallback
            _r = row or {}
            _fb = _fact_anchored_fallback(
                name or _r.get("name", ""), _r.get("yachts", ""),
                _r.get("dates", ""), _r.get("party_size", ""))
        except Exception:
            _fb = ""
        send(200, {"ok": bool(_fb), "degraded": True, "error": str(e),
                         "draft_text": _fb,
                         "label": ((row or {}).get("label") or "WARM"),
                         "fallback_used": bool(_fb),
                         "quality_badge": ("⚠️ Auto-fallback draft — an error "
                                           "occurred while drafting; "
                                           "fact-anchored placeholder, edit "
                                           "before sending." if _fb else ""),
                         "notes_for_zayn": "", "customer_name": (name or "")})

def handle_reconcile_identities(payload, send):
    """POST /reconcile-identities — merge @lid/@c.us duplicate customer
    rows using WAHA's authoritative @lid->phone resolution. Deterministic
    + reversible (sets merged_into; richer-history row stays canonical).
    Futureproofs the identity-duality fix so new splits self-heal.
    Body: {cap?: int}. Returns {ok, checked, merged, pairs}."""
    from waha import lid_to_cus
    cap = int(payload.get("cap") or 60)
    out, err = _psql(
        "SELECT customer_id || '|' || COALESCE(message_count,0) "
        "FROM customer_facts WHERE customer_id LIKE '%@lid' "
        f"AND merged_into IS NULL ORDER BY updated_at DESC LIMIT {cap}")
    if err:
        send(200, {"ok": False, "error": str(err)})
        return
    checked = merged = 0
    pairs = []
    skipped = []
    for ln in (out or "").strip().splitlines():
        parts = ln.split("|")
        if len(parts) < 2:
            continue
        lid = parts[0].strip()
        if not lid:
            continue
        try:
            lmc = int(parts[1].strip())
        except ValueError:
            lmc = 0
        checked += 1
        # WAHA is the source of truth for @lid -> @c.us. Use the
        # authoritative LID endpoint (resolves personal AND business
        # contacts; self-heals the WAHA base IP) rather than
        # /api/contacts.id, which is empty for WhatsApp Business profiles
        # and so silently skipped every business dup (fixed 2026-05-30).
        cus = lid_to_cus(lid)
        if not cus.endswith("@c.us") or cus == lid:
            continue
        crow, _e = _psql(
            "SELECT COALESCE(message_count,0) FROM customer_facts "
            f"WHERE customer_id = {_lit(cus)} AND merged_into IS NULL")
        cline = (crow or "").strip()
        if not cline:
            continue
        try:
            cmc = int(cline.splitlines()[0])
        except (ValueError, IndexError):
            cmc = 0
        # canon is chosen AFTER the safety checks below (A1: a booked/CONFIRMED
        # side must survive, not the chattier row) — see _merge_canonical_pick.
        # Identity merge-safety guard (2026-06-02 Qurbani/Royalty 136 false
        # merge): WhatsApp recycles/re-points LIDs, so the live lid->phone
        # lookup can map an old @lid row (Antonio/Bliss 55) to a DIFFERENT
        # person's @c.us (Qurbani/Royalty 136). Refuse to merge two rows that
        # carry DISTINCT real names — they are different humans. Fail-OPEN on a
        # name-fetch error (only a clear name conflict blocks).
        from labels import (_merge_blocked, _do_not_merge_pinned,
                            _merge_canonical_pick)
        # Durable name-INDEPENDENT un-merge pin (stress #4): once a pair has been
        # refused for a name conflict, a 'do_not_merge' row keeps it un-merged
        # even if a later WAHA name-refresh blanks/aligns a name (which would
        # otherwise let _merge_blocked fail open and silently re-merge them).
        pin_out, _pe = _psql(
            "SELECT customer_id || '\x1f' || COALESCE(signal,'') || '\x1f' || "
            "COALESCE(evidence,'') FROM customer_label_history "
            f"WHERE customer_id IN ({_lit(lid)}, {_lit(cus)}) "
            "AND signal = 'do_not_merge'")
        pin_rows = []
        for pl in (pin_out or "").strip().splitlines():
            pin_rows.append((pl.split("\x1f", 2) + ["", "", ""])[:3])
        if _do_not_merge_pinned(lid, cus, pin_rows):
            skipped.append(f"{lid} <-> {cus} — do_not_merge pin (held un-merged)")
            log(f"reconcile SKIP pinned: {skipped[-1]}")
            continue
        nm_out, _nme = _psql(
            "SELECT customer_id || '\x1f' || COALESCE(name,'') || '\x1f' || "
            "COALESCE(label,'') || '\x1f' || COALESCE(booked_yacht,'') "
            f"FROM customer_facts WHERE customer_id IN ({_lit(lid)}, {_lit(cus)})")
        names = {}
        facts = {}
        for nl in (nm_out or "").strip().splitlines():
            p = (nl.split("\x1f") + ["", "", "", ""])[:4]
            cidk = p[0].strip()
            if cidk:
                names[cidk] = p[1].strip()
                facts[cidk] = (p[2].strip(), p[3].strip())  # (label, booked)
        if _merge_blocked(names.get(lid, ""), names.get(cus, "")):
            # Persist a durable pin so this refusal survives future name changes
            # (stress #4). The pin-check above means this INSERT fires at most once.
            _psql(
                "INSERT INTO customer_label_history (customer_id, from_label, "
                "to_label, signal, evidence, message_count, created_at, "
                f"created_by) SELECT {_lit(lid)}, label, label, 'do_not_merge', "
                f"{_lit('recycled-LID name conflict vs ' + cus)}, "
                "COALESCE(message_count,0), now(), 'system' FROM customer_facts "
                f"WHERE customer_id = {_lit(lid)}")
            skipped.append(
                f"{lid} [{names.get(lid, '')!r}] != {cus} "
                f"[{names.get(cus, '')!r}] — name conflict, possible recycled LID")
            log(f"reconcile SKIP name-conflict (pinned): {skipped[-1]}")
            continue
        # A1: pick the survivor by booked/CONFIRMED precedence (not raw
        # message_count) so a won lead is never buried under a chattier dup.
        def _is_booked(cidk):
            lbl, bk = facts.get(cidk, ("", ""))
            return lbl == "CONFIRMED" or bool(bk)
        canon, dup = _merge_canonical_pick(
            lid, cus, lmc, cmc, _is_booked(lid), _is_booked(cus))
        _psql(
            f"UPDATE customer_facts SET merged_into = {_lit(canon)}, "
            f"updated_at = now() WHERE customer_id = {_lit(dup)} "
            "AND merged_into IS NULL")
        _psql(
            "INSERT INTO customer_label_history (customer_id, from_label, "
            "to_label, signal, evidence, message_count, created_at, "
            f"created_by) SELECT {_lit(dup)}, label, label, "
            f"'auto:identity_merge', {_lit('WAHA-resolved dup -> ' + canon)}, "
            f"COALESCE(message_count,0), now(), 'system' FROM customer_facts "
            f"WHERE customer_id = {_lit(dup)}")
        merged += 1
        pairs.append(dup + " -> " + canon)
    log(f"reconcile-identities: checked={checked} merged={merged} "
        f"skipped={len(skipped)}")
    send(200, {"ok": True, "checked": checked, "merged": merged,
                     "pairs": pairs[:25],
                     "skipped": skipped[:25], "skipped_count": len(skipped)})


def handle_pipeline_analyze(payload, send):
    """POST /pipeline-analyze — hourly cron entry point. Walks active
    leads (NEW/WARM/HOT/WAITING_FOR_PAYMENT/NEEDS_ATTENTION/COLD), runs
    hermes_analyze_lead on each, writes importance_score+reasoning+
    suggested_action to customer_facts. Bounded by PIPELINE_ANALYZE_CAP.

    Skipped outside UAE working hours unless payload.force=true.
    Returns summary: {analyzed, top_3, skipped_reason?}"""
    from server import (
        PIPELINE_ANALYZE_CAP,
        PIPELINE_ANALYZE_WORKERS,
        _has_recent_payment_link_sent,
        _is_uae_working_hours,
        _name_fallback,
        apply_label_transition,
        get_current_label_row,
        get_customer_facts,
        hermes_analyze_lead,
        refresh_customer_facts_from_waha,
        waha_fetch_history,
    )
    force = bool(payload.get("force"))
    source = (payload.get("source") or "").strip()
    reanalyze = source == "reanalyze"   # R2: drain the on-demand inbound queue
    # reanalyze runs ANYTIME (a fresh inbound at night must still refresh) and
    # uses a smaller cap to bound per-run load on the 2-vCPU box.
    cap = int(payload.get("cap")
              or (int(os.environ.get("REANALYZE_CAP", "12")) if reanalyze
                  else PIPELINE_ANALYZE_CAP))
    if not force and not reanalyze and not _is_uae_working_hours():
        send(200, {
            "ok": True, "skipped": True,
            "skipped_reason": "outside_uae_working_hours",
            "telegram_text": ""})
        return
    # Re-entrancy guard (2026-05-30). A sweep can run many minutes (each
    # lead is a serialized background Hermes call); if the hourly cron
    # fires while the previous run is still draining, sweeps stack and
    # melt the box (load-36 incident). Take an NX lock that auto-expires
    # (crash-safe) and release it in finally on normal completion.
    _ANALYZE_LOCK = "lock:pipeline_analyze"
    _lk, _ = _redis(["SET", _ANALYZE_LOCK, "1", "NX", "EX", "2400"])
    if (_lk or "").strip() != "OK":
        send(200, {
            "ok": True, "skipped": True,
            "skipped_reason": "previous_sweep_still_running",
            "telegram_text": ""})
        return
    try:
        if reanalyze:
            # R2: analyze EXACTLY the leads queued by recent inbounds, drained
            # under the shared lock + capped. Empty queue → nothing to do
            # (finally still releases the lock).
            cids = _drain_reanalyze_queue(cap)
            if not cids:
                send(200, {"ok": True, "skipped": True,
                                 "skipped_reason": "reanalyze_queue_empty",
                                 "telegram_text": ""})
                return
        else:
            # Pull leads worth scoring. CONFIRMED is included because
            # post-confirm chats actively change (yacht upgrades, addons,
            # boarding details) — operator wants Hermes' next-action
            # suggestion to reflect chat state ('addons', 'send
            # boarding pack', 'thank-you nudge') rather than the generic
            # CONFIRMED guidance. PAUSED_*/DISREGARDED stay excluded —
            # they're terminal/dormant.
            sql = (
                "SELECT customer_id FROM customer_facts WHERE label IN ("
                "'NEW','WARM','HOT','NEEDS_ATTENTION','COLD',"
                "'WAITING_FOR_PAYMENT','CONFIRMED') "
                "ORDER BY importance_analyzed_at ASC NULLS FIRST, "
                f"updated_at DESC LIMIT {int(cap)}"
            )
            out, err = _psql(sql)
            if err:
                send(200, {"ok": False, "error": str(err),
                                 "telegram_text":
                                 f"⚠️ Pipeline analyze DB error: {err}"})
                return
            cids = [ln.strip() for ln in (out or "").splitlines()
                    if ln.strip()]

        def _analyze_one(cid):
            """Per-customer pipeline: refresh facts, score, persist.
            Returns ('analyzed', score, cid, name, reasoning,
            verdict) on success, ('error', cid) on Hermes failure.
            Pure side-effects on DB so calling in parallel is safe."""
            try:
                refresh_customer_facts_from_waha(cid)
                facts_ = get_customer_facts(cid) or {}
                row_ = get_current_label_row(cid) or {}
                mc_ = int(row_.get("message_count") or
                          facts_.get("message_count") or 0)
                cs_out_, _e_ = _psql(
                    "SELECT EXTRACT(EPOCH FROM (now() - "
                    "last_customer_message_at))::int FROM "
                    "conversation_state "
                    f"WHERE customer_id = {_lit(cid)}")
                try:
                    sh_ = (int((cs_out_ or "").strip().splitlines()[0])
                           / 3600.0)
                except (ValueError, IndexError):
                    sh_ = None
                waha_ = waha_fetch_history(cid, limit=100)
                history_ = (waha_ or {}).get("history") or ""
                v_ = hermes_analyze_lead(cid, history_, facts_,
                                         message_count=mc_,
                                         silent_hours=sh_)
                if not v_:
                    return ("error", cid)
                score_ = int(v_.get("importance_score") or 0)
                reasoning_ = (v_.get("reasoning") or "").strip()
                suggested_ = (v_.get("suggested_action") or "").strip()
                verdict_ = v_.get("verdict")
                # Active-paylink override — production bug 2026-05-27:
                # operator reported Hermes recommending "nudge"/"follow
                # up" for customers we just sent a payment link to,
                # which is the opposite of useful. If we sent a paylink
                # in the last 24h, REPLACE any nudge-style suggestion
                # with a wait directive. The customer is processing the
                # link; pinging them would push them away.
                if _has_recent_payment_link_sent(cid, hours=24):
                    sa_low = suggested_.lower()
                    if any(k in sa_low for k in (
                            "nudge", "follow up", "follow-up",
                            "follow-through", "check in", "check-in",
                            "ghost", "ping", "remind", "re-engage",
                            "reach out", "send a message",
                            "send message", "drop a message")):
                        log(f"pipeline-analyze cid={cid} OVERRIDE "
                            f"suggested_action (paylink <24h): "
                            f"was={suggested_!r}")
                        suggested_ = (
                            "WAIT — paylink sent <24h ago. "
                            "Don't nudge; let the customer process "
                            "the link. Operator will see the "
                            "payment land via webhook.")
                        reasoning_ = (
                            reasoning_ +
                            " [Override: recent paylink, no nudge.]"
                        ).strip()
                _psql(
                    "UPDATE customer_facts SET "
                    f"importance_score = {score_}, "
                    f"importance_reasoning = {_lit(reasoning_)}, "
                    f"suggested_action = {_lit(suggested_)}, "
                    "importance_analyzed_at = now() "
                    f"WHERE customer_id = {_lit(cid)}")
                # #3 reconcile (2026-05-30): a 'close' verdict means the
                # analyzer judged the lead not convertible (lost / booked
                # elsewhere / spam / vendor). Demote the sticky label to
                # COLD so /review stops showing it as HOT/"push to book"
                # (operator can still 🛑 Disregard or /label it back).
                # Guard: only active RETAIL labels, never paid stages
                # (WAITING_FOR_PAYMENT/CONFIRMED), and respect snooze/locks.
                # Demote dead leads out of active labels. Fire on EITHER an
                # explicit close verdict OR importance_score==0 — the model
                # sometimes writes the correct "Rule 4: rejection" reasoning
                # + score 0 but forgets verdict='close' (Madawi 2026-05-30 sat
                # HOT despite "booked elsewhere"), and "booking date passed"
                # scores 0 without a close verdict. Score 0 = analyzer judged
                # it not worth pursuing, so it must not stay HOT/WARM/NEW.
                if verdict_ == "close" or score_ == 0:
                    prev_lbl_ = (row_.get("label") or "").strip()
                    if prev_lbl_ in ("NEW", "WARM", "HOT", "NEEDS_ATTENTION"):
                        # B-fix (2026-06-02 Tal Sudai cash-booking): never auto-
                        # COLD a lead that was EVER booked/paid, nor kill one on
                        # an UNVERIFIABLE 'passed date' (dates='today' was
                        # misread as 6+ days past -> NEW->COLD). ever_booked =
                        # history ever reached a paid/booked state.
                        from labels import (_demote_to_cold_blocked,
                                            _manual_override_protects)
                        eb_, _ebe = _psql(
                            "SELECT 1 FROM customer_label_history WHERE "
                            f"customer_id = {_lit(cid)} AND to_label IN "
                            "('CONFIRMED','WAITING_FOR_PAYMENT') LIMIT 1")
                        ever_booked_ = bool((eb_ or "").strip())
                        # #13 (stress #11): never auto-undo a RECENT manual
                        # operator override (the manual HOT reverts) — respect
                        # the human decision until it ages out.
                        mo_, _moe = _psql(
                            "SELECT COALESCE(signal,'') || '\x1f' || "
                            "EXTRACT(EPOCH FROM (now() - created_at))/86400.0 "
                            "FROM customer_label_history WHERE customer_id = "
                            f"{_lit(cid)} AND signal LIKE 'manual:%' "
                            "ORDER BY created_at DESC LIMIT 1")
                        mo_sig, mo_age = "", None
                        _mol = (mo_ or "").strip().splitlines()
                        if _mol:
                            _pp = _mol[0].split("\x1f", 1)
                            mo_sig = _pp[0].strip()
                            if len(_pp) > 1:
                                try:
                                    mo_age = float(_pp[1].strip())
                                except ValueError:
                                    mo_age = None
                        if _manual_override_protects(mo_sig, mo_age):
                            log("pipeline-analyze demote BLOCKED "
                                f"cid={cid} (recent manual override "
                                f"{mo_sig!r})")
                        elif _demote_to_cold_blocked(
                                facts_.get("dates"), reasoning_, ever_booked_):
                            log("pipeline-analyze demote BLOCKED "
                                f"cid={cid} (booked/unverifiable-passed; "
                                f"dates={facts_.get('dates')!r})")
                        else:
                            lk_, _lke = _psql(
                                "SELECT label_locked_until > now() FROM "
                                "customer_facts WHERE customer_id = "
                                f"{_lit(cid)}")
                            if not (lk_ or "").strip().startswith("t"):
                                try:
                                    _sig = ("auto:analyzer_close"
                                            if verdict_ == "close"
                                            else "auto:analyzer_score0")
                                    apply_label_transition(
                                        cid, prev_lbl_, "COLD", _sig,
                                        reasoning_ or
                                        "analyzer: not convertible",
                                        mc_, created_by="system")
                                except Exception as _le:
                                    log("pipeline-analyze demote err "
                                        f"cid={cid}: {_le!r}")
                return ("analyzed", score_, cid,
                        facts_.get("name") or _name_fallback(cid),
                        reasoning_, verdict_)
            except Exception as ex_:
                log(f"pipeline_analyze worker err cid={cid!r}: {ex_!r}")
                return ("error", cid)

        from concurrent.futures import ThreadPoolExecutor
        analyzed = 0
        errors = 0
        closed = []
        top = []  # (score, cid, name, reasoning)
        with ThreadPoolExecutor(
                max_workers=PIPELINE_ANALYZE_WORKERS) as pool:
            for result in pool.map(_analyze_one, cids):
                if result[0] == "error":
                    errors += 1
                    continue
                _, sc, c, nm_, rea, ver = result
                analyzed += 1
                if ver == "close":
                    closed.append((c, nm_, rea))
                else:
                    top.append((sc, c, nm_, rea))
        # Dedup the not-convertible cards: ONE per customer (bug 4a, 2026-06-02).
        # Canonicalize so already-merged @lid/@c.us splits collapse and the
        # Disregard button / closecard key act on the canonical row; then a
        # name-based pass catches UNMERGED splits (e.g. "Noha" under two ids,
        # both merged_into NULL) that canonicalize_cid can't yet unify.
        try:
            from server import canonicalize_cid
            from labels import _dedup_leads
            closed = _dedup_leads([(canonicalize_cid(c), nm_, r)
                                   for (c, nm_, r) in closed])
        except Exception as _de:
            log(f"pipeline-analyze closed-dedup err: {_de!r}")
        top.sort(key=lambda t: t[0], reverse=True)
        top3 = top[:3]
        # Silent-by-default policy. The operator's mental model is ONE
        # pipeline command: /review. The cron is the engine, not a
        # second feature — it just updates importance scores in the
        # background. We ONLY ping Telegram when Hermes flagged
        # close-candidates the operator should review (actionable),
        # otherwise leave telegram_text empty so the cron's
        # 'If has_summary?' gate short-circuits and stays quiet.
        telegram_text = ""
        if closed:
            # Operator 2026-05-31: post ONE card per flagged lead, each with
            # its own [🛑 Disregard] [ℹ️ Info] buttons (callback_data matches
            # the /review cards, so n8n routes them) — so the operator can act
            # straight from the alert instead of hunting the /review card.
            # Per-lead Redis dedup (closecard:<cid>, 20h) so the hourly sweep
            # doesn't re-post the same COLD leads every hour. Posted directly
            # via _tg_post; telegram_text stays "" so the cron doesn't double-post.
            try:
                from server import _tg_post, DEFAULT_ADMIN_CHAT
                fresh = []
                for c, nm, r in closed:
                    seen, _se = _redis(
                        ["SET", f"closecard:{c}", "1", "NX", "EX", "72000"])
                    if (seen or "").strip() == "OK":
                        fresh.append((c, nm, r))
                if fresh:
                    _tg_post("sendMessage", {
                        "chat_id": DEFAULT_ADMIN_CHAT,
                        "text": (f"🛑 Hermes flagged {len(fresh)} lead(s) as "
                                 "not-convertible — tap Disregard to close, or "
                                 "override with /label <name> WARM"),
                    })
                    for c, nm, r in fresh:
                        _tg_post("sendMessage", {
                            "chat_id": DEFAULT_ADMIN_CHAT,
                            "text": f"🛑 {nm}\n{r}",
                            "reply_markup": {"inline_keyboard": [[
                                {"text": "🛑 Disregard",
                                 "callback_data": f"disregard:{c}"},
                                {"text": "ℹ️ Info",
                                 "callback_data": f"inf:{c}"},
                            ]]},
                        })
                    log(f"pipeline-analyze posted {len(fresh)} disregard "
                        f"card(s) of {len(closed)} closed")
            except Exception as _ce:
                # Never lose the alert — fall back to a single text message.
                log("pipeline-analyze close-card err:", repr(_ce))
                close_lines = [
                    f"🛑 Hermes flagged {len(closed)} lead(s) as "
                    "not-convertible — Disregard on /review or /label "
                    "<name> WARM.", ""]
                for cid, nm, r in closed[:5]:
                    close_lines.append(f"  • {nm} — {r}")
                if len(closed) > 5:
                    close_lines.append(
                        f"  …and {len(closed) - 5} more — see /review")
                telegram_text = "\n".join(close_lines)
        send(200, {
            "ok": True, "analyzed": analyzed, "errors": errors,
            "close_recommended": len(closed),
            "top_3": [{"customer_id": c, "name": nm,
                       "importance_score": s, "reasoning": r}
                      for s, c, nm, r in top3],
            "telegram_text": telegram_text,
        })
    except Exception as e:
        log("pipeline_analyze ERROR:", repr(e))
        send(200, {"ok": False, "degraded": True,
                         "telegram_text":
                         f"⚠️ Pipeline analyze error: {e}"})
    finally:
        _redis(["DEL", _ANALYZE_LOCK])

# (moved to routes.py — module-level resolve_target / update_last_analysis)

def handle_lead_analyze_disregard(payload, send):
    """POST /lead-analyze-disregard — operator pressed [🛑 Disregard] on a
    /review card. Two modes:

      - default: pulls full WAHA history, calls hermes_analyze_lead. If
        verdict='close' → flips label to DISREGARDED. If verdict=
        'keep_open' → returns reasoning + suggested_action AND a
        [🛑 Close anyway] / [💬 Draft nudge] reply_markup so the
        operator can override Hermes either direction without re-typing
        commands.

      - payload.force=true: skips Hermes, force-flips to DISREGARDED.
        Used by the [🛑 Close anyway] override button from the
        keep_open response above.

    Hermes failure → returns degraded, never auto-closes."""
    from server import (
        _md_escape,
        _name_fallback,
        apply_label_transition,
        get_current_label_row,
        get_customer_facts,
        hermes_analyze_lead,
        waha_fetch_history,
    )
    cid, err = resolve_target(payload)
    if not cid:
        send(200, {"ok": False, "telegram_text": err or
                         "⚠️ customer_id required"})
        return
    force_close = bool(payload.get("force"))
    explicit_force = force_close  # F6: distinguish a true Force tap (Hermes WAS
    #   asked + said keep-open) from the terminal-label 1-tap close below.
    try:
        row = get_current_label_row(cid) or {}
        cur_label = row.get("label") or "NEW"
        facts = get_customer_facts(cid) or {}
        nm = facts.get("name") or _name_fallback(cid)
        mc = int(row.get("message_count") or facts.get("message_count")
                 or 0)

        # Operator's explicit Disregard / 🗂 Close-chat tap is authoritative on
        # an already-terminal lead — a COLD dead lead, or a CONFIRMED won
        # booking being closed — so it closes in ONE tap instead of asking
        # Hermes for permission (which left COLD leads un-closed, 2026-06-02).
        # Active HOT/WARM/NEW leads still get the Hermes safety analysis below.
        if cur_label in ("COLD", "CONFIRMED"):
            force_close = True

        # ---- force-override path — skip Hermes, just close ----
        if force_close:
            prev_reasoning = facts.get("disregard_reasoning") or ""
            apply_label_transition(
                cid, cur_label, "DISREGARDED",
                signal="operator_override",
                evidence=(
                    ("operator override — Hermes had said keep_open. "
                     if explicit_force else
                     f"operator closed terminal {cur_label} lead in one tap. ")
                    + f"Prior reasoning: {prev_reasoning[:180]}"),
                message_count=mc,
                created_by="operator:disregard_force")
            _psql(
                "UPDATE customer_facts SET "
                "disregard_verdict = 'close', "
                "disregard_analyzed_at = now() "
                f"WHERE customer_id = {_lit(cid)}")
            # Escape dynamic name — customer pushNames can contain '_'
            # or '*' which would break Markdown parse on Telegram.
            nm_e = _md_escape(nm)
            _ov = ("_Operator override_ — closed despite Hermes saying "
                   "'keep open'."
                   if explicit_force else
                   f"_Closed by operator_ — {cur_label} lead, already "
                   "terminal (Hermes not consulted).")
            tx = (
                f"🛑 *DISREGARDED* — {nm_e}\n"
                f"{_ov}\n\n"
                f"→ label {cur_label} → DISREGARDED. Hidden from "
                f"/review.\n_Undo with_ `/label {nm_e} WARM`")
            send(200, {
                "ok": True, "verdict": "close",
                "label_before": cur_label, "label_after": "DISREGARDED",
                "reasoning": "operator override",
                "telegram_text": tx})
            return

        # ---- default path — Hermes-analyze first ----
        silent_h = None
        cs_out, _e = _psql(
            "SELECT EXTRACT(EPOCH FROM (now() - "
            "last_customer_message_at))::int FROM conversation_state "
            f"WHERE customer_id = {_lit(cid)}")
        try:
            silent_h = (int((cs_out or "").strip().splitlines()[0])
                        / 3600.0)
        except (ValueError, IndexError):
            silent_h = None

        waha = waha_fetch_history(cid, limit=100)
        history = (waha or {}).get("history") or ""

        verdict_obj = hermes_analyze_lead(
            cid, history, facts, message_count=mc,
            silent_hours=silent_h)
        if not verdict_obj:
            send(200, {
                "ok": False, "degraded": True,
                "telegram_text": (
                    "⚠️ Disregard analysis failed — Hermes timeout/error. "
                    "No label change. Try again or close manually via "
                    f"`/label {nm} DISREGARDED`.")})
            return

        verdict = verdict_obj["verdict"]
        reasoning = (verdict_obj.get("reasoning") or "").strip()
        suggested = (verdict_obj.get("suggested_action") or "").strip()
        score = verdict_obj.get("importance_score", 0)

        # Persist analysis snapshot regardless of verdict (drives future
        # /info display and avoids re-spending Hermes on same chat).
        upd = (
            "UPDATE customer_facts SET "
            f"disregard_verdict = {_lit(verdict)}, "
            f"disregard_reasoning = {_lit(reasoning)}, "
            "disregard_analyzed_at = now(), "
            f"importance_score = {int(score)}, "
            f"importance_reasoning = {_lit(reasoning)}, "
            f"suggested_action = {_lit(suggested)}, "
            "importance_analyzed_at = now() "
            f"WHERE customer_id = {_lit(cid)}"
        )
        _psql(upd)

        # Escape ANY Hermes/customer text going into Markdown. Hermes
        # reasoning can contain `code_fences`, *bold-like* phrases,
        # snake_case identifiers, or [bracketed] notes — all of which
        # open entity spans Telegram can't close → '400 can't parse
        # entities'.
        nm_e = _md_escape(nm)
        reasoning_e = _md_escape(reasoning or "(no reasoning)")
        suggested_e = _md_escape(suggested) if suggested else ""

        if verdict == "close":
            # Auto-close. apply_label_transition writes the audit row.
            apply_label_transition(
                cid, cur_label, "DISREGARDED",
                signal="hermes_disregard",
                evidence=(reasoning or "")[:240],
                message_count=mc,
                created_by="operator:disregard_button")
            tx = (
                f"🛑 *DISREGARDED* — {nm_e}\n"
                f"_Hermes analysis:_ {reasoning_e}"
                f"\n\n→ label flipped {cur_label} → DISREGARDED. "
                "Hidden from /review.\n"
                f"_Undo with_ `/label {nm_e} WARM`")
            send(200, {
                "ok": True, "verdict": "close",
                "label_before": cur_label, "label_after": "DISREGARDED",
                "reasoning": reasoning, "telegram_text": tx})
        else:
            tx = (
                f"🟢 *KEEP OPEN* — {nm_e}\n"
                f"_Hermes analysis:_ {reasoning_e}\n"
                f"_Importance score:_ {int(score)}/100"
                + (f"\n_Suggested play:_ {suggested_e}"
                   if suggested else "")
                + f"\n\n→ label unchanged ({cur_label})."
                "\n\n_Disagree? Override below._"
            )
            # Override buttons. The operator can either force-close
            # (against Hermes) or accept Hermes' advice by drafting a
            # nudge immediately. Both stay one tap away.
            reply_markup = {
                "inline_keyboard": [[
                    {"text": "🛑 Close anyway",
                     "callback_data": f"disregard_force:{cid}"},
                    {"text": "💬 Draft nudge",
                     "callback_data": f"nudge:{cid}"},
                ]]
            }
            send(200, {
                "ok": True, "verdict": "keep_open",
                "label_before": cur_label, "label_after": cur_label,
                "importance_score": int(score),
                "reasoning": reasoning,
                "suggested_action": suggested,
                "telegram_text": tx,
                "reply_markup": reply_markup})
    except Exception as e:
        log("lead_analyze_disregard ERROR:", repr(e))
        send(200, {"ok": False, "degraded": True,
                         "telegram_text":
                         f"⚠️ Disregard endpoint error: {e}"})


# ============================================================================
# Group: caps/mode
# ============================================================================

def handle_set_mode(payload, send):
    from server import manual_killswitch, set_mode
    cid = (payload.get("customer_id") or "").strip()
    mode = (payload.get("mode") or "").strip().lower()
    by = (payload.get("activated_by") or "operator").strip()
    break_reason = (payload.get("break_reason") or "").strip() or None
    if not cid:
        send(400, {"ok": False, "error": "customer_id is required"})
        return
    if cid == "__ALL__":  # /manual kill switch
        ok, err = manual_killswitch()
        if not ok:
            log("manual killswitch failed:", err)
            send(502, {"ok": False, "error": str(err)})
            return
        log("MANUAL KILL SWITCH — all conversations -> approval")
        send(200, {"ok": True, "message": "all conversations set to approval"})
        return
    if cid == "__ALL_AUTO__":  # /auto all confirm — enable autonomous for all
        from server import set_all_autonomous
        n, e2 = set_all_autonomous()
        if n is None:
            log("set_all_autonomous failed:", e2)
            send(502, {"ok": False, "error": str(e2)})
            return
        # Mode flip ONLY — the synchronous 8/10 floor + caps are untouched and
        # still gate every auto-send.
        log(f"AUTO ALL — {n} conversations -> autonomous (floor + caps unchanged)")
        send(200, {"ok": True, "count": n,
                   "message": (f"{n} conversations set to autonomous — the 8/10 "
                               "quality floor and caps still gate every send.")})
        return
    m, err = set_mode(cid, mode, by, break_reason)
    if m is None:
        log("set-mode failed:", err)
        send(502, {"ok": False, "error": str(err)})
        return
    log(f"conversation mode set: {cid} -> {m} (by {by})")
    send(200, {"ok": True, "customer_id": cid, "mode": m})

def handle_autosend_check(payload, send):
    """FR-4: mode + caps decision for an autonomous draft.

    The autonomous branch calls this TWICE per draft:
      - Auto Gate   (commit=false): only needs to know the conversation
        is still autonomous, to decide whether to start the countdown.
      - Auto Commit (commit=true): the final decision — caps are
        evaluated here, exactly ONCE per draft, so the QC sample is
        rolled once and the checkpoint event is logged once. Also logs
        the auto-send (advances the cap counters).

    Caps are deliberately NOT evaluated on the gate probe: evaluate_caps()
    rolls the QC sample and logs the checkpoint event, so evaluating on
    both calls would roll QC twice and double-count the checkpoint."""
    from server import (evaluate_caps, get_mode, log_autosend,
                        AUTOSEND_MIN_SCORE, build_quality_query,
                        _draft_latest_for_customer, canonicalize_cid)
    from labels import _quality_floor_ok, _is_handoff_message
    cid = (payload.get("customer_id") or "").strip()
    if not cid:
        send(400, {"ok": False, "error": "customer_id is required"})
        return
    # #10 (stress #1): drafts, modes and cap counters are keyed under the
    # CANONICAL cid (customer_facts.merged_into) — _draft_save canonicalizes on
    # write — but n8n passes the raw inbound cid. Canonicalize here so the
    # floor's draft-fetch + get_mode + evaluate_caps + log_autosend all align for
    # a freshly-merged customer (otherwise the pending draft isn't found ->
    # fail-closed block, and the caps split across the @lid/@c.us identities).
    cid = canonicalize_cid(cid)
    commit = bool(payload.get("commit"))
    mode = get_mode(cid)
    if mode != "autonomous":
        send(200, {"ok": True, "mode": mode, "auto_send": False,
                         "reason": "conversation is not in autonomous mode"})
        return
    if not commit:
        # Gate probe — mode only. The workflow (Auto Is Autonomous) reads
        # just `mode` from this response; caps are the commit call's job.
        log(f"autosend-check GATE customer={cid} mode=autonomous "
            "(caps deferred to commit)")
        send(200, {"ok": True, "mode": mode, "auto_send": True,
                         "reason": "autonomous — caps evaluated at commit"})
        return
    # SYNCHRONOUS QUALITY FLOOR (2026-06-02) — an autonomous draft auto-sends
    # ONLY if it scores >= AUTOSEND_MIN_SCORE. Enforced HERE, before caps and
    # before the send. FAIL-CLOSED: a missing draft or any scoring error routes
    # to approval (auto_send=False) — it NEVER auto-sends an unscored/low draft.
    _draft = payload.get("current_draft")
    if isinstance(_draft, list):
        _draft = "\n\n".join(str(m) for m in _draft)
    _draft = str(_draft or "").strip()
    _inc = payload.get("incoming_message") or ""
    _hist = payload.get("history") or ""
    _nm = payload.get("customer_name") or ""
    # The live n8n Auto Commit doesn't pass the draft — fetch the customer's
    # latest PENDING draft (the one being auto-sent) and score THAT. Bridge-only,
    # so the floor functions without any n8n change.
    if not _draft:
        try:
            _d, _ = _draft_latest_for_customer(cid, "pending")
        except Exception:
            _d = None
        if _d:
            _draft = (str(_d.get("draft_text") or "").strip()
                      or "\n\n".join(str(m) for m in (_d.get("messages") or [])
                                     if isinstance(m, str)).strip())
            _inc = _inc or (_d.get("customer_message") or "")
            _hist = _hist or (_d.get("conversation_history") or "")
            _nm = _nm or (_d.get("customer_name") or "")
    if not _draft:
        log(f"autosend-check FLOOR-BLOCK customer={cid} no draft to score")
        send(200, {"ok": True, "mode": mode, "auto_send": False, "score": None,
                   "reason": "quality_floor: no draft to score — routed for approval"})
        return
    # HANDOFF EXEMPTION (2026-06-02): the conservative handoff/holding line is a
    # SAFE non-answer the operator explicitly allows to auto-send even below the
    # floor. Exact-match only (labels._is_handoff_message) so it can never widen
    # into a bypass for a real answer. Caps still apply.
    if _is_handoff_message(_draft):
        ok, reason = evaluate_caps(cid)
        if ok:
            log_autosend(cid, "auto")
        log(f"autosend-check HANDOFF-ALLOW customer={cid} auto_send={ok} "
            f"reason={reason!r}")
        send(200, {"ok": True, "mode": mode, "auto_send": ok,
                   "score": "handoff", "reason": reason})
        return
    try:
        _score, _flags, _summary = _anthropic_score(build_quality_query({
            "system_prompt": payload.get("system_prompt") or "",
            "customer_name": _nm,
            "history": _hist,
            "incoming_message": _inc,
            "current_draft": _draft,
            "customer_id": cid,
        }))
    except Exception as _se:
        log(f"autosend-check FLOOR score error customer={cid}: {_se!r}")
        _score = 0
    if not _quality_floor_ok(_score, AUTOSEND_MIN_SCORE):
        log(f"autosend-check FLOOR-BLOCK customer={cid} score={_score} "
            f"< {AUTOSEND_MIN_SCORE} -> approval")
        send(200, {"ok": True, "mode": mode, "auto_send": False, "score": _score,
                   "reason": (f"quality_floor: scored {_score}/10 < "
                              f"{AUTOSEND_MIN_SCORE} — routed for approval")})
        return
    ok, reason = evaluate_caps(cid)
    if ok:
        log_autosend(cid, "auto")
    log(f"autosend-check COMMIT customer={cid} mode=autonomous score={_score} "
        f"auto_send={ok} reason={reason!r}")
    send(200, {"ok": True, "mode": mode, "auto_send": ok, "score": _score,
                     "reason": reason})

def handle_autosend_state(payload, send):
    """BUG-1 fix: Redis-backed autonomous-send state. action = arm |
    disarm | get, keyed autosend:<draft_id>. The FR-4 autonomous branch
    uses this instead of n8n staticData, which is unreliable across the
    Auto Wait countdown."""
    from server import AUTOSEND_TTL
    action = (payload.get("action") or "").strip().lower()
    did = (payload.get("draft_id") or "").strip()

    # NEW: disarm_by_customer — used when a new customer message arrives
    # to cancel any in-flight Auto Wait timers for that customer. Without
    # this, the original execution wakes up from Auto Wait, reads
    # armed=true, and sends the (now-stale) draft AFTER the customer has
    # already moved on with a new message. Operator-visible bug:
    # "draft was created and AUTO was enabled to send to customer a
    # response, then customer sent another message, now another draft
    # was created, planning a second response. which would be ugly."
    if action == "disarm_by_customer":
        cid = (payload.get("customer_id") or "").strip()
        if not cid:
            send(400, {"ok": False,
                             "error": "customer_id is required"})
            return
        # SCAN autosend:* keys, GET each, match by customer_phone,
        # DEL matches. The pendingQueue→Redis migration only landed
        # Phases 1-3 (drafts:bycustomer is partial), so the autosend
        # state is the canonical source of truth for in-flight Auto
        # Wait timers. SCAN is O(N) but at typical load there are
        # 0-5 armed autosends.
        cursor = "0"
        scanned = 0
        disarmed = []
        iterations = 0
        while True:
            iterations += 1
            if iterations > 64:  # safety cap
                break
            scan_out, err = _redis(["SCAN", cursor, "MATCH",
                                    "autosend:*", "COUNT", "100"])
            if err:
                log("disarm_by_customer SCAN failed:", err)
                break
            lines = [l for l in (scan_out or "").splitlines()
                     if l.strip()]
            if not lines:
                break
            cursor = lines[0].strip()
            keys = lines[1:]
            for k in keys:
                scanned += 1
                v_out, _e = _redis(["GET", k])
                raw = (v_out or "").strip()
                if not raw:
                    continue
                try:
                    data = json.loads(raw)
                except Exception:
                    continue
                if not isinstance(data, dict):
                    continue
                if str(data.get("customer_phone", "")) != cid:
                    continue
                _redis(["DEL", k])
                d_id = k.split(":", 1)[1] if ":" in k else k
                disarmed.append(d_id)
            if cursor == "0":
                break
        log(f"autosend-state DISARM_BY_CUSTOMER cid={cid} "
            f"scanned={scanned} disarmed={len(disarmed)} "
            f"ids={disarmed}")
        send(200, {"ok": True, "action": "disarm_by_customer",
                         "customer_id": cid,
                         "scanned": scanned,
                         "disarmed": disarmed,
                         "count": len(disarmed)})
        return

    if not did:
        send(400, {"ok": False, "error": "draft_id is required"})
        return
    key = "autosend:" + did
    if action == "arm":
        # store the whole payload (minus action) — the autonomous branch
        # gets every field back from `get`, with no dependency on n8n
        # staticData or post-Wait node references.
        value = json.dumps({k: v for k, v in payload.items()
                            if k != "action"})
        _, err = _redis(["SET", key, value, "EX", str(AUTOSEND_TTL)])
        if err:
            log("autosend-state arm failed:", err)
            send(502, {"ok": False, "error": err})
            return
        log(f"autosend-state ARM {did}")
        send(200, {"ok": True, "action": "arm", "draft_id": did})
    elif action == "disarm":
        _redis(["DEL", key])  # DEL of a missing key is a harmless no-op
        log(f"autosend-state DISARM {did}")
        send(200, {"ok": True, "action": "disarm", "draft_id": did})
    elif action == "get":
        out, err = _redis(["GET", key])
        if err:
            log("autosend-state get failed:", err)
            send(502, {"ok": False, "error": err})
            return
        raw = (out or "").strip()
        data = None
        if raw:
            try:
                data = json.loads(raw)
            except Exception:
                data = None
        send(200, {"ok": True, "action": "get", "draft_id": did,
                         "armed": bool(data), "data": data})
    elif action == "update":
        # FR-5 BUG-3 fix: the improver rewrites the draft text on an armed
        # autonomous draft so the auto-send dispatches the improved copy,
        # not the arm-time snapshot. No-op (updated=false) if not armed —
        # an approval-mode draft has no key, so calling this is harmless.
        out, err = _redis(["GET", key])
        if err:
            log("autosend-state update GET failed:", err)
            send(502, {"ok": False, "error": err})
            return
        raw = (out or "").strip()
        if not raw:
            send(200, {"ok": True, "action": "update",
                             "draft_id": did, "updated": False})
            return
        try:
            data = json.loads(raw)
        except Exception:
            data = {}
        new_text = payload.get("draft_text")
        if new_text is not None:
            data["draft_text"] = str(new_text)
        ttl_out, _ = _redis(["TTL", key])
        try:
            ttl = int((ttl_out or "0").strip())
        except (TypeError, ValueError):
            ttl = AUTOSEND_TTL
        if ttl <= 0:
            ttl = AUTOSEND_TTL
        _redis(["SET", key, json.dumps(data), "EX", str(ttl)])
        log(f"autosend-state UPDATE {did}")
        send(200, {"ok": True, "action": "update",
                         "draft_id": did, "updated": True})
    else:
        send(400, {"ok": False,
                         "error": "action must be arm|disarm|get|update"})

# (moved to routes.py — handle_<name>(payload, self._send))
# (moved to routes.py — handle_<name>(payload, self._send))


# ============================================================================
# Group: sweeps
# ============================================================================

def handle_hourly_sweep(payload, send):
    """POST /hourly-sweep — deep re-analysis with skip-if-unchanged.

    Runs every hour from the cron workflow. Algorithm:
    1. Select customers with new activity since last_analyzed_at
       (skip-if-unchanged gate). Cap at HOURLY_SWEEP_BATCH_LIMIT.
    2. For each: run cold-decay (>7d silent → COLD) then re-evaluate
       via compute_label() against cached facts. Apply confidence
       dampening from label_corrections.
    3. UPDATE conversation_state.last_analyzed_at / signal /
       confidence on every row processed.
    Always returns 200; per-customer errors logged + skipped (one
    bad row never kills the sweep)."""
    from server import (
        CONFIDENCE_DEMOTE_THRESHOLD,
        HOURLY_SWEEP_BATCH_LIMIT,
        _HARD_DEMOTE_SIGNALS,
        _LABEL_RANK,
        _TIER_BELOW,
        apply_label_transition,
        compute_confidence,
        compute_label,
        get_current_label_row,
        scan_followup_eligibility,
    )
    from labels import _should_cold_decay
    import time as _time
    t0 = _time.time()
    # Disk-space guard (incident 2026-05-29): alert admin if the root fs is
    # filling, BEFORE it hits 100% and silently kills n8n/postgres.
    try:
        from server import _disk_alert_check
        _disk_alert_check()
    except Exception as _de:
        log("disk alert check err:", repr(_de))
    try:
        sql = (
            "SELECT cs.customer_id, "
            "COALESCE(cs.last_customer_message_at, 'epoch'::timestamptz), "
            "COALESCE(cs.last_operator_reply_at, 'epoch'::timestamptz), "
            "COALESCE(cs.last_analyzed_at, 'epoch'::timestamptz), "
            "EXTRACT(EPOCH FROM (now() - COALESCE(cs.last_customer_message_at, 'epoch'::timestamptz))), "
            "(cs.last_customer_message_at IS NULL) "  # B2: never-messaged flag
            "FROM conversation_state cs "
            "WHERE COALESCE(cs.last_customer_message_at, 'epoch'::timestamptz) "
            "    > COALESCE(cs.last_analyzed_at, 'epoch'::timestamptz) "
            "   OR COALESCE(cs.last_operator_reply_at, 'epoch'::timestamptz) "
            "    > COALESCE(cs.last_analyzed_at, 'epoch'::timestamptz) "
            "ORDER BY COALESCE(cs.last_analyzed_at, 'epoch'::timestamptz) ASC "
            f"LIMIT {HOURLY_SWEEP_BATCH_LIMIT}"
        )
        out, err = _psql(sql)
        if err:
            log("hourly_sweep select err:", err)
            send(200, {"ok": False, "degraded": True,
                             "error": err[:200], "scanned": 0,
                             "transitions": 0, "hermes_calls": 0,
                             "skipped_unchanged": 0,
                             "elapsed_ms": int((_time.time() - t0) * 1000)})
            return
        rows = []
        for line in (out or "").strip().splitlines():
            parts = line.split("|")
            if len(parts) < 6:
                continue
            cid = parts[0].strip()
            if not cid:
                continue
            try:
                silent_seconds = float(parts[4].strip())
            except ValueError:
                silent_seconds = 0.0
            cmsg_null = parts[5].strip().startswith("t")  # B2
            rows.append((cid, silent_seconds, cmsg_null))
        scanned = len(rows)
        transitions = 0
        for cid, silent_seconds, cmsg_null in rows:
            try:
                row = get_current_label_row(cid)
                if row is None:
                    # No customer_facts yet — keep last_analyzed_at fresh,
                    # but nothing to evaluate.
                    update_last_analysis(cid, "no_facts_row", 1.0)
                    continue
                prev = row.get("label")
                # CONFIRMED is terminal — skip the hourly sweep entirely,
                # no transitions, no follow-up nudges. Operator can still
                # downgrade via /label.
                if prev == "CONFIRMED":
                    update_last_analysis(cid, "confirmed_terminal", 1.0)
                    continue
                # Honor manual lock window.
                cid_esc = cid.replace("'", "''")
                lock_out, _err = _psql(
                    "SELECT label_locked_until > now() FROM customer_facts "
                    f"WHERE customer_id = '{cid_esc}'"
                )
                is_locked = (lock_out or "").strip().startswith("t")
                if is_locked:
                    update_last_analysis(cid, "locked", 1.0)
                    continue
                applied = prev
                sig = "hourly_noop"
                ev = "no change"
                confidence = 1.0
                # Cold decay — highest priority for the sweep. Guarded by
                # _should_cold_decay (B2: skip never-messaged NULL-cmsg leads;
                # B3: skip WAITING_FOR_PAYMENT/PAUSED_*/already-COLD).
                if _should_cold_decay(silent_seconds, prev, cmsg_null):
                    applied = "COLD"
                    sig = "cold_decay"
                    ev = (f"silent {int(silent_seconds // 86400)}d "
                          f"(>{7}d threshold)")
                else:
                    # Re-evaluate against cached facts with no specific
                    # latest message (catches accumulated multi-yacht etc).
                    target, sig, ev = compute_label("", row)
                    confidence = compute_confidence(sig)
                    applied = target
                    if confidence < CONFIDENCE_DEMOTE_THRESHOLD:
                        applied = _TIER_BELOW.get(target, target)
                    # Sticky-upward guard — see same logic in _label_eval.
                    # Hourly sweep re-evals with no specific latest message,
                    # so signal often reverts to date_asked_no_commit etc.
                    # Don't let that demote a HOT customer to NEW.
                    prev_rank = _LABEL_RANK.get(prev or "NEW", 0)
                    new_rank = _LABEL_RANK.get(applied, 0)
                    if (new_rank < prev_rank
                            and sig not in _HARD_DEMOTE_SIGNALS
                            and confidence < CONFIDENCE_DEMOTE_THRESHOLD):
                        log(f"hourly_sweep sticky-keep cid={cid!r} "
                            f"prev={prev} would-apply={applied} "
                            f"sig={sig} conf={confidence:.2f} — kept")
                        applied = prev
                        sig = "sticky_" + (prev or "new").lower()
                        ev = (f"weak {target}/{confidence:.2f} "
                              f"signal not allowed to demote {prev}")
                        confidence = 1.0
                if applied != prev:
                    _o, err = apply_label_transition(
                        cid, prev, applied,
                        f"hourly_sweep:{sig}", ev,
                        row.get("message_count", 0))
                    if err:
                        log(f"hourly_sweep transition err cid={cid!r}:", err)
                    else:
                        transitions += 1
                update_last_analysis(cid, sig, confidence)
            except Exception as e_inner:
                log(f"hourly_sweep cust err cid={cid!r}:", repr(e_inner))
                continue
        # Estimate skipped — every customer NOT in the changed set.
        skipped_out, _err = _psql(
            "SELECT count(*) FROM conversation_state WHERE "
            "COALESCE(last_customer_message_at, 'epoch'::timestamptz) "
            "  <= COALESCE(last_analyzed_at, 'epoch'::timestamptz) "
            "AND COALESCE(last_operator_reply_at, 'epoch'::timestamptz) "
            "  <= COALESCE(last_analyzed_at, 'epoch'::timestamptz)"
        )
        try:
            skipped = int((skipped_out or "0").strip().splitlines()[0])
        except (ValueError, IndexError):
            skipped = 0
        # Proactive follow-up engine — runs AFTER label-eval so
        # newly-transitioned labels (e.g. WARM→COLD via cold-decay)
        # are considered. Fail-safe: empty list on any error.
        try:
            eligible_followups = scan_followup_eligibility()
        except Exception as _fe:
            log("followup_scan EXC:", repr(_fe))
            eligible_followups = []
        elapsed_ms = int((_time.time() - t0) * 1000)
        log(f"hourly-sweep scanned={scanned} transitions={transitions} "
            f"followups_eligible={len(eligible_followups)} "
            f"skipped={skipped} elapsed_ms={elapsed_ms}")
        send(200, {
            "ok": True, "scanned": scanned, "transitions": transitions,
            "hermes_calls": 0,  # v1: no Hermes-driven disambiguation
            "skipped_unchanged": skipped, "elapsed_ms": elapsed_ms,
            "eligible_followups": eligible_followups,
        })
    except Exception as e:
        log("hourly_sweep ERROR:", repr(e))
        send(200, {"ok": False, "degraded": True, "error": str(e),
                         "scanned": 0, "transitions": 0,
                         "hermes_calls": 0, "skipped_unchanged": 0,
                         "eligible_followups": [],
                         "elapsed_ms": int((_time.time() - t0) * 1000)})

def handle_followup_action(payload, send):
    """POST /followup-action — log a proactive-follow-up event to
    autonomous_sends.notes. Body: {customer_id, action, label?,
    silence_window?, silence_hours?, draft_text?}.
    action ∈ {drafted, sent, skipped, edited}.
    Fail-safe: always returns 200; logs but never raises."""
    cid = (payload.get("customer_id") or "").strip()
    action = (payload.get("action") or "").strip().lower()
    if not cid or action not in ("drafted", "sent", "skipped", "edited"):
        send(200, {"ok": False,
                         "error": "customer_id + action(drafted|sent|skipped|edited) required"})
        return
    try:
        kind = "proactive_followup_" + action
        notes = {
            "label": payload.get("label"),
            "silence_window": payload.get("silence_window"),
            "silence_hours": payload.get("silence_hours"),
            "draft_text": payload.get("draft_text"),
        }
        notes = {k: v for k, v in notes.items() if v is not None}
        sql = (
            "INSERT INTO autonomous_sends (customer_id, kind, notes) "
            f"VALUES ({_lit(cid)}, {_lit(kind)}, "
            f"{_lit(json.dumps(notes))}::jsonb)"
        )
        _, err = _psql(sql)
        if err:
            log("followup_action insert err:", err)
            send(200, {"ok": False, "degraded": True,
                             "error": err[:200]})
            return
        log(f"followup-action cid={cid!r} action={action} "
            f"window={notes.get('silence_window')!r}")
        send(200, {"ok": True, "customer_id": cid, "action": action})
    except Exception as e:
        log("followup_action EXC:", repr(e))
        send(200, {"ok": False, "degraded": True, "error": str(e)})

# (moved to routes.py — handle_<name>(payload, self._send))
# (moved to routes.py — handle_<name>(payload, self._send))
# (moved to routes.py — handle_<name>(payload, self._send))

def handle_draft_freshness(payload, send):
    """POST /draft-freshness — check if a follow-up draft has gone stale
    (customer replied OR operator replied after the draft was generated).
    Returns
    {ok, stale: bool, stale_reason, is_followup, draft_ts, last_msg_ts,
     customer_id, draft_id, last_msg_preview, customer_name}. Fail-safe:
    returns stale=false on any error so Send chain isn't blocked by
    bridge issues."""
    from server import _draft_get, waha_fetch_history
    did = (payload.get("draft_id") or "").strip()
    if not did:
        send(200, {"ok": False, "stale": False,
                         "error": "draft_id required"})
        return
    try:
        d, err = _draft_get(did)
        if err or not d:
            send(200, {"ok": False, "stale": False,
                             "error": err or "draft not found"})
            return
        # Only enforce freshness for follow-up drafts. Normal customer-
        # message drafts were already generated FROM the latest history.
        if not d.get("is_followup"):
            send(200, {"ok": True, "stale": False,
                             "is_followup": False, "draft_id": did})
            return
        cid = d.get("customer_phone", "")
        draft_ts = d.get("timestamp", "")
        cid_e = (cid or "").replace("'", "''")
        # Get BOTH last_customer_message_at AND last_operator_reply_at as
        # epoch seconds. A proactive follow-up is stale if EITHER fired
        # after the draft was generated:
        #   - customer messaged → the chat has moved on, this card is stale
        #   - operator replied manually → we already responded, this card
        #     is now an unsolicited upsell (the production bug we hit
        #     2026-05-25: customer says "Okay", operator replies "take
        #     your time! 😊", queued proactive card later sends as an
        #     unsolicited Zenith 64 specs upsell).
        out, _err = _psql(
            "SELECT EXTRACT(EPOCH FROM last_customer_message_at), "
            "to_char(last_customer_message_at, 'YYYY-MM-DD HH24:MI:SS'), "
            "COALESCE(EXTRACT(EPOCH FROM last_operator_reply_at), 0) "
            f"FROM conversation_state WHERE customer_id = '{cid_e}'"
        )
        last_msg_epoch = 0.0
        last_msg_str = ""
        last_op_epoch = 0.0
        for line in (out or "").strip().splitlines():
            parts = line.split("|")
            if len(parts) >= 3:
                try:
                    last_msg_epoch = float(parts[0].strip())
                except ValueError:
                    last_msg_epoch = 0.0
                last_msg_str = parts[1].strip()
                try:
                    last_op_epoch = float(parts[2].strip())
                except ValueError:
                    last_op_epoch = 0.0
            break
        # Parse draft.timestamp (ISO string) → epoch seconds
        from datetime import datetime as _dt
        draft_epoch = 0.0
        if draft_ts:
            try:
                draft_epoch = _dt.fromisoformat(
                    draft_ts.replace("Z", "+00:00")).timestamp()
            except Exception:
                draft_epoch = 0.0
        # Stale if EITHER condition holds. Operator-reply check is the
        # primary defense against unsolicited upsells when the operator
        # manually replied before getting to the queued follow-up card.
        stale_by_customer = (last_msg_epoch > 0 and draft_epoch > 0
                             and last_msg_epoch > draft_epoch)
        stale_by_op = (last_op_epoch > 0 and draft_epoch > 0
                       and last_op_epoch > draft_epoch)
        stale = stale_by_customer or stale_by_op
        stale_reason = ("op_replied" if stale_by_op
                        else ("customer_replied" if stale_by_customer
                              else None))
        # If stale, also fetch the customer's last message body via WAHA
        # so the alert can show what was said.
        last_preview = ""
        cust_name = d.get("customer_name", "")
        if stale:
            waha = waha_fetch_history(cid, limit=3)
            if not waha.get("err"):
                last_preview = (waha.get("last_message") or "")[:200]
                if waha.get("push_name") and not cust_name:
                    cust_name = waha["push_name"]
        send(200, {
            "ok": True, "stale": stale, "is_followup": True,
            "stale_reason": stale_reason,
            "draft_id": did, "customer_id": cid,
            "customer_name": cust_name,
            "draft_ts": draft_ts, "last_msg_ts": last_msg_str,
            "last_msg_preview": last_preview,
            "lag_seconds": int(last_msg_epoch - draft_epoch)
                           if (last_msg_epoch and draft_epoch) else 0,
            "lag_seconds_op": int(last_op_epoch - draft_epoch)
                              if (last_op_epoch and draft_epoch) else 0,
        })
    except Exception as e:
        log("draft_freshness EXC:", repr(e))
        send(200, {"ok": True, "stale": False, "error": str(e)})

# (moved to routes.py — handle_<name>(payload, self._send))


# ============================================================================
# Group: drafting
# ============================================================================

def handle_draft(payload, send):
    from server import (
        HERMES_TIMEOUT,
        build_query,
        extract_json,
        extract_session,
        get_mode,
        run_hermes,
        save_health,
        save_trigger,
    )
    if not (payload.get("incoming_message") or "").strip():
        send(400, {"ok": False, "error": "incoming_message is required"})
        return
    query = build_query(payload)
    try:
        rc, out, err, elapsed = run_hermes(query)
    except subprocess.TimeoutExpired:
        log(f"hermes TIMEOUT after {HERMES_TIMEOUT}s")
        send(502, {"ok": False, "error": "hermes timeout"})
        return
    except Exception as e:
        log("hermes EXEC ERROR", repr(e))
        send(502, {"ok": False, "error": f"hermes exec error: {e}"})
        return

    parsed, blob = extract_json(out)
    sid = extract_session(out, err)
    messages = parsed.get("messages") if isinstance(parsed, dict) else None
    ok = (rc == 0 and isinstance(messages, list) and len(messages) > 0)
    if not ok:
        log(f"hermes FAIL rc={rc} parsed={parsed is not None} err={err[:300]!r}")
        send(502, {"ok": False, "error": "hermes produced no parseable draft",
                         "rc": rc, "raw": (blob or out)[:2000]})
        return

    notes = parsed.get("notes_for_zayn", "")
    # Step 7: conversation health — prepend to operator notes + persist
    health = parsed.get("health")
    if isinstance(health, dict) and (health.get("score") or "").strip():
        hs = str(health.get("score")).strip()
        hr = str(health.get("reason") or "").strip()
        notes = "⚕️ " + hs + (" — " + hr if hr else "") + "\n" + notes
        save_health(payload.get("customer_id"), payload.get("customer_name"),
                    hs, hr)
    else:
        health = None
    sugg = parsed.get("suggested_rule")
    if not (isinstance(sugg, dict) and (sugg.get("text") or "").strip()):
        sugg = None
    # Step 6: customer-promise trigger detection
    trig = parsed.get("detected_trigger")
    trig_id = None
    if isinstance(trig, dict) and (trig.get("type") or "").strip():
        trig_id, terr = save_trigger(
            payload.get("customer_id"), payload.get("customer_name"),
            trig.get("type"), trig.get("reminder_hours", 24),
            trig.get("context", ""), payload.get("incoming_message", ""),
            trig.get("confidence", 0.5))
        if not trig_id:
            log("trigger save failed:", terr)
    conv_mode = get_mode(payload.get("customer_id"))
    # /draft NEVER decides or logs an autonomous send. Autonomous-send
    # gating — the safety caps, the QC sample, and the cap counters — is
    # owned solely by /autosend-check, which the workflow calls AFTER the
    # operator wait window. Evaluating it here would auto-send before that
    # window exists and would double-count against /autosend-check, so
    # /draft only ever drafts. conversation_mode is reported below for the
    # operator's awareness only; it is never acted on here.
    auto_send, auto_send_reason = False, ""
    log(f"draft OK customer={payload.get('customer_name')!r} "
        f"mode={payload.get('mode', 'initial')} conv_mode={conv_mode} "
        f"auto_send={auto_send} msgs={len(messages)} "
        f"rule_suggested={sugg is not None} trigger={trig_id} "
        f"elapsed={elapsed}ms session={sid}")
    send(200, {
        "ok": True,
        "messages": messages,
        "notes_for_zayn": notes,
        "suggested_rule": sugg,
        "detected_trigger": trig if trig_id else None,
        "trigger_id": trig_id,
        "health": health,
        "conversation_mode": conv_mode,
        "auto_send": auto_send,
        "auto_send_reason": auto_send_reason,
        "raw": blob,
        "session_id": sid,
        "elapsed_ms": elapsed,
    })

def handle_improve(payload, send):
    from server import (
        HERMES_TIMEOUT,
        build_improve_query,
        extract_json,
        extract_session,
        run_hermes,
    )
    if not payload.get("current_draft"):
        send(400, {"ok": False, "error": "current_draft is required"})
        return
    query = build_improve_query(payload)
    try:
        rc, out, err, elapsed = run_hermes(query, priority="background")
    except subprocess.TimeoutExpired:
        log(f"improve TIMEOUT after {HERMES_TIMEOUT}s")
        send(502, {"ok": False, "error": "hermes timeout"})
        return
    except Exception as e:
        log("improve EXEC ERROR", repr(e))
        send(502, {"ok": False, "error": f"hermes exec error: {e}"})
        return
    parsed, blob = extract_json(out)
    sid = extract_session(out, err)
    messages = parsed.get("messages") if isinstance(parsed, dict) else None
    ok = (rc == 0 and isinstance(messages, list) and len(messages) > 0)
    if not ok:
        log(f"improve FAIL rc={rc} parsed={parsed is not None} err={err[:200]!r}")
        send(502, {"ok": False,
                         "error": "hermes produced no parseable result",
                         "rc": rc, "raw": (blob or out)[:2000]})
        return
    improved = bool(parsed.get("improved"))
    note = str(parsed.get("note") or "")
    log(f"improve OK customer={payload.get('customer_name')!r} "
        f"improved={improved} msgs={len(messages)} "
        f"elapsed={elapsed}ms session={sid}")
    send(200, {
        "ok": True,
        "improved": improved,
        "messages": messages,
        "note": note,
        "raw": blob,
        "session_id": sid,
        "elapsed_ms": elapsed,
    })

def handle_edit_rule(payload, send):
    """POST /edit-rule — operator decision on a pattern-detected rule suggestion
    (draft feedback loop P3). Confirm card buttons editrule:<suggest_id>:<action>.

    actions:
      save     → INSERT the suggested rule (active, source='edit_learning') and
                 stamp the contributing corrections.
      discard  → drop the suggestion.
      arm-edit → arm an await key so the operator's next reply replaces the rule
                 text (handled by /edit-feedback await-detail).
    Returns {ok, card_text} for the n8n card edit. Fail-safe 200."""
    from server import (
        save_behavior_rule,
        edit_corr_stamp_rule,
        _redis,
    )
    action = (payload.get("action") or "").strip().lower()
    sid = (payload.get("suggest_id") or "").strip()
    chat_id = str(payload.get("chat_id") or "").strip()
    key = "rulesuggest:" + sid
    if action == "discard":
        try:
            _redis(["DEL", key])
        except Exception:
            pass
        send(200, {"ok": True, "card_text": "❌ Rule discarded."})
        return
    if action in ("arm-edit", "edit"):
        if chat_id and sid:
            try:
                _redis(["SET", "editrule:await:" + chat_id, sid, "EX", "300"])
            except Exception:
                pass
        send(200, {"ok": True,
                         "card_text": "✏️ Reply to this message with your "
                         "version of the rule."})
        return
    if action == "save":
        cur, _ = _redis(["GET", key])
        raw = (cur or "").strip()
        if not raw:
            send(200, {"ok": False,
                             "card_text": "⚠️ This suggestion expired."})
            return
        try:
            s = json.loads(raw)
        except Exception:
            send(200, {"ok": False,
                             "card_text": "⚠️ Couldn't read the suggestion."})
            return
        rid, err = save_behavior_rule(
            s.get("rule_text", ""), s.get("scope", "GLOBAL_RULE"),
            s.get("scope_value"), "edit_learning", s.get("reasoning", ""),
            source="edit_learning")
        if err or not rid:
            send(200, {"ok": False, "card_text": "⚠️ Save failed: " + str(err)})
            return
        edit_corr_stamp_rule(rid, s.get("reason_tag", ""),
                             s.get("context_label", ""))
        try:
            _redis(["DEL", key])
        except Exception:
            pass
        log(f"edit-rule SAVE id={rid} from suggest={sid}")
        send(200, {"ok": True, "rule_id": rid,
                         "card_text": "✅ Rule saved & active:\n"
                         + s.get("rule_text", "")})
        return
    send(400, {"ok": False, "error": "action must be save|discard|arm-edit"})


def handle_edit_feedback(payload, send):
    """POST /edit-feedback — feedback-prompt interactions (draft feedback loop P2).

    actions:
      tag          {correction_id, reason_tag, chat_id} → save reason_tag; if
                   not 'skip', arm an await-detail key for the chat and return a
                   guided follow-up question (Hermes, deterministic fallback).
      await-detail {chat_id, text} → if this chat has an armed correction, save
                   the text as reason_detail and clear the key. Returns {captured}.

    Fail-safe: 200 ok:false on any error (never disturbs operator messaging)."""
    from server import (
        EDIT_REASON_QUESTIONS,
        build_edit_question_query,
        build_rule_draft_query,
        edit_corr_get,
        edit_corr_set_detail,
        edit_corr_set_reason,
        edit_corr_stamp_rule,
        edit_pattern_check,
        run_hermes,
        save_behavior_rule,
        _redis,
        _tg_post,
    )
    import random
    action = (payload.get("action") or "").strip().lower()
    if action == "tag":
        corr_id = payload.get("correction_id")
        tag = (payload.get("reason_tag") or "").strip()
        chat_id = str(payload.get("chat_id") or "").strip()
        ok, err = edit_corr_set_reason(corr_id, tag)
        if tag == "skip":
            send(200, {"ok": ok, "skip": True, "error": err})
            return
        question = EDIT_REASON_QUESTIONS.get(tag, "What would you change?")
        row, _ = edit_corr_get(corr_id)
        if row:
            try:
                rc, out, _, _ = run_hermes(
                    build_edit_question_query(tag, row.get("original", ""),
                                              row.get("sent", "")),
                    timeout=30, priority="background")
                lines = [l.strip() for l in (out or "").splitlines() if l.strip()]
                if rc == 0 and lines:
                    cand = lines[-1].strip().strip('"').strip()
                    if 5 <= len(cand) <= 200:
                        question = cand
            except Exception as e:
                log("edit-feedback question err:", repr(e))
        if chat_id and corr_id is not None:
            try:
                _redis(["SET", "editfb:await:" + chat_id, str(corr_id),
                        "EX", "180"])
            except Exception:
                pass
        # Pattern detection (P3): >=3 edits with this reason_tag + context ->
        # Hermes drafts a rule and we post a confirm card. Never auto-applied.
        try:
            pairs, ctx = edit_pattern_check(corr_id, tag)
            if pairs:
                rule_text = ""
                rc, out, _, _ = run_hermes(build_rule_draft_query(tag, ctx, pairs),
                                           timeout=40, priority="background")
                cand = [l.strip() for l in (out or "").splitlines() if l.strip()]
                if rc == 0 and cand:
                    rule_text = cand[-1].strip().strip('"').strip()[:300]
                if rule_text:
                    sug = "".join(random.choice("0123456789abcdef")
                                  for _ in range(10))
                    suggestion = {
                        "rule_text": rule_text, "scope": "GLOBAL_RULE",
                        "scope_value": None,
                        "reasoning": (f"auto-drafted from {len(pairs)} '{tag}' "
                                      "edits" + (f" ({ctx})" if ctx else "")),
                        "reason_tag": tag, "context_label": ctx}
                    _redis(["SET", "rulesuggest:" + sug, json.dumps(suggestion),
                            "EX", "86400"])
                    kb = {"inline_keyboard": [[
                        {"text": "✅ Save", "callback_data": "editrule:" + sug + ":save"},
                        {"text": "✏️ Edit", "callback_data": "editrule:" + sug + ":edit"},
                        {"text": "❌ Discard", "callback_data": "editrule:" + sug + ":discard"}]]}
                    card = ("🧠 Pattern detected — " + str(len(pairs))
                            + " edits flagged '" + tag + "'"
                            + (" in " + ctx if ctx else "")
                            + ".\n\nSuggested rule:\n" + rule_text
                            + "\n\nSave as an active behavior rule?")
                    _tg_post("sendMessage", {
                        "chat_id": int(chat_id) if chat_id.isdigit() else 5532831477,
                        "text": card, "reply_markup": kb})
                    log(f"edit-feedback PATTERN tag={tag!r} ctx={ctx!r} "
                        f"n={len(pairs)} suggest={sug}")
        except Exception as e:
            log("edit-feedback pattern err:", repr(e))
        log(f"edit-feedback TAG id={corr_id} tag={tag!r}")
        send(200, {"ok": True, "skip": False, "question": question})
        return
    if action == "await-detail":
        chat_id = str(payload.get("chat_id") or "").strip()
        text = (payload.get("text") or "").strip()
        if not chat_id or not text:
            send(200, {"ok": True, "captured": False})
            return
        # editrule edit-via-reply takes priority: operator tapped Edit on a rule
        # confirm card, now typed their version → save it as the rule.
        er, _ = _redis(["GET", "editrule:await:" + chat_id])
        er = (er or "").strip()
        if er:
            cur, _ = _redis(["GET", "rulesuggest:" + er])
            raw = (cur or "").strip()
            saved = False
            if raw:
                try:
                    s = json.loads(raw)
                except Exception:
                    s = None
                if s:
                    rid, _e = save_behavior_rule(
                        text, s.get("scope", "GLOBAL_RULE"), s.get("scope_value"),
                        "edit_learning", s.get("reasoning", ""),
                        source="edit_learning")
                    if rid:
                        edit_corr_stamp_rule(rid, s.get("reason_tag", ""),
                                             s.get("context_label", ""))
                        saved = True
                    _redis(["DEL", "rulesuggest:" + er])
            _redis(["DEL", "editrule:await:" + chat_id])
            log(f"edit-rule EDIT-SAVE chat={chat_id} saved={saved}")
            send(200, {"ok": True, "captured": True, "type": "rule"})
            return
        cur, _ = _redis(["GET", "editfb:await:" + chat_id])
        corr_id = (cur or "").strip()
        if not corr_id:
            send(200, {"ok": True, "captured": False})
            return
        ok, err = edit_corr_set_detail(corr_id, text)
        try:
            _redis(["DEL", "editfb:await:" + chat_id])
        except Exception:
            pass
        log(f"edit-feedback DETAIL id={corr_id} captured={ok}")
        send(200, {"ok": ok, "captured": ok, "error": err})
        return
    send(400, {"ok": False, "error": "action must be tag|await-detail"})


def handle_edit_capture(payload, send):
    """POST /edit-capture — store an operator edit-delta (the first Hermes
    draft vs the text actually sent) for the draft-feedback learning loop.

    Stores EVERY correction (difflib ratio < 0.8 == >20% changed) immediately,
    regardless of whether the operator later engages the feedback prompt — the
    diffs feed batch pattern analysis. Below threshold → nothing stored, no
    prompt. Fail-safe: returns 200 ok:false on ANY error so it can never block
    or disturb the send flow.

    Body: {draft_id, sent_text?}  (sent_text overrides the Redis draft's
    draft_text — used by the manual 'send this: X' path)."""
    import difflib
    from server import (
        _draft_get,
        edit_corr_insert,
        get_current_label_row,
    )
    from waha import country_flag_for_cid
    did = (payload.get("draft_id") or "").strip()
    if not did:
        send(400, {"ok": False, "error": "draft_id required"})
        return
    try:
        d, _ = _draft_get(did)
        if not d:
            send(200, {"ok": False, "captured": False, "error": "draft not found"})
            return
        original = str(d.get("original_draft_text") or "").strip()
        sent = str(payload.get("sent_text") or d.get("draft_text") or "").strip()
        cid = (d.get("customer_phone") or "").strip()
        # No baseline (e.g. a draft created before original_draft_text existed)
        # or no sent text → nothing to compare; skip silently.
        if not original or not sent or not cid:
            send(200, {"ok": True, "captured": False, "should_prompt": False,
                             "reason": "no baseline"})
            return
        ratio = difflib.SequenceMatcher(None, original, sent).ratio()
        if ratio >= 0.8:
            send(200, {"ok": True, "captured": False, "should_prompt": False,
                             "similarity": round(ratio, 3)})
            return
        label = yacht = country = ""
        try:
            row = get_current_label_row(cid) or {}
            label = row.get("label", "") or ""
            yacht = (str(row.get("yachts", "") or "").split(",")[0]).strip()
        except Exception as e:
            log("edit-capture context err:", repr(e))
        try:
            country = country_flag_for_cid(cid) or ""
        except Exception:
            country = ""
        corr_id, err = edit_corr_insert(cid, original, sent, label, yacht,
                                        country, round(ratio, 3))
        if err:
            log("edit-capture INSERT err:", err)
            send(200, {"ok": False, "captured": False, "error": err})
            return
        log(f"edit-capture STORED id={corr_id} cid={cid} sim={ratio:.3f} "
            f"label={label!r} yacht={yacht!r}")
        send(200, {"ok": True, "captured": True, "correction_id": corr_id,
                         "similarity": round(ratio, 3), "should_prompt": True})
    except Exception as e:
        log("edit-capture ERROR", repr(e))
        send(200, {"ok": False, "captured": False, "error": str(e)})


def handle_quality_check(payload, send):
    """POST /quality-check — fast quality SCORE of a draft (no rewrite).
    Returns {ok, score:1-10, flags:[...], summary}. Fail-safe: on ANY error
    returns 200 ok:false so n8n simply shows no badge — the draft is never
    silently changed. Replaces the old /improve auto-rewrite pass."""
    from server import (
        build_quality_query,
        extract_json,
        run_hermes,
    )
    if not payload.get("current_draft"):
        send(400, {"ok": False, "error": "current_draft is required"})
        return
    try:
        # interactive, NOT background: the operator is actively waiting for the
        # draft/refine/regen card this badge goes on. Background waits for the
        # single BG slot, which the hourly lead-analysis sweep holds for ~25s+
        # → the refine/regen quality-check timed out (35s) and the scorecard
        # silently vanished during sweep windows (operator 2026-06-01).
        rc, out, err, elapsed = run_hermes(
            build_quality_query(payload), priority="interactive")
    except subprocess.TimeoutExpired:
        log("quality-check TIMEOUT")
        send(200, {"ok": False, "error": "hermes timeout"})
        return
    except Exception as e:
        log("quality-check EXEC ERROR", repr(e))
        send(200, {"ok": False, "error": f"hermes exec error: {e}"})
        return
    parsed, blob = extract_json(out)
    score = None
    if isinstance(parsed, dict):
        try:
            score = int(parsed.get("score"))
        except (TypeError, ValueError):
            score = None
    if rc != 0 or score is None or not (1 <= score <= 10):
        log(f"quality-check FAIL rc={rc} parsed={parsed is not None}")
        send(200, {"ok": False, "error": "no parseable score",
                         "rc": rc, "raw": (blob or out)[:1000]})
        return
    flags = parsed.get("flags")
    flags = [str(f) for f in flags][:3] if isinstance(flags, list) else []
    summary = str(parsed.get("summary") or "")
    badge = _format_quality_badge({"score": score, "flags": flags})
    log(f"quality-check OK customer={payload.get('customer_name')!r} "
        f"score={score} flags={flags} elapsed={elapsed}ms")
    # Shadow mode (draft-log Part 2): if this conversation is in 'shadow' mode,
    # RECORD what autonomy WOULD do with this draft (would_send if it clears the
    # floor, else would_hold) — WITHOUT sending. /quality-check fires for every
    # draft's badge regardless of mode, so it's the natural observation point; it
    # reuses the score just computed (no extra scoring, no n8n). Fail-OPEN: a
    # logging error never affects the badge response below.
    try:
        _sh_cid = (payload.get("customer_id") or "").strip()
        if _sh_cid:
            from server import (get_mode, canonicalize_cid,
                                AUTOSEND_MIN_SCORE, _draft_log_write)
            from labels import _quality_floor_ok
            _sh_cid = canonicalize_cid(_sh_cid)
            if get_mode(_sh_cid) == "shadow":
                _wd = ("would_send"
                       if _quality_floor_ok(score, AUTOSEND_MIN_SCORE)
                       else "would_hold")
                _sh_did = ((payload.get("draft_id") or "").strip()
                           or ("shadow:" + _sh_cid + ":"
                               + str(int(time.time() * 1000))))
                _draft_log_write(_sh_did, customer_id=_sh_cid, mode="shadow",
                                 score=score, score_flags=", ".join(flags),
                                 score_summary=summary, outcome=_wd,
                                 is_shadow=True,
                                 draft_text=payload.get("current_draft"))
                log(f"SHADOW {_sh_cid} score={score} -> {_wd} "
                    "(logged, NOT sent)")
    except Exception as _she:
        log(f"shadow-log non-fatal: {_she!r}")
    send(200, {"ok": True, "score": score, "flags": flags,
                     "summary": summary, "badge": badge,
                     "elapsed_ms": elapsed})


# === ≥8 quality gate (operator 2026-06-01: "every draft should score 8+") =====
def _anthropic_draft(system_text, history, name, phone, user_message, hint=""):
    """One draft via the Anthropic Messages API — same model/shape as the n8n
    'Claude AI' node. Returns (messages_list, notes, err)."""
    import urllib.request
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        return [], "", "ANTHROPIC_API_KEY not set"
    uc = ("CONVERSATION HISTORY (oldest first):\n" + (history or "") +
          "\n\nCustomer: " + (name or "unknown") + " (" + (phone or "") +
          ")\n\nTHEIR NEWEST MESSAGE:\n" + (user_message or "") +
          (("\n\nIMPORTANT: " + hint) if hint else "") +
          "\n\nReturn ONLY the JSON object specified in the system prompt - "
          "no preamble, no code fences.")
    data = json.dumps({"model": "claude-sonnet-4-6", "max_tokens": 1024,
                       "system": [{"type": "text", "text": system_text}],
                       "messages": [{"role": "user", "content": uc}]}).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=data)
    req.add_header("x-api-key", key)
    req.add_header("anthropic-version", "2023-06-01")
    req.add_header("content-type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            txt = json.load(r)["content"][0]["text"]
    except Exception as e:
        return [], "", f"anthropic error: {e}"
    t = re.sub(r"^```json\s*|^```\s*|```\s*$", "", (txt or "").strip()).strip()
    try:
        parsed = json.loads(t)
        msgs = parsed.get("messages")
        msgs = [str(m) for m in msgs][:4] if isinstance(msgs, list) else []
        return msgs, (parsed.get("notes_for_zayn") or ""), ""
    except Exception:
        return [], "", "unparseable draft"


def _anthropic_score(query_text):
    """Fast quality score via Anthropic (the local Hermes scorer is slow on the
    box). query_text = build_quality_query(...). Returns (score, flags, summary)."""
    import urllib.request
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        return 0, [], ""
    data = json.dumps({"model": "claude-sonnet-4-6", "max_tokens": 300,
                       "system": [{"type": "text", "text": "You are a strict "
                        "WhatsApp-draft quality scorer. Output ONLY the JSON "
                        "object requested — no preamble, no code fences."}],
                       "messages": [{"role": "user", "content": query_text}]}).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=data)
    req.add_header("x-api-key", key)
    req.add_header("anthropic-version", "2023-06-01")
    req.add_header("content-type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            txt = json.load(r)["content"][0]["text"]
    except Exception:
        return 0, [], ""
    t = re.sub(r"^```json\s*|^```\s*|```\s*$", "", (txt or "").strip()).strip()
    try:
        p = json.loads(t)
        sc = int(p.get("score")) if str(p.get("score", "")).strip() else 0
        fl = p.get("flags") if isinstance(p.get("flags"), list) else []
        return (sc if 1 <= sc <= 10 else 0), fl, str(p.get("summary") or "")
    except Exception:
        return 0, [], ""


def _gate_loop(full_system, history, name, phone, user_message, cid,
               score_system, threshold=8, max_attempts=3, override=""):
    """Draft via Anthropic + score via Hermes (against score_system), regen
    feeding the scorer's flags back until >= threshold or max_attempts. Returns
    the best {messages, notes, score, flags, summary, attempts, capped} or None.
    Shared by the main-draft gate and the (fast) Draft-message button."""
    from server import build_quality_query, sanitize_draft_messages
    best, hint, attempts = None, "", 0
    for attempt in range(1, max_attempts + 1):
        attempts = attempt
        msgs, notes, _err = _anthropic_draft(full_system, history, name, phone,
                                             user_message, hint)
        if not msgs:
            if best is None:
                continue
            break
        try:
            score, flags, summary = _anthropic_score(build_quality_query({
                "system_prompt": score_system, "customer_name": name,
                "history": history, "incoming_message": user_message,
                "current_draft": "\n\n".join(msgs), "customer_id": cid}))
        except Exception:
            score, flags, summary = 0, [], ""
        if best is None or score > best["score"]:
            best = {"messages": msgs, "notes": notes, "score": score,
                    "flags": flags, "summary": summary}
        if override or score >= threshold:
            break
        hint = (f"Your previous draft scored {score}/10. Produce a clearly "
                f"BETTER draft that fixes: "
                f"{', '.join(str(f) for f in flags) or summary}. Professional, "
                "no emoji unless the customer used emoji, no hype opener, answer "
                "directly.")
    if best is not None:
        best["attempts"] = attempts
        best["capped"] = best["score"] < threshold
        try:
            best["messages"], _ = sanitize_draft_messages(best["messages"])
        except Exception:
            pass
    return best


def handle_draft_gated(payload, send):
    """POST /draft-gated — draft via Anthropic, SCORE via Hermes against the SAME
    persona, and if below threshold REGENERATE feeding the scorer's flags back,
    up to max_attempts. Returns the BEST draft. override_directions skips the
    gate. Fail-open: ok:false lets n8n fall back to its normal draft."""
    from server import (build_quality_query, extract_json, run_hermes,
                        sanitize_draft_messages)
    sp = (payload.get("system_prompt") or "").strip()
    if not sp:
        send(200, {"ok": False, "error": "system_prompt required"})
        return
    cid = (payload.get("customer_id") or "").strip()
    name = (payload.get("customer_name") or "").strip()
    phone = (payload.get("customer_phone") or "").strip()
    hist = payload.get("conversation_history") or ""
    umsg = payload.get("user_message") or ""
    bctx = (payload.get("behavioral_context") or "").strip()
    override = (payload.get("override_directions") or "").strip()
    try:
        threshold = int(payload.get("threshold") or 8)
    except (TypeError, ValueError):
        threshold = 8
    try:
        max_attempts = max(1, min(4, int(payload.get("max_attempts") or 3)))
    except (TypeError, ValueError):
        max_attempts = 3
    full_system = sp + (("\n\n" + bctx) if bctx else "")
    if override:
        full_system += ("\n\nOPERATOR OVERRIDE: write the customer message "
                        "exactly as the operator directs here, even if it would "
                        "score low — \"" + override + "\"")
    best, hint, attempts = None, "", 0
    for attempt in range(1, max_attempts + 1):
        attempts = attempt
        msgs, notes, err = _anthropic_draft(full_system, hist, name, phone,
                                            umsg, hint)
        if not msgs:
            if best is None:
                continue
            break
        try:
            # Score via the FAST Anthropic HTTP scorer (mirrors _gate_loop),
            # NOT the slow local Hermes CLI: 3 sequential ~30-60s CLI scores on
            # a 2-vCPU box made regen crawl and hit the n8n 150s timeout. Same
            # (score, flags, summary) contract. (Bug 2 slow-regen, 2026-06-02.)
            score, flags, summary = _anthropic_score(build_quality_query({
                "system_prompt": sp, "customer_name": name, "history": hist,
                "incoming_message": umsg, "current_draft": "\n\n".join(msgs),
                "customer_id": cid}))
        except Exception:
            score, flags, summary = 0, [], ""
        if best is None or score > best["score"]:
            best = {"messages": msgs, "notes": notes, "score": score,
                    "flags": flags, "summary": summary}
        if override or score >= threshold:
            break
        hint = (f"Your previous draft scored {score}/10. Produce a clearly "
                f"BETTER draft that fixes these problems: "
                f"{', '.join(str(f) for f in flags) or summary}. Professional, "
                "no emoji unless the customer used emoji, no hype opener, answer "
                "directly.")
    if best is None:
        send(200, {"ok": False, "error": "no draft produced"})
        return
    try:
        # MUST unpack the (list, bool) tuple — assigning the whole tuple put a
        # Python False into messages -> a literal "false" bubble sent to a
        # customer (2026-06-02 incident). Mirror the correct _gate_loop caller.
        best["messages"], _ = sanitize_draft_messages(best["messages"])
    except Exception:
        pass
    log(f"draft-gated cid={cid} score={best['score']} attempts={attempts} "
        f"capped={best['score'] < threshold}")
    send(200, {"ok": True, "messages": best["messages"],
               "notes_for_zayn": best["notes"], "score": best["score"],
               "flags": best["flags"], "summary": best["summary"],
               "attempts": attempts, "capped": best["score"] < threshold,
               "override": bool(override)})


def handle_learn(payload, send):
    from server import (
        VALID_SCOPES,
        build_learn_query,
        extract_json,
        run_hermes,
        save_behavior_rule,
    )
    fb = (payload.get("feedback") or "").strip()
    if not fb:
        send(400, {"ok": False, "error": "feedback is required"})
        return
    query = build_learn_query(payload)
    try:
        rc, out, err, elapsed = run_hermes(query, priority="background")
    except subprocess.TimeoutExpired:
        send(502, {"ok": False, "error": "hermes timeout"})
        return
    except Exception as e:
        send(502, {"ok": False, "error": f"hermes exec error: {e}"})
        return
    parsed, blob = extract_json(out)
    if not isinstance(parsed, dict):
        log(f"learn FAIL rc={rc} (no parseable JSON)")
        send(502, {"ok": False, "error": "hermes produced no result",
                         "rc": rc, "raw": (blob or out)[:1000]})
        return
    if not parsed.get("is_rule"):
        log(f"learn: feedback judged one-off elapsed={elapsed}ms")
        send(200, {"ok": True, "captured": False,
                         "reason": "feedback judged a one-off tweak"})
        return
    rule_text = (parsed.get("rule_text") or "").strip()
    if not rule_text:
        send(200, {"ok": True, "captured": False,
                         "reason": "no rule_text returned"})
        return
    scope = (parsed.get("scope") or "global").strip().lower()
    if scope not in VALID_SCOPES:
        scope = "global"
    scope_value = ((payload.get("customer_id") or "").strip()
                   if scope == "customer" else None)
    rid, e2 = save_behavior_rule(rule_text, scope, scope_value,
                                 "edit_feedback", parsed.get("reasoning"))
    if rid is None:
        log("learn save FAILED:", e2)
        send(502, {"ok": False, "error": "rule insert failed: " + str(e2)})
        return
    log(f"learn: rule captured id={rid} scope={scope} text={rule_text[:70]!r}")
    send(200, {"ok": True, "captured": True, "rule_id": rid,
                     "rule_text": rule_text, "scope": scope,
                     "reasoning": parsed.get("reasoning", ""),
                     "elapsed_ms": elapsed})

def handle_save_rule(payload, send):
    from server import VALID_SCOPES, save_behavior_rule
    text = (payload.get("rule_text") or "").strip()
    if not text:
        send(400, {"ok": False, "error": "rule_text is required"})
        return
    scope = (payload.get("scope") or "global").strip().lower()
    if scope not in VALID_SCOPES:
        scope = "global"
    scope_value = (payload.get("scope_value") or "").strip() or None
    created_via = (payload.get("created_via") or "refinement_capture").strip()
    reasoning = (payload.get("reasoning") or "").strip() or None
    rid, err = save_behavior_rule(text, scope, scope_value, created_via, reasoning)
    if rid is None:
        log("save-rule FAILED:", err)
        send(502, {"ok": False, "error": "insert failed: " + str(err)})
        return
    log(f"rule saved id={rid} scope={scope} text={text[:70]!r}")
    send(200, {"ok": True, "rule_id": rid, "scope": scope})


# === Pillar C: "ask, don't guess" + learn (operator 2026-06-01) ============
# If Hermes lacks a concrete fact to answer correctly, it must ASK the operator
# BEFORE drafting (hard rule injected via behavioral_context). The drafter sets
# needs_operator_input → n8n calls /ask-operator → operator's reply is caught by
# the /queue 'awaiting-info' lookup → /answer-info saves the answer as a GLOBAL
# rule (applies to ALL future customers) and hands n8n a hint to draft the reply.
def handle_ask_operator(payload, send):
    """POST /ask-operator — store an awaiting-info state for the chat and ask
    the operator the question instead of guessing. Fail-open (always 200)."""
    from db import _redis
    question = (payload.get("question") or "").strip()
    if not question:
        send(200, {"ok": False, "error": "question required"})
        return
    cid = (payload.get("customer_id") or "").strip()
    chat = str(payload.get("chat_id")
               or os.environ.get("ADMIN_CHAT_ID", "")).strip()
    cust_msg = (payload.get("customer_msg") or "").strip()
    name = (payload.get("customer_name") or "").strip()
    phone = (payload.get("customer_phone") or "").strip()
    who = name or phone or cid or "a customer"
    state = json.dumps({"customer_id": cid, "customer_phone": phone,
                        "customer_name": name, "question": question,
                        "customer_msg": cust_msg})
    try:
        _redis(["SET", "hermes:awaiting_info:" + chat, state, "EX", "86400"])
    except Exception as e:
        log("ask-operator redis err:", repr(e))
    msg = ("❓ *Hermes needs your input* — it won't guess.\n\n"
           f"*{who}* asked:\n_{(cust_msg[:300] or '(see chat)')}_\n\n"
           f"To answer correctly I need:\n*{question}*\n\n"
           "Reply to this with the answer — I'll draft it and "
           "remember it for next time (every future customer).")
    try:
        from server import _tg_post
        _tg_post("sendMessage", {"chat_id": int(chat), "text": msg,
                                 "parse_mode": "Markdown"})
    except Exception as e:
        log("ask-operator tg err:", repr(e))
    log(f"ask-operator cid={cid} q={question[:70]!r}")
    send(200, {"ok": True, "asked": True})


def handle_answer_info(payload, send):
    """POST /answer-info — operator answered a /ask-operator question. Clear the
    state, LEARN the answer as a global rule (all future customers), and return
    an operator_hint so n8n drafts this customer's reply. Fail-open."""
    from db import _redis
    from server import save_behavior_rule
    chat = str(payload.get("chat_id") or "").strip()
    answer = (payload.get("answer") or "").strip()
    cid = (payload.get("customer_id") or "").strip()
    question = (payload.get("question") or "").strip()
    if chat:
        try:
            _redis(["DEL", "hermes:awaiting_info:" + chat])
        except Exception:
            pass
    if not answer:
        send(200, {"ok": False, "error": "answer required"})
        return
    # Remember it for EVERY future customer (global rule). Keep the question so
    # the rule stays self-contained; the operator can refine/discard via /rules.
    rule_text = (f"{question.rstrip('?').strip()} → {answer}"
                 if question else answer)
    rid, err = save_behavior_rule(
        rule_text, "global", None, "operator_answer",
        "Operator answer to a Hermes ask-before-guess question")
    if rid is None:
        log("answer-info rule save FAILED:", err)
    else:
        log(f"answer-info learned rule id={rid}: {rule_text[:70]!r}")
        try:
            from server import _tg_post
            _tg_post("sendMessage", {
                "chat_id": int(chat or os.environ.get("ADMIN_CHAT_ID", "0")),
                "text": (f"✓ learned (rule #{rid}) — I'll use this for "
                         f"everyone from now on. /discardrule {rid} to undo."),
            })
        except Exception:
            pass
    operator_hint = (
        f"The customer asked: \"{question}\". Answer them clearly and warmly "
        f"using this exact information: {answer}"
        if question else f"Tell the customer: {answer}")
    send(200, {"ok": True, "rule_id": rid, "learned": rid is not None,
               "customer_id": cid, "operator_hint": operator_hint})

# (moved to routes.py — handle_<name>(payload, self._send))

def handle_rules(payload, send):
    from server import activate_rule, discard_rule, list_pending_rules
    action = (payload.get("action") or "list").strip().lower()
    if action == "list":
        rows, err = list_pending_rules()
        if rows is None:
            send(502, {"ok": False, "error": str(err)})
            return
        send(200, {"ok": True, "pending": rows, "count": len(rows)})
        return
    if action in ("activate", "discard"):
        fn = activate_rule if action == "activate" else discard_rule
        ok, err = fn(payload.get("rule_id"))
        if not ok:
            code = 400 if "integer" in str(err) else 404
            send(code, {"ok": False, "error": str(err)})
            return
        log(f"rule {action}d: id={payload.get('rule_id')}")
        send(200, {"ok": True, "action": action,
                         "rule_id": payload.get("rule_id")})
        return
    send(400, {"ok": False, "error": "action must be list|activate|discard"})

def handle_feedback(payload, send):
    """Operator /feedback command — classify, save, behavioural-context.

    Actions:
      classify             — Hermes classifies the free-form feedback;
                             proposal staged in Redis (TTL FEEDBACK_TTL).
      save                 — operator confirmed Yes; writes to
                             customer_notes / behavior_rules and applies
                             the active-rule cap.
      discard              — operator hit No; deletes the Redis proposal.
      behavioral-context   — drafts read this on every customer message;
                             returns {global, scenario, customer_notes}.

    Fail-safe — always returns 200 with an `ok` flag so a classifier or
    DB error never breaks the operator-confirmation card."""
    from server import (
        FEEDBACK_MAX_GLOBAL,
        FEEDBACK_MAX_PER_CUSTOMER,
        FEEDBACK_MAX_SCENARIO,
        FEEDBACK_TTL,
        behavioral_context,
        classify_feedback,
        feedback_apply_cap,
        resolve_customer_by_name,
    )
    action = (payload.get("action") or "").strip().lower()
    if action == "classify":
        text = (payload.get("text") or "").strip()
        if not text:
            send(200, {"ok": False, "error": "text is required"})
            return
        result = classify_feedback(text)
        if not result:
            log("feedback classify failed for:", text[:80])
            send(200, {"ok": False,
                             "error": "classifier did not return valid JSON"})
            return
        if result["classification"] == "CUSTOMER_NOTE":
            cid, matches = resolve_customer_by_name(result["customer_name"])
            result["customer_id"] = cid or ""
            result["matches"] = matches
            result["needs_disambiguation"] = (cid is None
                                              and len(matches) != 1)
        pid = str(uuid.uuid4())
        stored = dict(result)
        stored["raw"] = text
        _, err = _redis(["SET", "feedback:" + pid, json.dumps(stored),
                         "EX", str(FEEDBACK_TTL)])
        if err:
            log("feedback classify redis error:", err)
            send(200, {"ok": False, "error": "redis: " + err})
            return
        log("feedback classify -> %s proposal=%s"
            % (result["classification"], pid))
        send(200, {"ok": True, "proposal_id": pid, **result})
        return
    if action == "save":
        pid = (payload.get("proposal_id") or "").strip()
        if not pid:
            send(400, {"ok": False, "error": "proposal_id required"})
            return
        out, err = _redis(["GET", "feedback:" + pid])
        if err:
            send(502, {"ok": False, "error": "redis: " + err})
            return
        raw = (out or "").strip()
        if not raw:
            send(200, {"ok": False,
                             "error": "proposal not found or expired"})
            return
        try:
            p = json.loads(raw)
        except Exception:
            send(200, {"ok": False, "error": "bad proposal data"})
            return
        cls = p.get("classification")
        if cls == "CUSTOMER_NOTE":
            cid = (p.get("customer_id") or "").strip()
            if not cid:
                send(200, {"ok": False,
                                 "error": "no customer_id resolved",
                                 "matches": p.get("matches", [])})
                return
            note = (p.get("text") or "").strip()
            sql = ("INSERT INTO customer_notes (customer_id, note_text) "
                   "VALUES (" + _lit(cid) + ", " + _lit(note)
                   + ") RETURNING id")
            rid_out, err2 = _psql(sql)
            if err2:
                send(502, {"ok": False, "error": "db: " + err2})
                return
            rid = ((rid_out or "").strip().splitlines() or [""])[0]
            feedback_apply_cap("customer_notes",
                               FEEDBACK_MAX_PER_CUSTOMER,
                               customer_id=cid)
            _redis(["DEL", "feedback:" + pid])
            log("feedback SAVED CUSTOMER_NOTE id=%s cid=%s" % (rid, cid))
            send(200, {"ok": True, "table": "customer_notes",
                             "row_id": rid, "customer_id": cid,
                             "classification": cls,
                             "summary": p.get("summary", "")})
            return
        if cls == "GLOBAL_RULE":
            rule = (p.get("text") or "").strip()
            sql = ("INSERT INTO behavior_rules "
                   "(scope, rule_text, created_via, active) VALUES ("
                   + _lit("global") + ", " + _lit(rule) + ", "
                   + _lit("feedback") + ", true) RETURNING id")
            rid_out, err2 = _psql(sql)
            if err2:
                send(502, {"ok": False, "error": "db: " + err2})
                return
            rid = ((rid_out or "").strip().splitlines() or [""])[0]
            feedback_apply_cap("behavior_rules", FEEDBACK_MAX_GLOBAL,
                               scope="global")
            _redis(["DEL", "feedback:" + pid])
            log("feedback SAVED GLOBAL_RULE id=%s" % rid)
            send(200, {"ok": True, "table": "behavior_rules",
                             "row_id": rid, "classification": cls,
                             "summary": p.get("summary", "")})
            return
        if cls == "SCENARIO_RULE":
            scenario = (p.get("scenario") or "").strip()
            if not scenario:
                send(200, {"ok": False,
                                 "error": "no scenario in proposal"})
                return
            rule = (p.get("text") or "").strip()
            sql = ("INSERT INTO behavior_rules "
                   "(scope, scope_value, rule_text, created_via, active) "
                   "VALUES (" + _lit("scenario") + ", " + _lit(scenario)
                   + ", " + _lit(rule) + ", " + _lit("feedback")
                   + ", true) RETURNING id")
            rid_out, err2 = _psql(sql)
            if err2:
                send(502, {"ok": False, "error": "db: " + err2})
                return
            rid = ((rid_out or "").strip().splitlines() or [""])[0]
            feedback_apply_cap("behavior_rules", FEEDBACK_MAX_SCENARIO,
                               scope="scenario", scope_value=scenario)
            _redis(["DEL", "feedback:" + pid])
            log("feedback SAVED SCENARIO_RULE id=%s scenario=%s"
                % (rid, scenario))
            send(200, {"ok": True, "table": "behavior_rules",
                             "row_id": rid, "scenario": scenario,
                             "classification": cls,
                             "summary": p.get("summary", "")})
            return
        send(200, {"ok": False,
                         "error": "unknown classification: " + str(cls)})
        return
    if action == "discard":
        pid = (payload.get("proposal_id") or "").strip()
        if not pid:
            send(400, {"ok": False, "error": "proposal_id required"})
            return
        _redis(["DEL", "feedback:" + pid])
        log("feedback discarded %s" % pid)
        send(200, {"ok": True})
        return
    if action == "behavioral-context":
        cid = (payload.get("customer_id") or "").strip()
        ctx = behavioral_context(cid)
        send(200, {"ok": True, **ctx})
        return
    send(400, {"ok": False,
                     "error": "action must be "
                              "classify|save|discard|behavioral-context"})

# --- Pipeline Review handlers ------------------------------------------
# See docs/pipeline-review-plan.md §2a, §2b. Both fail-open (always 200).

# (moved to routes.py — module-level resolve_target / update_last_analysis)
# (moved to routes.py — handle_<name>(payload, self._send))


# ============================================================================
# Helper: Telegram admin notifications (fire directly from bridge,
# bypassing n8n). Used by webhook handlers that need to push messages
# without going through the workflow.
# ============================================================================

def _tg_send(text, parse_mode="HTML"):
    """Fire one Telegram message to the operator chat. Same pattern as
    cron-reminders.py / cron-daily-summary.py: ADMIN_TG_TOKEN +
    ADMIN_CHAT_ID from .env. Fail-safe — logs on error, never raises.
    Returns True on HTTP 2xx, False otherwise."""
    token = os.environ.get("ADMIN_TG_TOKEN", "")
    chat_id = os.environ.get("ADMIN_CHAT_ID", "")
    if not (token and chat_id):
        log("_tg_send: ADMIN_TG_TOKEN / ADMIN_CHAT_ID not configured")
        return False
    payload = json.dumps({
        "chat_id": int(chat_id),
        "text": text,
        "parse_mode": parse_mode,
    }).encode("utf-8")
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=8) as r:
            return 200 <= r.status < 300
    except Exception as e:
        log(f"_tg_send err: {e!r}")
        return False


# ============================================================================
# Group: webhooks — real-time event ingress from external services
# ============================================================================

def handle_nomod_webhook(headers, raw_body, send):
    """POST /nomod-webhook — Nomod payment-webhook receiver. Replaces
    the 2-min /poll-payments cron for real-time payment detection; the
    cron stays running as a fallback (same Redis dedup key —
    nomod_seen:<charge_id> — so they never double-notify).

    Signature verification (svix HMAC-SHA256, mandatory):
      Headers required: svix-id, svix-timestamp, svix-signature.
      Timestamp must be within ±5 min of now (replay-attack window).
      Signed string: '<svix-id>.<svix-timestamp>.<raw_body>'.
      Key: base64-decode(NOMOD_WEBHOOK_SECRET stripped of 'whsec_').
      Expected sig = base64(HMAC-SHA256(key, signed_string)).
      Compare with each 'v1,<sig>' token in svix-signature (multiple
      sigs allowed during key rotation; any match passes).

    On valid signature: send 200 IMMEDIATELY, then handoff to a
    background thread for processing. Nomod retries slow responses;
    sub-100ms ACK is the goal. _send already flushes the wfile
    (server.py:_send) so 200 hits the socket before this returns."""
    import base64
    import hashlib
    import hmac
    import threading
    from server import NOMOD_WEBHOOK_SECRET

    # --- 1. svix headers present? ---
    svix_id = (headers.get("svix-id")
               or headers.get("Svix-Id") or "")
    svix_ts = (headers.get("svix-timestamp")
               or headers.get("Svix-Timestamp") or "")
    svix_sig = (headers.get("svix-signature")
                or headers.get("Svix-Signature") or "")
    if not (svix_id and svix_ts and svix_sig):
        log("nomod-webhook missing svix headers")
        send(400, {"error": "missing svix headers"})
        return

    # --- 2. timestamp freshness ---
    try:
        ts_int = int(svix_ts)
    except ValueError:
        send(400, {"error": "invalid svix-timestamp"})
        return
    now = int(time.time())
    if abs(now - ts_int) > 300:
        log(f"nomod-webhook stale timestamp lag={now-ts_int}s")
        send(400, {"error": "stale timestamp"})
        return

    # --- 3. signature ---
    secret = (NOMOD_WEBHOOK_SECRET or "").strip()
    if not secret.startswith("whsec_"):
        log("nomod-webhook NOMOD_WEBHOOK_SECRET not configured "
            "(expected whsec_<base64>)")
        send(500, {"error": "webhook secret not configured"})
        return
    try:
        secret_bytes = base64.b64decode(secret[len("whsec_"):])
    except Exception as e:
        log(f"nomod-webhook secret decode err: {e!r}")
        send(500, {"error": "webhook secret malformed"})
        return
    signed_msg = f"{svix_id}.{svix_ts}.{raw_body}".encode("utf-8")
    expected_sig = base64.b64encode(
        hmac.new(secret_bytes, signed_msg, hashlib.sha256).digest()
    ).decode("ascii")
    presented_sigs = [t[3:] for t in svix_sig.split(" ")
                      if t.startswith("v1,")]
    if not any(hmac.compare_digest(expected_sig, s)
               for s in presented_sigs):
        log(f"nomod-webhook BAD signature event_id={svix_id}")
        send(400, {"error": "bad signature"})
        return

    # --- 4. 200 OK now, processing in background ---
    send(200, {"ok": True})

    # Hand processing to a daemon thread so the response is fully out
    # the door before we touch DB / WAHA / Telegram. _send already
    # flushed (server.py change in this commit) so the 200 should
    # already be on the wire by the time threading.Thread().start()
    # returns; this is belt + braces.
    threading.Thread(
        target=_process_nomod_charge_completed,
        args=(raw_body, svix_id),
        daemon=True,
        name=f"nomod-process-{svix_id[:8]}",
    ).start()


def _process_nomod_charge_completed(raw_body, svix_id):
    """Background worker for handle_nomod_webhook — runs AFTER the
    200 OK has been flushed to Nomod. All DB writes, label transitions,
    Telegram notifications, and CRM-trail notes happen here. Broad
    try/except so a worker error never crashes the bridge.

    3-layer customer match (same priority ladder as handle_poll_payments):
      1. link_id — charge.link.id ↔ autonomous_sends.payment_link_sent.
         Most reliable: maps the charge to the customer we ORIGINALLY
         sent the link to, regardless of who actually paid (forwarded
         link, friend pays for friend, etc.).
      2. phone — payer phone resolves to a customer_facts row.
      3. amount + time — autonomous_sends.payment_link_sent within
         ±1 AED and ±30min/+5min of charge.created (last resort).

    Payer-vs-customer mismatch (the 'Musawi paid for Qurbani' case):
    when matched via link_id or amount_time AND payer phone/name
    differs from the customer's known phone:
      - Promote to CONFIRMED anyway (booking IS paid)
      - Insert a row into customer_notes with full payer info so the
        operator can act/save in CRM without digging into JSON
      - Telegram shows ⚠️ + both parties' names

    Schema for autonomous_sends.notes matches handle_poll_payments
    exactly (snake_case + link_id + payer_mismatch fields).
    """
    try:
        import datetime as _dt
        from server import (
            _payer_mismatch,
            apply_label_transition,
            get_current_label_row,
            resolve_customer_by_phone,
        )

        try:
            body = json.loads(raw_body or "{}")
        except json.JSONDecodeError:
            log("nomod-webhook valid sig but invalid JSON body")
            return

        event_type = body.get("type") or body.get("eventType") or ""
        if event_type != "charge.completed":
            log(f"nomod-webhook ignored event_type={event_type!r}")
            return

        # Dedup on Svix event id (handles Nomod retries).
        _o, _err = _redis(["SET", f"nomod_webhook_seen:{svix_id}", "1",
                           "EX", str(30 * 24 * 3600), "NX"])
        if not _o or "OK" not in str(_o):
            log(f"nomod-webhook event dedup HIT svix_id={svix_id}")
            return

        data = body.get("data") or {}
        charge_id = (data.get("id") or "").strip()

        # Cross-system dedup with polling cron (same key prefix).
        if charge_id:
            _o2, _ = _redis(["SET", f"nomod_seen:{charge_id}", "1",
                             "EX", str(30 * 24 * 3600), "NX"])
            if not _o2 or "OK" not in str(_o2):
                log(f"nomod-webhook charge dedup HIT — polling cron "
                    f"already handled charge_id={charge_id}")
                return

        # Extract fields (camelCase from Nomod webhook; tolerate
        # snake_case too in case the API ever returns the polling shape).
        customer = data.get("customer") or {}
        payer_phone = (customer.get("phoneNumber")
                       or customer.get("phone_number") or "").strip()
        try:
            total_f = float(data.get("total") or 0)
        except (TypeError, ValueError):
            total_f = 0.0
        method = (data.get("paymentMethod")
                  or data.get("payment_method") or "unknown")
        items = data.get("items") or []
        summary = ""
        if items and isinstance(items[0], dict):
            summary = items[0].get("name", "") or ""
        currency = data.get("currency") or "AED"
        created_at = (data.get("created")
                      or data.get("createdAt") or "")

        # Link reference — Nomod's webhook charge mirrors the polling
        # API shape: data.link.id IS the link_id we stored when
        # nomod_create_link returned. Drives Layer 1 of the
        # match-priority ladder (most reliable: maps charge ↔ the
        # customer we originally sent the link to).
        link_obj = data.get("link") or {}
        link_id = (link_obj.get("id")
                   if isinstance(link_obj, dict) else "") or ""

        # ── 3-layer customer match ──
        customer_id = ""
        customer_name = ""
        matched_via = ""

        # Layer 1 — link_id (PRIMARY). Maps charge → original recipient
        # of the payment link, regardless of who actually paid.
        if link_id:
            try:
                lid_esc = link_id.replace("'", "''")
                out, _err = _psql(
                    "SELECT customer_id FROM autonomous_sends "
                    "WHERE kind = 'payment_link_sent' "
                    f"AND notes->>'link_id' = '{lid_esc}' "
                    "ORDER BY id DESC LIMIT 1"
                )
                row_line = (out or "").strip().splitlines()
                if row_line and row_line[0].strip():
                    customer_id = row_line[0].strip()
                    matched_via = "link_id"
            except Exception as e:
                log(f"nomod-webhook link_id match err: {e!r}")

        # Layer 2 — phone (FALLBACK). The payer IS the customer.
        if not customer_id and payer_phone:
            try:
                resolved, _matches = resolve_customer_by_phone(payer_phone)
                if resolved:
                    customer_id = resolved
                    matched_via = "phone_webhook"
            except Exception as e:
                log(f"nomod-webhook resolve_customer_by_phone err: "
                    f"{e!r}")

        # Layer 3 — amount + time fuzzy (LAST RESORT). No link_id,
        # no phone match, but a payment_link_sent row matches amount
        # within ±1 AED and time within charge_created ±30min/+5min.
        if not customer_id and total_f > 0:
            try:
                if created_at:
                    created_dt = _dt.datetime.fromisoformat(
                        created_at.replace("Z", "+00:00"))
                else:
                    created_dt = _dt.datetime.now(_dt.timezone.utc)
                created_iso = created_dt.isoformat()
                out, _err = _psql(
                    "SELECT customer_id FROM autonomous_sends "
                    "WHERE kind = 'payment_link_sent' "
                    f"AND ABS((notes->>'amount')::numeric - {total_f}) < 1 "
                    f"AND sent_at BETWEEN "
                    f"  '{created_iso}'::timestamptz "
                    f"  - interval '30 minutes' "
                    f"AND '{created_iso}'::timestamptz "
                    f"  + interval '5 minutes' "
                    "LIMIT 2"
                )
                lines = (out or "").strip().splitlines()
                if len(lines) == 1 and lines[0].strip():
                    customer_id = lines[0].strip()
                    matched_via = "amount_time"
            except Exception as e:
                log(f"nomod-webhook amount_time match err: {e!r}")

        # ── Payer-vs-customer mismatch detection ──
        # Only meaningful when matched via link_id or amount_time
        # (phone-match implies the payer IS the customer).
        pay_mismatch = False
        row = None
        if customer_id and matched_via in ("link_id", "amount_time"):
            try:
                row = get_current_label_row(customer_id) or {}
                pay_mismatch = _payer_mismatch(
                    {"phone_number": payer_phone}, row, None)
            except Exception as e:
                log(f"nomod-webhook payer_mismatch detect err: {e!r}")

        # ── Promote matched customer to CONFIRMED ──
        if customer_id:
            try:
                from server import is_real_deposit as _is_real_deposit
                if row is None:
                    row = get_current_label_row(customer_id) or {}
                customer_name = row.get("name") or ""
                prev_label = row.get("label") or "NEW"
                if not _is_real_deposit(total_f):
                    # Below deposit threshold — log only, keep label.
                    # Same policy as poll-payments path. Operator still
                    # gets the Telegram notification so they know a
                    # test payment landed; just no auto-confirm.
                    log(f"nomod-webhook cid={customer_id!r} "
                        f"BELOW deposit threshold AED "
                        f"{total_f:.2f} — logged only, "
                        f"label unchanged ({prev_label})")
                elif prev_label != "CONFIRMED":
                    apply_label_transition(
                        customer_id, prev_label, "CONFIRMED",
                        f"nomod-webhook:payment_received:{matched_via}",
                        f"charge {charge_id[:8]} via webhook "
                        f"(matched_via={matched_via}, "
                        f"total=AED{total_f:.0f}"
                        + (", payer_mismatch=True" if pay_mismatch else "")
                        + ")",
                        row.get("message_count", 0),
                        created_by="system:webhook")
                    log(f"nomod-webhook cid={customer_id!r} "
                        f"{prev_label} -> CONFIRMED "
                        f"matched_via={matched_via} "
                        f"payer_mismatch={pay_mismatch}")
            except Exception as e:
                log(f"nomod-webhook promote err: {e!r}")

        # ── customer_notes for CRM trail (payer-mismatch only) ──
        # Preserves payer info inline so operator can act/save in CRM
        # without digging into autonomous_sends JSON.
        if pay_mismatch and customer_id:
            payer_name_full = " ".join([
                (customer.get("firstName")
                 or customer.get("first_name") or "").strip(),
                (customer.get("lastName")
                 or customer.get("last_name") or "").strip(),
            ]).strip()
            payer_email = (customer.get("email") or "").strip()
            note_text = (
                f"💰 Payment of AED {total_f:g} received from "
                f"{payer_name_full or '(no name)'}"
                + (f" ({payer_phone}" if payer_phone else " (")
                + (f" / {payer_email}" if payer_email else "")
                + f"). Paid on behalf of customer "
                f"(link matched via {matched_via}, "
                f"charge {charge_id[:8] or '?'})."
            )
            try:
                cid_e = customer_id.replace("'", "''")
                _psql(
                    "INSERT INTO customer_notes "
                    "(customer_id, note_text, active) "
                    f"VALUES ('{cid_e}', {_lit(note_text)}, true)"
                )
                log(f"nomod-webhook customer_notes row inserted "
                    f"cid={customer_id} (payer-mismatch trail)")
            except Exception as e:
                log(f"nomod-webhook customer_notes insert err: {e!r}")

        # ── Audit row in autonomous_sends — same schema as polling ──
        kind = ("payment_received" if customer_id
                else "payment_received_unmatched")
        notes = {
            "charge_id": charge_id,
            "link_id": link_id,
            "matched_via": matched_via or "unmatched",
            "total": total_f,
            "currency": currency,
            "payment_method": method,
            "created_at": created_at,
            "payer_info": {
                "first_name": (customer.get("firstName")
                               or customer.get("first_name")
                               or "").strip(),
                "last_name": (customer.get("lastName")
                              or customer.get("last_name")
                              or "").strip(),
                "email": (customer.get("email") or "").strip(),
                "phone_number": payer_phone,
            },
            "payer_mismatch": pay_mismatch,
            "summary": summary,
            "via": "webhook",
            "event_id": svix_id,
        }
        try:
            sql = (
                "INSERT INTO autonomous_sends (customer_id, kind, notes) "
                f"VALUES ({_lit(customer_id or 'unknown')}, "
                f"{_lit(kind)}, "
                f"{_lit(json.dumps(notes))}::jsonb)"
            )
            _psql(sql)
        except Exception as e:
            log(f"nomod-webhook autonomous_sends insert err: {e!r}")

        # ── Telegram notification — three branches ──
        try:
            payer_full = " ".join([
                (customer.get("firstName")
                 or customer.get("first_name") or "").strip(),
                (customer.get("lastName")
                 or customer.get("last_name") or "").strip(),
            ]).strip()
            payer_email = (customer.get("email") or "").strip()

            if customer_id and not pay_mismatch:
                # Clean match: payer is the customer (or matched without
                # needing the payer-mismatch detection).
                display = (customer_name or payer_phone
                           or customer_id or "(unknown)")
                text = (
                    f"💰 <b>PAYMENT RECEIVED</b>\n"
                    f"Customer: <b>{display}</b>\n"
                    f"Amount: AED {total_f:g}\n"
                    f"Method: {method}\n"
                    f"Booking: {summary or '(no summary)'}\n"
                    f"→ promoted to ✅ CONFIRMED  "
                    f"<i>(via {matched_via} · "
                    f"{charge_id[:8] or 'no_id'})</i>"
                )
            elif customer_id and pay_mismatch:
                # The Qurbani/Musawi case: link maps to customer, but
                # payer is someone else. Confirm booking + surface
                # payer info so operator can link both in CRM.
                text = (
                    f"💰 <b>PAYMENT RECEIVED</b>  "
                    f"⚠️ payer ≠ customer\n"
                    f"Customer: <b>{customer_name or customer_id}</b>\n"
                    f"Paid by: <b>{payer_full or '(no name)'}</b> · "
                    f"{payer_phone or '(no phone)'}"
                    + (f" · {payer_email}" if payer_email else "")
                    + f"\nAmount: AED {total_f:g}\n"
                    f"Method: {method}\n"
                    f"Booking: {summary or '(no summary)'}\n"
                    f"→ promoted to ✅ CONFIRMED  "
                    f"<i>(via {matched_via} · {charge_id[:8]})</i>\n\n"
                    f"📋 Note saved to customer file: "
                    f"payer info preserved for CRM."
                )
            else:
                # Truly unmatched (no link, no phone, no amount/time hit).
                text = (
                    f"💰 <b>PAYMENT RECEIVED</b> (unmatched)\n"
                    f"Amount: AED {total_f:g}\n"
                    f"Method: {method}\n"
                    f"Time: {created_at or '?'}\n"
                    f"From: {payer_full or '(no name)'} · "
                    f"{payer_phone or '(no phone)'}\n"
                    f"Reference: <code>"
                    f"{charge_id[:8] or '?'}...</code>\n\n"
                    f"Check Nomod dashboard to identify customer + run "
                    f"<code>/label name CONFIRMED</code> manually."
                )
            _tg_send(text, parse_mode="HTML")
        except Exception as e:
            log(f"nomod-webhook telegram notify err: {e!r}")
    except Exception as e:
        # Anything unhandled — never crash the daemon thread.
        log(f"nomod-webhook worker EXC: {e!r}")


# ============================================================================
# Group: file sending (WAHA media — Drive-URL pass-through)
# ============================================================================
#
# File registry: docs/file-registry.md holds 85+ pre-curated Google
# Drive shareable links (yacht brochures, catering menus, route maps,
# itineraries, etc.). We parse it at startup into FILE_REGISTRY
# {key: drive_share_url}, then on every send convert the share URL to
# Drive's direct-download URL so WAHA can fetch raw bytes (the /view
# share URL returns HTML preview, not the file).
#
# Hermes detects file-need via system-prompt rules and emits
# should_send_file + file_key in its JSON output. The workflow card
# builder adds a [📎 Send File] button; operator taps it; n8n posts
# {customer_id, file_key, caption} to /send-file. Bridge fetches Drive
# URL via WAHA (RemoteFile path — no /tmp download), logs to
# autonomous_sends.

import re as _re
import mimetypes  # noqa: F401 — used by extension-based mime guess

REGISTRY_PATH = os.environ.get(
    "FILE_REGISTRY_PATH",
    os.path.expanduser("~/hermes-bridge/file-registry.md"))

# Parsed at first call; cached for lifetime of process. The registry
# is small (~10KB) so we don't bother with a TTL — operator restarts
# the bridge if they update the file.
_FILE_REGISTRY_CACHE = None
_FILE_REGISTRY_BY_CATEGORY = None  # {category: [keys]}

# Drive's /view?usp=sharing returns HTML; need the
# /uc?export=download&id=<ID> form for raw bytes.
_DRIVE_FILE_ID_RE = _re.compile(
    r"/file/d/([A-Za-z0-9_-]+)/")

# WhatsApp's document attachment ceiling is 100MB. Anything bigger
# fails at WhatsApp-API level; we pre-check via HEAD so the operator
# sees a clear error in Telegram instead of an opaque WAHA failure
# minutes later.
MAX_FILE_BYTES = 100 * 1024 * 1024


def _load_file_registry():
    """Parse docs/file-registry.md into {key: drive_share_url}. Also
    builds _FILE_REGISTRY_BY_CATEGORY for grouped /list-files output.
    Idempotent + cached."""
    global _FILE_REGISTRY_CACHE, _FILE_REGISTRY_BY_CATEGORY
    if _FILE_REGISTRY_CACHE is not None:
        return _FILE_REGISTRY_CACHE
    registry = {}
    by_cat = {}
    current_cat = "uncategorized"
    try:
        with open(REGISTRY_PATH, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.rstrip("\n")
                stripped = line.strip()
                # Category headers
                if stripped.startswith("## "):
                    current_cat = stripped[3:].strip()
                    by_cat.setdefault(current_cat, [])
                    continue
                if not stripped or stripped.startswith("#"):
                    continue
                # key: https://...
                m = _re.match(r"^([a-z0-9_-]+):\s*(https?://\S+)\s*$",
                              stripped)
                if not m:
                    continue
                key = m.group(1).strip()
                url = m.group(2).strip()
                registry[key] = url
                by_cat.setdefault(current_cat, []).append(key)
    except FileNotFoundError:
        log(f"file registry not found at {REGISTRY_PATH} — "
            f"/send-file will return errors")
    except Exception as e:
        log(f"file registry parse error: {e!r}")
    _FILE_REGISTRY_CACHE = registry
    _FILE_REGISTRY_BY_CATEGORY = by_cat
    log(f"file registry: {len(registry)} entries loaded from "
        f"{REGISTRY_PATH}")
    return registry


def _drive_url_to_direct(share_url):
    """Convert a Drive share URL to the direct-download URL WAHA can
    fetch as raw bytes. Returns (direct_url, file_id) or (None, None)
    if the URL doesn't look like a Drive share link.

    `confirm=t` bypasses the Drive 'virus scan warning' interstitial
    HTML page that Drive serves for files >~25MB on the standard
    /uc?export=download path. Without it, WAHA would fetch the HTML
    warning page instead of the file bytes and the send would fail."""
    m = _DRIVE_FILE_ID_RE.search(share_url or "")
    if not m:
        return None, None
    file_id = m.group(1)
    return (
        f"https://drive.google.com/uc?export=download&id={file_id}"
        f"&confirm=t",
        file_id)


def _head_file_size(url, timeout=15):
    """HEAD the given URL and return (size_bytes_or_None, err_or_None).
    Drive sometimes returns Content-Length, sometimes doesn't (e.g.
    when it would redirect to a confirmation page). When size can't
    be determined, return (None, None) and let the caller decide
    whether to proceed."""
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            cl = r.headers.get("Content-Length")
            if cl and cl.isdigit():
                return int(cl), None
            return None, None
    except urllib.error.HTTPError as e:
        # Some Drive variants 403/302 on HEAD but allow GET fine.
        # Treat as "size unknown" rather than failure — WAHA will
        # surface a real error if the GET also fails.
        return None, f"HEAD HTTP {e.code}"
    except Exception as e:
        return None, f"HEAD failed: {e!r}"


def _mime_for_key(file_key):
    """Best-effort mime detection from a registry key. Keys ending in
    -video / containing 'video' get mp4; everything else defaults to
    application/pdf (the registry is mostly PDFs)."""
    k = (file_key or "").lower()
    if "video" in k or k.endswith("-mp4"):
        return "video/mp4"
    return "application/pdf"


def _is_video_mime(mime):
    return (mime or "").startswith("video/")


def _is_image_mime(mime):
    return (mime or "").startswith("image/")


def _filename_for_key(file_key, mime):
    """e.g. 'sunseeker-satoshi-70' + 'application/pdf' ->
    'sunseeker-satoshi-70.pdf'."""
    ext = {
        "application/pdf": ".pdf",
        "video/mp4": ".mp4",
        "image/jpeg": ".jpg",
        "image/png": ".png",
    }.get(mime, "")
    return f"{file_key}{ext}"


def handle_send_file(payload, send):
    """POST /send-file — send a registered Google Drive file to a
    WhatsApp customer via WAHA (RemoteFile URL pass-through, no
    /tmp staging).

    Body: {customer_id, file_key, caption?}
      file_key matches a key in docs/file-registry.md

    Returns {ok, error?, file_key, filename, file_url?, drive_id?}.
    Logs the send to autonomous_sends (kind='file_sent') for audit
    and Hermes deduplication ('did we already send this file?').
    """
    from server import canonicalize_cid
    from waha import waha_send_file, waha_send_image, waha_send_text
    cid = canonicalize_cid(
        (payload.get("customer_id") or "").strip())
    if not cid:
        send(200, {"ok": False, "error": "customer_id required"})
        return
    file_key = (payload.get("file_key") or "").strip()
    caption = (payload.get("caption") or "").strip()
    if not file_key:
        send(200, {"ok": False, "error": "file_key required"})
        return

    # Idempotency guard — production 2026-05-28: under box load the
    # [📎 Send File] button is slow, the operator taps it repeatedly,
    # and each tap fired a separate WAHA send → the customer received the
    # file 2-3 times. Claim a short-lived per-(cid,file_key) Redis lock
    # (SET NX); only the first tap within the window proceeds. redis-cli
    # SET..NX prints 'OK' when it set the key, empty when it already
    # existed. Fail-OPEN: any Redis hiccup proceeds (better one send than
    # a blocked send).
    try:
        _claim, _cerr = _redis(["SET", "filesend:" + cid + ":" + file_key,
                                "1", "NX", "EX", "60"])
        if not _cerr and str(_claim or "").strip().upper() != "OK":
            log(f"send-file DEDUP skip cid={cid} key={file_key!r} "
                f"— repeat tap within 60s, ignored")
            send(200, {"ok": True, "deduped": True, "file_key": file_key,
                       "action_taken": "already sent moments ago "
                       "(ignored a repeat tap)"})
            return
    except Exception as _de:
        log(f"send-file dedup check failed, proceeding: {_de!r}")

    registry = _load_file_registry()
    share_url = registry.get(file_key)
    if not share_url:
        # Be helpful — surface a few near-matches
        suggestions = [k for k in registry
                       if file_key.lower() in k.lower()
                       or k.lower() in file_key.lower()][:5]
        send(200, {
            "ok": False,
            "error": f"file_key {file_key!r} not in registry",
            "suggestions": suggestions,
        })
        return

    direct_url, drive_id = _drive_url_to_direct(share_url)
    if not direct_url:
        send(200, {
            "ok": False,
            "error": f"registry url for {file_key!r} doesn't look like "
                     f"a Drive share link: {share_url[:80]}",
        })
        return

    # Size pre-check (best-effort). WhatsApp document ceiling is
    # 100MB; if Drive reveals a larger size on HEAD, abort early
    # with a clear error so the operator doesn't wait for WAHA to
    # silently fail. When HEAD can't determine size (Drive often
    # 302s/403s on HEAD), proceed and let WAHA enforce.
    file_size, _herr = _head_file_size(direct_url)
    if file_size is not None and file_size > MAX_FILE_BYTES:
        send(200, {
            "ok": False,
            "error": (f"file too large: {file_size / 1024 / 1024:.1f}MB "
                      f"exceeds WhatsApp's 100MB document limit"),
            "file_key": file_key,
            "file_size_bytes": file_size,
            "max_bytes": MAX_FILE_BYTES,
        })
        log(f"send-file ABORT (oversize) cid={cid} key={file_key!r} "
            f"size={file_size}")
        return

    mime = _mime_for_key(file_key)
    filename = _filename_for_key(file_key, mime)
    # Image-mime → /api/sendImage path so it renders inline in
    # WhatsApp. Video and PDF go through /api/sendFile (which still
    # produces native WhatsApp document/video previews).
    is_image = _is_image_mime(mime)

    if is_image:
        ok, err = waha_send_image(cid, direct_url, caption)
    else:
        ok, err = waha_send_file(cid, direct_url, caption, filename)

    # ━━━ CORE-tier fallback ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # WAHA's CORE (free) tier returns HTTP 422 with a "Plus version"
    # message for any media send — regardless of engine (WEBJS or
    # NOWEB). When this fires, degrade gracefully by sending the
    # customer a regular text message containing the Drive share URL
    # (which IS supported on CORE via /api/sendText). Customer still
    # gets the file — just one tap further than a native attachment.
    # When the operator later upgrades to WAHA Plus, this fallback
    # automatically goes dormant because the media send succeeds first.
    fallback_used = False
    if not ok and err and (
            "Plus version" in err or "422" in err):
        # Build a friendly link-message in Maria's voice. We use the
        # Drive SHARE URL (not the direct-download form) because the
        # share URL opens in Drive's mobile preview, which renders
        # PDFs natively on iOS and Android. share_url already comes
        # from registry.get(file_key) at the top of this handler.
        link_msg_parts = []
        if caption:
            link_msg_parts.append(caption.strip())
        else:
            # Auto-caption from file_key
            link_msg_parts.append(f"here's the {file_key.replace('-', ' ')} 📎")
        link_msg_parts.append(share_url)
        link_msg = "\n\n".join(link_msg_parts)
        log(f"send-file FALLBACK cid={cid} key={file_key!r} "
            f"(CORE-tier 422) → sending as text link")
        fb_ok, fb_err = waha_send_text(cid, link_msg)
        if fb_ok:
            ok = True
            err = None
            fallback_used = True
        else:
            # Both paths failed — surface the original WAHA error.
            log(f"send-file FALLBACK FAILED cid={cid} "
                f"sendText err={fb_err!r}")

    if not ok:
        log(f"send-file FAIL cid={cid} key={file_key!r} "
            f"drive_id={drive_id} err={err!r}")
        send(200, {
            "ok": False, "error": err,
            "file_key": file_key, "file_url": direct_url,
        })
        return

    # Log to autonomous_sends so Hermes can see we've already sent
    # this file (dedup via files_sent in customer-facts). The kind
    # distinguishes native attachments from link-fallback so we can
    # measure how often the fallback fires. notes is jsonb — must be
    # valid JSON cast to ::jsonb, not a plain string.
    try:
        kind = "file_sent_link" if fallback_used else "file_sent"
        notes_obj = {
            "file_key": file_key,
            "filename": filename,
            "caption": caption[:200],
            "fallback_used": fallback_used,
        }
        _psql(
            "INSERT INTO autonomous_sends (customer_id, kind, notes) "
            f"VALUES ({_lit(cid)}, {_lit(kind)}, "
            f"{_lit(json.dumps(notes_obj))}::jsonb)")
    except Exception as _e:
        log(f"send-file autonomous_log err: {_e!r}")

    log(f"send-file OK cid={cid} key={file_key!r} "
        f"mime={mime} fallback={fallback_used} "
        f"caption={(caption or '')[:40]!r}")
    # A file just went out → the conversation advanced. Close any open
    # draft cards for this customer so stale cards (often offering the
    # very file we just sent) don't linger. Best-effort; never blocks
    # the send response. Fix 2026-06-02 (dup-draft-after-brochure).
    try:
        from server import _supersede_pending_for_cid
        _supersede_pending_for_cid(cid)
    except Exception as _se:
        log(f"send-file supersede err: {_se!r}")
    send(200, {
        "ok": True,
        "customer_id": cid,
        "file_key": file_key,
        "filename": filename,
        "file_url": direct_url,
        "drive_id": drive_id,
        "is_image": is_image,
        "is_video": _is_video_mime(mime),
        "fallback_used": fallback_used,
        "action_taken": (
            f"Sent {file_key} as "
            + ("text link (CORE tier)" if fallback_used
               else f"native {mime} attachment")
            + " to customer."),
    })


def handle_list_files(payload, send):
    """POST /list-files — return the registry as
    {category: [keys]} for operator browsing. Read-only."""
    registry = _load_file_registry()
    by_cat = _FILE_REGISTRY_BY_CATEGORY or {}
    # Telegram-renderable summary
    lines = [f"📁 *File registry* ({len(registry)} files):"]
    for cat, keys in by_cat.items():
        if not keys:
            continue
        lines.append("")
        lines.append(f"*{cat}*")
        for k in keys[:25]:
            lines.append(f"  • `{k}`")
        if len(keys) > 25:
            lines.append(f"  • _+ {len(keys) - 25} more_")
    send(200, {
        "ok": True,
        "count": len(registry),
        "by_category": by_cat,
        "telegram_text": "\n".join(lines)
        if registry
        else (f"📁 File registry empty — check that "
              f"`{REGISTRY_PATH}` exists on the bridge."),
    })
