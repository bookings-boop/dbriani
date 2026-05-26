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
    Actions: save | get | update | mark | latest-for-customer | drop.
    Always 200; ok flag carries the outcome."""
    from server import (
        _draft_drop,
        _draft_get,
        _draft_latest_for_customer,
        _draft_save,
        _draft_update,
    )
    action = (payload.get("action") or "").strip().lower()

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
        send(200, {"ok": True, "draft": d,
                         "found": d is not None, "error": err})
        return

    if action == "update":
        did = (payload.get("draft_id") or "").strip()
        d, err = _draft_update(did, payload.get("fields") or {})
        send(200, {"ok": d is not None, "draft": d, "error": err})
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

    send(200, {"ok": False,
                     "error": ("action must be save|get|update|mark|"
                               "latest-for-customer|drop")})


# ============================================================================
# Group: customer facts
# ============================================================================

def handle_customer_facts(payload, send):
    """feature-header: maintain customer_facts + return a context header.
    Fail-safe — ANY error degrades to cached facts and still returns 200,
    so the workflow's draft card is never blocked by header logic."""
    from server import (
        _facts_extract_gate,
        _merge_facts,
        build_customer_header,
        extract_customer_facts,
        get_customer_facts,
        upsert_customer_facts,
        waha_lookup_push_name,
    )
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
            send(200, {"ok": True, "extracted": False,
                             "message_count": mc, "customer_header": hdr})
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
        log(f"customer-facts cid={cid!r} extract={do_extract} msg#{mc}")
        send(200, {"ok": True, "extracted": bool(do_extract),
                         "message_count": mc, "customer_header": hdr})
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
        send(200, {"ok": True, "extracted": False, "degraded": True,
                         "message_count": mc, "customer_header": hdr})

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
        waha = waha_fetch_history(cid, limit=30)
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
        if not (merged.get("name") or "").strip() and waha["push_name"]:
            merged["name"] = waha["push_name"]
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

        # ── LAYER 2: phone → customer_id via DB then WAHA fallback ──
        if not customer_id:
            phone = _normalize_phone_digits(payer.get("phone_number"))
            if phone and len(phone) >= 7:
                tail = phone[-9:] if len(phone) >= 9 else phone
                out, _err = _psql(
                    "SELECT customer_id, COALESCE(name,'') FROM customer_facts "
                    f"WHERE customer_id LIKE '%{tail}@%' "
                    "ORDER BY updated_at DESC LIMIT 1"
                )
                line = (out or "").strip().splitlines()
                if line and "|" in line[0]:
                    parts = line[0].split("|", 1)
                    customer_id = parts[0].strip()
                    customer_name = parts[1].strip()
                    matched_via = "phone_db"
                elif waha_chats_cache:
                    # WAHA fallback — match the pushName-digits to phone tail
                    for ch in waha_chats_cache:
                        cid_str = ch.get("_serialized") or (
                            ch.get("id") or {}).get(
                            "_serialized") or ""
                        pn_digits = _normalize_phone_digits(
                            ch.get("name") or "")
                        if (pn_digits.endswith(tail)
                                or cid_str.startswith(phone + "@")):
                            customer_id = cid_str
                            matched_via = "phone_waha"
                            break

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
            # Promote to CONFIRMED (operator's terminal-state rule).
            try:
                cid_e = customer_id.replace("'", "''")
                prev_row = row or get_current_label_row(customer_id)
                prev_label = (prev_row or {}).get("label") or "NEW"
                if prev_label != "CONFIRMED":
                    apply_label_transition(
                        customer_id, prev_label, "CONFIRMED",
                        "poll-payments:payment_received",
                        f"charge {charge_id[:8]} matched_via={matched_via}",
                        (prev_row or {}).get("message_count", 0),
                        created_by="system")
                    log(f"poll-payments cid={customer_id!r} "
                        f"{prev_label} -> CONFIRMED matched_via={matched_via}")
            except Exception as e:
                log("poll-payments promote err:", repr(e))
            matched.append(entry)
        else:
            unmatched.append(entry)

    log(f"poll-payments scanned={scanned} matched={len(matched)} "
        f"unmatched={len(unmatched)} dedup_skipped={dedup_skipped}")
    send(200, {
        "ok": True,
        "scanned": scanned,
        "dedup_skipped": dedup_skipped,
        "matched": matched,
        "unmatched": unmatched,
    })


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
    """POST /info — operator-readable single-lead summary. Returns
    markdown text shaped for human reading (no raw signal names, no
    internal IDs unless useful). Fail-open, always 200."""
    from server import (
        _fmt_dur,
        _humanize_signal,
        _name_fallback,
        get_current_label_row,
        waha_fetch_history,
    )
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

        # Last 5 customer messages from WAHA — gives operator the actual
        # quotes so they can decide quickly without opening WhatsApp.
        try:
            waha = waha_fetch_history(cid, limit=20)
            hist_lines = (waha.get("history") or "").split("\n")
            cust_lines = [ln for ln in hist_lines
                          if ln.startswith("Customer ")]
            if cust_lines:
                lines.append("\n   💬 *Last customer messages:*")
                for ln in cust_lines[-5:]:
                    # Normalize to: "      • (1h ago) "text"
                    # Source format: 'Customer (1h ago): "body"'
                    try:
                        # WAHA format: 'Customer (1h ago): "body"'.
                        # ts already includes "ago", don't append it.
                        ts = ln.split("(", 1)[1].split(")", 1)[0]
                        body = ln.split('"', 1)[1].rstrip('"')
                        lines.append(f"      • _({ts})_ \"{body[:200]}\"")
                    except (IndexError, ValueError):
                        lines.append(f"      • {ln[:240]}")
        except Exception as _e:
            log("info WAHA history err:", repr(_e))

        # notes
        if notes:
            lines.append("\n   📝 *Notes:*")
            for note_text, dt in notes:
                lines.append(f"      • {note_text}  _({dt})_")

        # human-readable timeline
        if timeline:
            lines.append("\n   📅 *Recent activity:*")
            for sig, ev, dt, by in timeline:
                pretty = _humanize_signal(sig, ev, by)
                if pretty:
                    lines.append(f"      • {pretty}  _({dt})_")

        send(200, {
            "ok": True, "customer_id": cid,
            "telegram_text": "\n".join(lines),
        })
    except Exception as e:
        log("info ERROR:", repr(e))
        send(200, {"ok": False, "degraded": True, "error": str(e),
                         "telegram_text": "⚠️ Info failed — bridge error."})

# (moved to routes.py — handle_<name>(payload, self._send))

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
        # Build the prompt — mirrors _draft() but with the directive
        # injected as the incoming_message context.
        inner = {
            "customer_id": cid,
            "customer_name": payload.get("customer_name") or name,
            "incoming_message": (
                f"[PROACTIVE FOLLOW-UP — label={label}] {directive}"),
            "history": history,
            "session_id": payload.get("session_id"),
        }
        query = build_query(inner)
        rc, out, err, elapsed = run_hermes(query)
        if rc != 0:
            log(f"draft_followup hermes rc={rc} err={err[:200]!r}")
            send(200, {"ok": False, "degraded": True,
                             "error": f"hermes rc={rc}",
                             "draft_text": "", "label": label})
            return
        # extract_json returns (parsed_dict, raw_blob_str) — unpack both.
        parsed, _blob = extract_json(out)
        draft_text = ""
        notes_for_zayn = ""
        if isinstance(parsed, dict):
            notes_for_zayn = (parsed.get("notes_for_zayn") or "").strip()
        if isinstance(parsed, dict):
            # Hermes shape: {messages: ["hey Mark...", "..."]} —
            # strings, not dicts. Be tolerant of both.
            msgs = parsed.get("messages") or []
            if msgs and isinstance(msgs, list):
                parts = []
                for m in msgs:
                    if isinstance(m, str):
                        parts.append(m.strip())
                    elif isinstance(m, dict):
                        parts.append((m.get("text") or "").strip())
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
        if not draft_text:
            log(f"draft_followup empty draft — parsed_keys="
                f"{list(parsed.keys()) if isinstance(parsed, dict) else None}"
                f"  raw[:200]={(out or '')[:200]!r}")
        # Mark the nudge so the report damps + reengage_attempts increments.
        try:
            upsert_conversation_state(cid, "nudge_drafted")
        except Exception as _e:
            log("draft_followup nudge_drafted err:", repr(_e))
        log(f"draft-followup cid={cid!r} label={label} "
            f"waha_used={waha_used} waha_count={waha_count} "
            f"draft_len={len(draft_text)} elapsed={elapsed}s")
        send(200, {
            "ok": True, "customer_id": cid, "label": label,
            "draft_text": draft_text,
            "notes_for_zayn": notes_for_zayn,
            "customer_name": name,
            "approval_card_header": (
                f"🔔 PROACTIVE FOLLOW-UP — {label.lower()}"),
            "session_id": extract_session(out, err),
        })
    except Exception as e:
        log("draft_followup ERROR:", repr(e))
        send(200, {"ok": False, "degraded": True, "error": str(e),
                         "draft_text": "", "label": "WARM",
                         "notes_for_zayn": "", "customer_name": ""})

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
        _is_uae_working_hours,
        _name_fallback,
        get_current_label_row,
        get_customer_facts,
        hermes_analyze_lead,
        refresh_customer_facts_from_waha,
        waha_fetch_history,
    )
    force = bool(payload.get("force"))
    cap = int(payload.get("cap") or PIPELINE_ANALYZE_CAP)
    if not force and not _is_uae_working_hours():
        send(200, {
            "ok": True, "skipped": True,
            "skipped_reason": "outside_uae_working_hours",
            "telegram_text": ""})
        return
    try:
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
                waha_ = waha_fetch_history(cid, limit=30)
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
                _psql(
                    "UPDATE customer_facts SET "
                    f"importance_score = {score_}, "
                    f"importance_reasoning = {_lit(reasoning_)}, "
                    f"suggested_action = {_lit(suggested_)}, "
                    "importance_analyzed_at = now() "
                    f"WHERE customer_id = {_lit(cid)}")
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
            close_lines = [
                f"🛑 *Hermes flagged {len(closed)} lead(s) as "
                "not-convertible* — tap 🛑 Disregard on the matching "
                "/review card to close, or override with `/label "
                "<name> WARM`.",
                "",
            ]
            for cid, nm, r in closed[:5]:
                close_lines.append(f"  • *{nm}* — {r}")
            if len(closed) > 5:
                close_lines.append(
                    f"  _…and {len(closed) - 5} more — see /review_")
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
    try:
        row = get_current_label_row(cid) or {}
        cur_label = row.get("label") or "NEW"
        facts = get_customer_facts(cid) or {}
        nm = facts.get("name") or _name_fallback(cid)
        mc = int(row.get("message_count") or facts.get("message_count")
                 or 0)

        # ---- force-override path — skip Hermes, just close ----
        if force_close:
            prev_reasoning = facts.get("disregard_reasoning") or ""
            apply_label_transition(
                cid, cur_label, "DISREGARDED",
                signal="operator_override",
                evidence=(
                    "operator override — Hermes had said keep_open. "
                    f"Prior reasoning: {prev_reasoning[:180]}"),
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
            tx = (
                f"🛑 *DISREGARDED* — {nm_e}\n"
                f"_Operator override_ — closed despite Hermes saying "
                "'keep open'.\n\n"
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

        waha = waha_fetch_history(cid, limit=30)
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
    from server import evaluate_caps, get_mode, log_autosend
    cid = (payload.get("customer_id") or "").strip()
    if not cid:
        send(400, {"ok": False, "error": "customer_id is required"})
        return
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
    ok, reason = evaluate_caps(cid)
    if ok:
        log_autosend(cid, "auto")
    log(f"autosend-check COMMIT customer={cid} mode=autonomous "
        f"auto_send={ok} reason={reason!r}")
    send(200, {"ok": True, "mode": mode, "auto_send": ok,
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
    import time as _time
    t0 = _time.time()
    try:
        sql = (
            "SELECT cs.customer_id, "
            "COALESCE(cs.last_customer_message_at, 'epoch'::timestamptz), "
            "COALESCE(cs.last_operator_reply_at, 'epoch'::timestamptz), "
            "COALESCE(cs.last_analyzed_at, 'epoch'::timestamptz), "
            "EXTRACT(EPOCH FROM (now() - COALESCE(cs.last_customer_message_at, 'epoch'::timestamptz))) "
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
            if len(parts) < 5:
                continue
            cid = parts[0].strip()
            if not cid:
                continue
            try:
                silent_seconds = float(parts[4].strip())
            except ValueError:
                silent_seconds = 0.0
            rows.append((cid, silent_seconds))
        scanned = len(rows)
        transitions = 0
        for cid, silent_seconds in rows:
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
                # Cold decay — highest priority for the sweep.
                if (silent_seconds > 7 * 86400
                        and not (prev or "").startswith("PAUSED_")
                        and prev != "COLD"):
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
        rc, out, err, elapsed = run_hermes(query)
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
        rc, out, err, elapsed = run_hermes(query)
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
                if row is None:
                    row = get_current_label_row(customer_id) or {}
                customer_name = row.get("name") or ""
                prev_label = row.get("label") or "NEW"
                if prev_label != "CONFIRMED":
                    apply_label_transition(
                        customer_id, prev_label, "CONFIRMED",
                        f"nomod-webhook:payment_received:{matched_via}",
                        f"charge {charge_id[:8]} via webhook "
                        f"(matched_via={matched_via}"
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
