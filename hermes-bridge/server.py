#!/usr/bin/env python3
"""Hermes Bridge — thin HTTP wrapper around the Hermes Agent.

Endpoints (all POST except /health; POSTs require X-Bridge-Token):
  GET  /health      liveness probe
  POST /draft       customer context -> Hermes draft (JSON {messages, notes_for_zayn})
  POST /improve     review + improve an existing draft (FR-5 background pass)
  POST /learn       judge operator feedback -> capture a behavior_rule (inactive)
  POST /rules       list pending rules; activate or discard one
  POST /autosend-check  mode + caps decision for an autonomous draft (FR-4)
  POST /save-rule   persist a behavior_rule (INSERT into Postgres)

Drafting runs `hermes chat -q ... -Q` headless. On a refinement the prompt
also asks Hermes to suggest a durable behavior_rule when the operator's
instruction looks like a lasting preference (Step 5 / spec §4.6).

Postgres (behavior_rules) is reached via `docker exec` on the Postgres
container — psql connects over the local socket (trust auth), as the
hermes_rw role scoped to the 3 Hermes tables.

Stdlib only. Runs as the systemd user service hermes-bridge.service.
"""
import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Refactor week #3: foundational helpers moved out of server.py.
# These imports re-export the names at server module scope, so any
# `from server import _psql, _lit, _redis, log, _envflag, _md_escape`
# still resolves — no caller-side change required.
from util import log, _envflag, _md_escape  # noqa: F401
from db import (  # noqa: F401
    PG_CONTAINER, PG_USER, PG_DB, REDIS_CONTAINER,
    _psql, _redis, _lit,
)
from hermes_calls import (  # noqa: F401
    HERMES, HERMES_TIMEOUT, SESSION_RE, FENCE_RE,
    run_hermes, extract_json, extract_session,
)
from waha import (  # noqa: F401
    WAHA_API_KEY, WAHA_BASE,
    _waha_get, waha_lookup_push_name, waha_fetch_history,
)
from labels import (  # noqa: F401
    LABELS, _LABEL_RANK, _HARD_DEMOTE_SIGNALS, _TIER_BELOW,
    MONEY_RE, LETS_DO_IT_RE, PAST_DATE_MONTH_RE, PAYMENT_CONFIRMED_RE,
    SAME_DAY_RE, PRICING_INQUIRED_RE, YACHT_KEYWORD_RE,
    CORRECTION_WINDOW_DAYS, CORRECTION_DAMPENING_DIVISOR,
    CONFIDENCE_FLOOR, CONFIDENCE_DEMOTE_THRESHOLD,
    _MONTH_NUM, _parse_booking_date,
)
from payments import (  # noqa: F401
    NOMOD_API_KEY, NOMOD_API_BASE, NOMOD_WEBHOOK_SECRET, PAYMENTS_ENABLED,
    _normalize_phone_digits, _payer_mismatch,
    nomod_list_recent_charges, nomod_create_link,
)
from review import (  # noqa: F401
    YACHT_NAMES, _FACTS_DATE_RE, _FACTS_NAME_RE, _FACTS_BOOK_RE,
    REVIEW_CAP_HOT, REVIEW_CAP_NEEDS_ATTENTION,
    REVIEW_CAP_WARM, REVIEW_CAP_COLD, REVIEW_INLINE_REFRESH_CAP,
    UAE_WORK_HOURS_START, UAE_WORK_HOURS_END,
    _is_uae_working_hours,
    _facts_extract_gate, build_customer_header, _merge_facts,
    score_lead, _fmt_dur, render_review, _name_fallback, _why_line,
)
from routes import (  # noqa: F401
    handle_autonomous_log,
    handle_autosend_check,
    handle_autosend_state,
    handle_conversation_state,
    handle_customer_facts,
    handle_debounce,
    handle_draft,
    handle_draft_followup,
    handle_draft_freshness,
    handle_feedback,
    handle_followup_action,
    handle_hourly_sweep,
    handle_improve,
    handle_info,
    handle_label,
    handle_label_eval,
    handle_lead_analyze_disregard,
    handle_learn,
    handle_nomod_webhook,
    handle_payment_link,
    handle_pipeline_analyze,
    handle_poll_payments,
    handle_queue,
    handle_refresh_facts,
    handle_review,
    handle_rules,
    handle_save_rule,
    handle_set_mode,
    handle_snooze,
)

HOME = os.path.expanduser("~")
BRIDGE_DIR = os.path.join(HOME, "hermes-bridge")
SYSTEM_PROMPT_PATH = os.path.join(BRIDGE_DIR, "system-prompt.md")
# HERMES path + HERMES_TIMEOUT moved to hermes_calls.py
# (still importable from server for backward-compat — see top of file).

TOKEN = os.environ.get("BRIDGE_TOKEN", "")
PORT = int(os.environ.get("BRIDGE_PORT", "8788"))

# PG_CONTAINER / PG_USER / PG_DB / REDIS_CONTAINER moved to db.py
# (still importable from server for backward-compat — see top of file).
AUTOSEND_TTL = int(os.environ.get("BRIDGE_AUTOSEND_TTL", "3600"))
DEBOUNCE_TTL = int(os.environ.get("BRIDGE_DEBOUNCE_TTL", "120"))
QUEUE_TTL = int(os.environ.get("BRIDGE_QUEUE_TTL", "86400"))  # 24h — drafts auto-expire

# --- /feedback (operator behavioural-feedback command) ---------------------
FEEDBACK_TTL = int(os.environ.get("BRIDGE_FEEDBACK_TTL", "600"))
FEEDBACK_MAX_GLOBAL = int(os.environ.get("FEEDBACK_MAX_GLOBAL", "20"))
FEEDBACK_MAX_SCENARIO = int(os.environ.get("FEEDBACK_MAX_SCENARIO", "5"))
FEEDBACK_MAX_PER_CUSTOMER = int(os.environ.get("FEEDBACK_MAX_PER_CUSTOMER", "5"))


# _envflag moved to util.py (imported at top of file for backward compat).


# Autonomous-mode safety caps (spec §5.7) — all default ON.
CAP_DAILY_ACTIVE = _envflag("CAP_DAILY_ACTIVE", "true")
CAP_DAILY_LIMIT = int(os.environ.get("CAP_DAILY_LIMIT", "20"))
CAP_CONSEC_ACTIVE = _envflag("CAP_CONSECUTIVE_ACTIVE", "true")
CAP_CONSEC_LIMIT = int(os.environ.get("CAP_CONSECUTIVE_LIMIT", "5"))
CAP_SAMPLE_ACTIVE = _envflag("CAP_SAMPLE_ACTIVE", "true")
CAP_SAMPLE_PCT = float(os.environ.get("CAP_SAMPLE_PCT", "5"))
DUBAI_MIDNIGHT = ("date_trunc('day', now() AT TIME ZONE 'Asia/Dubai') "
                  "AT TIME ZONE 'Asia/Dubai'")

# --- Nomod payment links (minimal build) -----------------------------------
# WAHA_API_KEY / WAHA_BASE moved to waha.py.
# NOMOD_API_KEY / NOMOD_API_BASE / PAYMENTS_ENABLED moved to payments.py.
# Both still importable from server for backward compat (re-exported
# at top of file).

# SESSION_RE / FENCE_RE moved to hermes_calls.py (re-exported above).
VALID_SCOPES = ("global", "customer", "scenario", "tier")

# --- customer facts (feature-header) ---------------------------------------
FACTS_EXTRACT_TIMEOUT = int(os.environ.get("BRIDGE_FACTS_TIMEOUT", "15"))
# yacht keywords worth gating extraction on — curated from system-prompt.md §7
# (moved to review.py — re-exported at top of file)


# log() moved to util.py; _psql, _redis, _lit moved to db.py.
# All four are imported at the top of this file for backward-compat
# with `from server import log, _psql, _redis, _lit` callers (tests,
# scripts) — re-export only, no behavior change.


# ============================================================================
# Redis-backed pendingQueue — see docs/pendingqueue-redis-migration-plan.md
# ============================================================================

DRAFTS_ACTIVE = "drafts:active"

# Ghost-recovery — verified phrasings per silence window. See the
# ghost-recovery table in hermes-bridge/system-prompt.md (shipped in 6cc3940).
# Used by _draft_followup when called with a silence_window field by the
# proactive follow-up engine. Legacy callers (the [Draft nudge] button)
# don't pass silence_window and fall back to the label-aware directive.
GHOST_RECOVERY_PHRASES = {
    "hot_30m_2h":    "Have you given up on booking a private yacht?",
    "hot_2h_24h":    "[Name], are you still there? "
                     "OR: Hi! May I know the hourly rate you are considering?",
    "warm_24h_72h":  ("Just checking in if you have any update for us, "
                     "are you still considering to book a yacht or has "
                     "there been any change in the plan perhaps?"),
    "cold_lastshot": "Have you given up on booking a private yacht?",
}
GHOST_RECOVERY_WINDOWS = frozenset(GHOST_RECOVERY_PHRASES.keys())


def _draft_key(did):
    return "draft:" + str(did)


def _byc_key(cid):
    return "drafts:bycustomer:" + str(cid)


def _draft_save(draft):
    """Write draft JSON, set TTL, update active set + per-customer ZSET.
    Returns (ok, err).

    Auto-supersede: when saving a NEW pending draft for a customer who
    already has prior pending drafts in Redis, flip those priors to
    'superseded' so operator's Telegram queue never accumulates stale
    cards for the same customer. The workflow's Queue & Format JS does
    the same on staticData; this keeps Redis (the persistent source of
    truth) aligned.

    Canonical-cid resolution: the customer_phone in the draft may come
    in under either @lid or @c.us — we canonicalize via merged_into so
    the per-customer ZSET and supersede sweep operate on ONE identity.
    Production bug 2026-05-26: Émilie had duplicate customer_facts rows
    (137813169274972@lid + 224167731408910@lid); drafts queued under
    cid A while Send fetched from cid B → wrong-version + double-send.
    Without this canonicalize, the fix would not survive a single
    multi-cid customer.

    Instrumentation: log every save with id+cid+status+msg_count so
    journalctl alone is sufficient to diagnose race conditions
    (double-send, edit-not-applied, draft-expired) post-mortem
    without needing n8n execution_data retention."""
    if not isinstance(draft, dict):
        return False, "draft must be an object"
    did = (draft.get("id") or "").strip()
    cid_raw = (draft.get("customer_phone") or "").strip()
    cid = canonicalize_cid(cid_raw)
    if cid != cid_raw:
        # Mutate the draft so downstream consumers (Send, Edit, etc.)
        # also see the canonical cid. Otherwise WAHA-send addresses
        # the non-canonical phone format.
        draft["customer_phone"] = cid
        log(f"_draft_save canonicalized cid {cid_raw!r} -> {cid!r} "
            f"for draft {did}")
    msgs = draft.get("messages") or []
    msg_count = len(msgs) if isinstance(msgs, list) else 0
    preview = ""
    if msgs and isinstance(msgs, list) and msgs[0]:
        preview = str(msgs[0])[:60].replace("\n", " / ")
    log(f"DRAFT_SAVE id={did} cid={cid} status={draft.get('status','?')} "
        f"msgs={msg_count} preview={preview!r}")
    if not did or not cid:
        return False, "id + customer_phone required"
    ts = int(time.time() * 1000)
    _, err = _redis(["SET", _draft_key(did), json.dumps(draft),
                     "EX", str(QUEUE_TTL)])
    if err:
        return False, err
    status = (draft.get("status") or "pending")
    if status == "pending":
        # Auto-supersede prior pendings for the same customer.
        try:
            existing_out, _xe = _redis(
                ["ZRANGE", _byc_key(cid), "0", "-1"])
            for other_id in (existing_out or "").splitlines():
                other_id = other_id.strip()
                if not other_id or other_id == did:
                    continue
                other, _ge = _draft_get(other_id)
                if other and other.get("status") == "pending":
                    other["status"] = "superseded"
                    _redis(["SET", _draft_key(other_id),
                            json.dumps(other),
                            "EX", str(QUEUE_TTL)])
                    _redis(["SREM", DRAFTS_ACTIVE, other_id])
                    log(f"_draft_save auto-superseded prior pending "
                        f"{other_id} for cid={cid}")
        except Exception as e:
            # Defensive: never block the new save on a cleanup failure.
            log(f"_draft_save supersede sweep err: {e!r}")
        _redis(["SADD", DRAFTS_ACTIVE, did])
    else:
        _redis(["SREM", DRAFTS_ACTIVE, did])
    _redis(["ZADD", _byc_key(cid), str(ts), did])
    _redis(["EXPIRE", _byc_key(cid), str(QUEUE_TTL)])
    return True, None


def _draft_get(did):
    """Return (draft|None, err|None)."""
    if not did:
        return None, None
    out, err = _redis(["GET", _draft_key(did)])
    if err:
        return None, err
    raw = (out or "").strip()
    if not raw:
        return None, None
    try:
        return json.loads(raw), None
    except Exception as e:
        return None, repr(e)


def _draft_update(did, fields):
    """Read-modify-write. Returns (draft|None, err|None).
    Single-threaded Redis makes this atomic-enough for our load.

    Instrumentation: every status transition is logged with prior +
    new status. status→sent is the critical one — log line gives
    operator + post-mortem a deterministic anchor to correlate with
    WAHA send events when diagnosing double-sends or edit-races."""
    if not did:
        return None, "draft_id required"
    d, err = _draft_get(did)
    if err:
        return None, err
    if not d:
        return None, "draft not found"
    prior_status = d.get("status", "?")
    cid = d.get("customer_phone", "?")
    d.update(fields or {})
    # status side-effect on the active set
    if "status" in (fields or {}):
        if (fields["status"] or "") == "pending":
            _redis(["SADD", DRAFTS_ACTIVE, did])
        else:
            _redis(["SREM", DRAFTS_ACTIVE, did])
    _, err = _redis(["SET", _draft_key(did), json.dumps(d),
                     "EX", str(QUEUE_TTL)])
    if err:
        return None, err
    # Log transitions — focus on status changes (signal-rich) and
    # mark messages/text edits (the refine path overwrites these
    # and races have caused production bugs).
    field_keys = list((fields or {}).keys())
    new_status = d.get("status", "?")
    if "status" in (fields or {}) and new_status != prior_status:
        log(f"DRAFT_UPDATE id={did} cid={cid} status={prior_status}"
            f"->{new_status} fields={field_keys}")
    elif "messages" in (fields or {}) or "draft_text" in (fields or {}):
        msgs = d.get("messages") or []
        msg_count = len(msgs) if isinstance(msgs, list) else 0
        log(f"DRAFT_UPDATE id={did} cid={cid} CONTENT_CHANGED "
            f"msgs={msg_count} fields={field_keys}")
    return d, None


def _draft_drop(did):
    """Cleanup. Returns (ok, err)."""
    if not did:
        return False, "draft_id required"
    _redis(["DEL", _draft_key(did)])
    _redis(["SREM", DRAFTS_ACTIVE, did])
    return True, None


def _draft_latest_for_customer(customer_id, want_status=None):
    """Return the newest draft for a customer, optionally filtered by status.
    Returns (draft|None, err|None)."""
    if not customer_id:
        return None, "customer_id required"
    out, err = _redis(["ZREVRANGE", _byc_key(customer_id), "0", "20"])
    if err:
        return None, err
    for line in (out or "").splitlines():
        did = line.strip()
        if not did:
            continue
        d, _ = _draft_get(did)
        if not d:
            continue
        if want_status and (d.get("status") or "") != want_status:
            continue
        return d, None
    return None, None


# nomod_list_recent_charges / _normalize_phone_digits / _payer_mismatch /
# nomod_create_link moved to payments.py (re-exported at top of server
# for backward compat).


# --- /feedback helpers -----------------------------------------------------

FEEDBACK_CLASSIFIER_PROMPT = (
    "Classify this operator feedback into ONE of:\n"
    "  CUSTOMER_NOTE — a fact about a specific named customer to remember.\n"
    "  GLOBAL_RULE — applies to ALL future drafts (no specific customer/scenario).\n"
    "  SCENARIO_RULE — applies in a specific scenario only "
    "(proposal, birthday, family group, corporate, B2B, etc.).\n"
    "Return ONLY valid JSON (no prose, no markdown fences):\n"
    "  {\"classification\":\"CUSTOMER_NOTE|GLOBAL_RULE|SCENARIO_RULE\","
    "\"customer_name\":\"<name or empty>\","
    "\"scenario\":\"<scenario name or empty>\","
    "\"text\":\"<the rule/note text, cleaned, imperative voice>\","
    "\"summary\":\"<one short sentence for operator confirmation>\"}\n"
    "Operator feedback: "
)


def _classify_feedback_fallback(text):
    """Deterministic last-resort classifier when Hermes is slow/down.
    Pattern-matches the operator's feedback to choose a class without LLM.
    Bias: prefer SCENARIO_RULE or CUSTOMER_NOTE only when the cue is strong;
    everything else lands in GLOBAL_RULE so the operator can still tweak it."""
    t = (text or "").strip()
    low = t.lower()
    # CUSTOMER_NOTE: contains "customer <Name>" or a quoted/capitalized
    # personal name marker. Pull the first capitalized token after "customer".
    name = ""
    m = re.search(r"\bcustomer\s+([A-Z][\w'-]{1,30})", t)
    if m:
        name = m.group(1).strip()
    elif re.search(r"\b(mr|mrs|ms|sir|madam)\s+[a-z]", low):
        pass  # honorific without name — skip
    # SCENARIO cues
    scenario = ""
    for cue, name_ in (
        ("proposal", "proposal"), ("birthday", "birthday"),
        ("anniversary", "anniversary"), ("family group", "family group"),
        ("corporate", "corporate"), ("b2b", "b2b"),
        ("vip", "vip"), ("repeat", "repeat customer"),
    ):
        if cue in low:
            scenario = name_
            break
    if name:
        cls = "CUSTOMER_NOTE"
    elif scenario:
        cls = "SCENARIO_RULE"
    else:
        cls = "GLOBAL_RULE"
    # Clean text: trim trailing punctuation, ensure imperative-ish.
    clean = re.sub(r"\s+", " ", t).strip().rstrip(".,;:")
    summary = clean if len(clean) <= 120 else (clean[:117] + "...")
    return {
        "classification": cls,
        "customer_name": name,
        "scenario": scenario,
        "text": clean,
        "summary": summary,
        "_fallback": True,
    }


def classify_feedback(text):
    """Hermes classifier call. Returns the parsed dict or a deterministic
    fallback on failure — never None when text is non-empty, so /feedback
    never silently drops."""
    if not text or not text.strip():
        return None
    try:
        rc, out, _, _ = run_hermes(FEEDBACK_CLASSIFIER_PROMPT + text.strip(),
                                   timeout=60)
        if rc == 0:
            parsed, _ = extract_json(out)
            if isinstance(parsed, dict):
                cls = (parsed.get("classification") or "").upper().strip()
                if cls in ("CUSTOMER_NOTE", "GLOBAL_RULE", "SCENARIO_RULE"):
                    return {
                        "classification": cls,
                        "customer_name": (parsed.get("customer_name") or "").strip(),
                        "scenario": (parsed.get("scenario") or "").strip(),
                        "text": (parsed.get("text") or "").strip(),
                        "summary": (parsed.get("summary") or "").strip(),
                    }
        log("feedback classify rc=", rc, "— falling back to deterministic")
    except Exception as e:
        log("feedback classify hermes error:", repr(e),
            "— falling back to deterministic")
    # Hermes failed/timed out — never drop operator feedback silently.
    fb = _classify_feedback_fallback(text)
    log(f"feedback classify FALLBACK -> {fb['classification']} "
        f"name={fb['customer_name']!r} scenario={fb['scenario']!r}")
    return fb


# _waha_get / waha_lookup_push_name / waha_fetch_history moved to
# waha.py (re-exported at top of server.py for backward compat).


def resolve_customer_by_name(name):
    """Find customer_id in customer_facts by case-insensitive name LIKE.
    Returns (customer_id_or_None, top_3_matches[])."""
    if not name or not name.strip():
        return None, []
    n = name.strip().replace("'", "''").lower()
    sql = ("SELECT customer_id || E'\\t' || name FROM customer_facts "
           f"WHERE lower(name) LIKE '%{n}%' "
           "ORDER BY updated_at DESC LIMIT 3")
    out, err = _psql(sql)
    if err:
        return None, []
    matches = []
    for ln in (out or "").splitlines():
        parts = ln.split("\t")
        if len(parts) >= 2:
            matches.append({"customer_id": parts[0].strip(),
                            "name": parts[1].strip()})
    if len(matches) == 1:
        return matches[0]["customer_id"], matches
    return None, matches


def _normalize_phone(s):
    """Strip everything except digits. Drop leading 00. UAE local
    formats (0xxxxxxxxx, xxxxxxxxx starting with 5) get normalized to
    971-prefix. Returns digits-only string or '' if no digits."""
    digits = "".join(ch for ch in (s or "") if ch.isdigit())
    if not digits:
        return ""
    if digits.startswith("00"):
        digits = digits[2:]
    if len(digits) == 10 and digits.startswith("0"):
        digits = "971" + digits[1:]
    elif len(digits) == 9 and digits.startswith("5"):
        digits = "971" + digits
    return digits


def resolve_customer_by_phone(phone):
    """Find a customer_id by phone number. WhatsApp customer_ids come in
    two shapes — `<digits>@c.us` (normal) and `<digits>@lid` (privacy-
    rotated id). The @lid id is a hash, NOT the phone number, so a phone
    lookup needs to go via WAHA's chat list, where pushName / id mapping
    is exposed. Strategy:
      1. Normalize phone to digits-only.
      2. Try DB: SELECT customer_id FROM customer_facts WHERE
         customer_id LIKE '<digits>@%' (catches the @c.us shape).
      3. If no DB hit, ask WAHA for the chat list and find an entry whose
         numeric pushName / id contains the digits.
      4. Return (customer_id_or_None, [matches]).
    """
    digits = _normalize_phone(phone)
    if not digits or len(digits) < 7:
        return None, []
    # DB path — covers @c.us shape (the customer_id literally contains
    # the phone digits before the @).
    safe = digits.replace("'", "''")
    sql = ("SELECT customer_id || E'\\t' || COALESCE(name,'') "
           "FROM customer_facts "
           f"WHERE customer_id LIKE '{safe}@%' "
           "ORDER BY updated_at DESC LIMIT 3")
    out, err = _psql(sql)
    matches = []
    if not err:
        for ln in (out or "").splitlines():
            parts = ln.split("\t")
            if len(parts) >= 2:
                matches.append({"customer_id": parts[0].strip(),
                                "name": parts[1].strip()})
    if len(matches) == 1:
        return matches[0]["customer_id"], matches
    if matches:
        return None, matches
    # WAHA fallback — @lid customer_ids are hashes; the only way to map
    # phone -> @lid is via the chat list, where WAHA exposes either the
    # phone-formatted pushName or an internal id object with `user`.
    chats, _err = _waha_get("/api/default/chats?limit=200")
    if isinstance(chats, list):
        for c in chats:
            cid = c.get("_serialized") or (c.get("id") or {}).get(
                "_serialized") or ""
            pn = (c.get("name") or "").strip()
            pn_digits = _normalize_phone(pn)
            # Match the phone digits against the chat's pushName-derived
            # digits (which is the form `+971 50 976 7187` for unsaved
            # contacts) or the @c.us-shaped customer_id.
            if (pn_digits and pn_digits.endswith(digits)) or \
               cid.startswith(digits + "@"):
                matches.append({"customer_id": cid, "name": pn})
    if len(matches) == 1:
        return matches[0]["customer_id"], matches
    return None, matches


def behavioral_context(customer_id):
    """Active behavioural rules + notes for a customer. Used by drafts.
    Returns {global:[...], scenario:[{scenario, rule}], customer_notes:[...]}."""
    cid = (customer_id or "").replace("'", "''")
    out, _ = _psql("SELECT rule_text FROM behavior_rules "
                   "WHERE scope='global' AND active=true "
                   f"ORDER BY id DESC LIMIT {FEEDBACK_MAX_GLOBAL}")
    glb = [ln.strip() for ln in (out or "").splitlines() if ln.strip()]
    out, _ = _psql("SELECT scope_value || E'\\t' || rule_text "
                   "FROM behavior_rules WHERE scope='scenario' AND active=true "
                   "ORDER BY id DESC LIMIT 50")
    sc = []
    for ln in (out or "").splitlines():
        parts = ln.split("\t")
        if len(parts) >= 2:
            sc.append({"scenario": parts[0].strip(),
                       "rule": parts[1].strip()})
    notes = []
    if cid:
        out, _ = _psql(f"SELECT note_text FROM customer_notes "
                       f"WHERE customer_id='{cid}' AND active=true "
                       f"ORDER BY id DESC LIMIT {FEEDBACK_MAX_PER_CUSTOMER}")
        notes = [ln.strip() for ln in (out or "").splitlines() if ln.strip()]
    # Pre-formatted block for drop-in at the end of Build Prompt's system
    # prompt. Empty string when no context exists — safe to concatenate.
    blocks = []
    if glb:
        blocks.append("### Global rules (always apply)")
        blocks.extend(["- " + r for r in glb])
    if sc:
        if blocks:
            blocks.append("")
        blocks.append("### Scenario rules")
        blocks.extend(["- [%s] %s" % (s["scenario"], s["rule"]) for s in sc])
    if notes:
        if blocks:
            blocks.append("")
        blocks.append("### Notes for THIS customer")
        blocks.extend(["- " + n for n in notes])
    formatted = ("## Behavioral context (live — operator feedback)\n"
                 + "\n".join(blocks)) if blocks else ""
    return {"global": glb, "scenario": sc, "customer_notes": notes,
            "formatted": formatted}


def feedback_apply_cap(table, cap, scope=None, scope_value=None, customer_id=None):
    """Keep at most `cap` rows active in a scope/scenario/customer slot;
    deactivate the oldest (lowest id) beyond the cap."""
    if cap is None or cap <= 0:
        return
    if table == "behavior_rules" and scope == "global":
        sql = ("UPDATE behavior_rules SET active=false WHERE id IN ("
               "SELECT id FROM behavior_rules WHERE scope='global' "
               "AND active=true ORDER BY id DESC OFFSET " + str(cap) + ")")
    elif table == "behavior_rules" and scope == "scenario" and scope_value:
        sv = scope_value.replace("'", "''")
        sql = ("UPDATE behavior_rules SET active=false WHERE id IN ("
               "SELECT id FROM behavior_rules WHERE scope='scenario' "
               f"AND scope_value='{sv}' AND active=true "
               "ORDER BY id DESC OFFSET " + str(cap) + ")")
    elif table == "customer_notes" and customer_id:
        cid = customer_id.replace("'", "''")
        sql = ("UPDATE customer_notes SET active=false WHERE id IN ("
               "SELECT id FROM customer_notes "
               f"WHERE customer_id='{cid}' AND active=true "
               "ORDER BY id DESC OFFSET " + str(cap) + ")")
    else:
        return
    _psql(sql)


def fetch_behavior_rules(customer_id):
    """Active global + customer-scoped behavior rules. Degrades to [] on any
    error so drafting never breaks over a rules lookup."""
    cid = (customer_id or "").replace("'", "''")
    sql = (
        "SELECT rule_text FROM behavior_rules "
        "WHERE active = true AND (scope = 'global' "
        f"OR (scope = 'customer' AND scope_value = '{cid}')) "
        "ORDER BY scope DESC, created_at"
    )
    try:
        out, err = _psql(sql)
        if err:
            log("behavior_rules fetch failed:", err)
            return []
        return [ln.strip() for ln in out.splitlines() if ln.strip()]
    except Exception as e:
        log("behavior_rules fetch error:", repr(e))
        return []


def save_behavior_rule(rule_text, scope, scope_value, created_via, reasoning):
    """INSERT a behavior_rule, INACTIVE by default — captured rules require
    operator approval (active=true) before they shape drafts. Returns
    (rule_id, None) or (None, error)."""
    sql = (
        "INSERT INTO behavior_rules "
        "(rule_text, scope, scope_value, created_via, reasoning, active) VALUES ("
        + ", ".join([_lit(rule_text), _lit(scope), _lit(scope_value),
                     _lit(created_via), _lit(reasoning)])
        + ", false) RETURNING id"
    )
    try:
        out, err = _psql(sql)
        if err:
            return None, err
        # psql -tA emits the RETURNING value then the command tag — take line 1.
        rid = (out.strip().splitlines() or [""])[0].strip()
        return rid, None
    except Exception as e:
        return None, repr(e)


def save_trigger(customer_id, customer_name, trigger_type, reminder_hours,
                 context, source_message, confidence):
    """INSERT a customer_triggers row (status defaults to 'pending'). Returns
    (trigger_id, None) or (None, error)."""
    try:
        hrs = max(1, min(720, int(reminder_hours)))
    except (TypeError, ValueError):
        hrs = 24
    try:
        conf = max(0.0, min(1.0, float(confidence)))
    except (TypeError, ValueError):
        conf = 0.5
    sql = (
        "INSERT INTO customer_triggers (customer_id, customer_name, "
        "trigger_type, reminder_date, trigger_context, source_message, "
        "confidence) VALUES ("
        + ", ".join([_lit(customer_id), _lit(customer_name), _lit(trigger_type)])
        + f", now() + interval '{hrs} hours', "
        + ", ".join([_lit(context), _lit(source_message)])
        + f", {conf}) RETURNING id"
    )
    try:
        out, err = _psql(sql)
        if err:
            return None, err
        return (out.strip().splitlines() or [""])[0].strip(), None
    except Exception as e:
        return None, repr(e)


def save_health(customer_id, customer_name, score, reason):
    """UPSERT the latest conversation-health snapshot for a customer (Step 7)."""
    sql = (
        "INSERT INTO conversation_health "
        "(customer_id, customer_name, score, reason, updated_at) VALUES ("
        + ", ".join([_lit(customer_id), _lit(customer_name), _lit(score),
                     _lit(reason)])
        + ", now()) ON CONFLICT (customer_id) DO UPDATE SET "
        "customer_name = EXCLUDED.customer_name, score = EXCLUDED.score, "
        "reason = EXCLUDED.reason, updated_at = now()"
    )
    try:
        _, err = _psql(sql)
        return (err is None), err
    except Exception as e:
        return False, repr(e)


def get_mode(customer_id):
    """Current conversation mode for a customer (Step 8). Fail-closed: any
    error, missing row, or unrecognised value -> 'approval'."""
    cid = (customer_id or "").replace("'", "''")
    sql = (f"SELECT mode FROM conversation_modes WHERE customer_id = '{cid}' "
           "ORDER BY id DESC LIMIT 1")
    try:
        out, err = _psql(sql)
        if err:
            return "approval"
        lines = [x.strip() for x in (out or "").splitlines() if x.strip()]
        m = lines[0] if lines else ""
        return m if m in ("approval", "autonomous", "paused") else "approval"
    except Exception:
        return "approval"


def set_mode(customer_id, mode, activated_by, break_reason=None):
    """Record a conversation-mode change. Returns (mode, None) or (None, err).
    break_reason is recorded when an automatic break-condition triggered the
    change; it is NULL for a normal operator-driven mode change."""
    if mode not in ("approval", "autonomous", "paused"):
        return None, "invalid mode (use approval|autonomous|paused)"
    sql = (
        "INSERT INTO conversation_modes "
        "(customer_id, mode, activated_at, activated_by, break_reason) VALUES ("
        + ", ".join([_lit(customer_id), _lit(mode)])
        + ", now(), " + _lit(activated_by) + ", " + _lit(break_reason) + ")"
    )
    try:
        _, err = _psql(sql)
        if err:
            return None, err
        log_autosend(customer_id, "intervention")  # resets the consecutive streak
        return mode, None
    except Exception as e:
        return None, repr(e)


def manual_killswitch():
    """Kill switch — force every known conversation back to 'approval'."""
    sql = (
        "INSERT INTO conversation_modes "
        "(customer_id, mode, activated_at, activated_by, break_reason) "
        "SELECT DISTINCT customer_id, 'approval', now(), 'manual_killswitch', "
        "'/manual kill switch' FROM conversation_modes "
        "WHERE customer_id IS NOT NULL"
    )
    try:
        _, err = _psql(sql)
        return (err is None), err
    except Exception as e:
        return False, repr(e)


# --- behavior-rule review (FR-5 learning loop) -----------------------------

def list_pending_rules(limit=25):
    """Inactive (awaiting-approval) behavior rules, newest first."""
    sql = ("SELECT id || E'\\t' || scope || E'\\t' || rule_text "
           "FROM behavior_rules WHERE active = false "
           "AND scope IN ('global', 'customer', 'scenario', 'tier') "
           f"ORDER BY created_at DESC LIMIT {int(limit)}")
    try:
        out, err = _psql(sql)
        if err:
            return None, err
        rows = []
        for ln in (out or "").splitlines():
            p = ln.split("\t")
            if len(p) >= 3 and p[0].strip().isdigit():
                rows.append({"id": p[0].strip(), "scope": p[1].strip(),
                             "rule_text": "\t".join(p[2:]).strip()})
        return rows, None
    except Exception as e:
        return None, repr(e)


def activate_rule(rule_id):
    """Set a behavior_rule active=true. Returns (ok, err)."""
    try:
        rid = int(rule_id)
    except (TypeError, ValueError):
        return False, "rule_id must be an integer"
    out, err = _psql(f"UPDATE behavior_rules SET active = true "
                     f"WHERE id = {rid} RETURNING id")
    if err:
        return False, err
    lines = [x for x in (out or "").splitlines() if x.strip()]
    ok = bool(lines) and lines[0].strip().isdigit()
    return ok, (None if ok else "no rule with that id")


def discard_rule(rule_id):
    """Discard a pending behavior_rule. The hermes_rw role has no DELETE grant,
    so this UPDATEs the scope to a sentinel ('_discarded') — the rule then
    never matches fetch_behavior_rules or list_pending_rules. Returns (ok, err)."""
    try:
        rid = int(rule_id)
    except (TypeError, ValueError):
        return False, "rule_id must be an integer"
    out, err = _psql(f"UPDATE behavior_rules SET scope = '_discarded' "
                     f"WHERE id = {rid} AND active = false RETURNING id")
    if err:
        return False, err
    lines = [x for x in (out or "").splitlines() if x.strip()]
    ok = bool(lines) and lines[0].strip().isdigit()
    return ok, (None if ok else "no pending rule with that id")


# --- autonomous-mode safety caps (§5.7) ------------------------------------

def _count(sql):
    out, err = _psql(sql)
    if err:
        return None
    try:
        return int((out or "0").strip().splitlines()[0])
    except (ValueError, IndexError):
        return 0


def cap_daily_count():
    """Auto-sends already made today (00:00 Asia/Dubai)."""
    n = _count("SELECT count(*) FROM autonomous_sends WHERE kind='auto' "
               f"AND sent_at >= {DUBAI_MIDNIGHT}")
    return n if n is not None else CAP_DAILY_LIMIT  # fail-closed: assume full


def cap_consecutive(customer_id):
    """Consecutive auto-sends for a customer since the last checkpoint or
    operator intervention."""
    cid = (customer_id or "").replace("'", "''")
    n = _count(
        f"SELECT count(*) FROM autonomous_sends WHERE customer_id='{cid}' "
        "AND kind='auto' AND id > COALESCE((SELECT max(id) FROM autonomous_sends "
        f"WHERE customer_id='{cid}' AND kind IN ('checkpoint','intervention')), 0)")
    return n if n is not None else CAP_CONSEC_LIMIT  # fail-closed


def log_autosend(customer_id, kind):
    """Record an autonomous-mode event: kind = auto | checkpoint | intervention."""
    cid = (customer_id or "").replace("'", "''")
    k = kind if kind in ("auto", "checkpoint", "intervention") else "auto"
    _psql(f"INSERT INTO autonomous_sends (customer_id, kind) VALUES ('{cid}', '{k}')")


def evaluate_caps(customer_id):
    """Decide whether an autonomous draft may auto-send. Returns (ok, reason).
    Fail-closed — any uncertainty routes the draft to approval."""
    try:
        if CAP_DAILY_ACTIVE:
            used = cap_daily_count()
            if used >= CAP_DAILY_LIMIT:
                return False, f"daily cap reached ({used}/{CAP_DAILY_LIMIT})"
        if CAP_CONSEC_ACTIVE:
            consec = cap_consecutive(customer_id)
            if consec >= CAP_CONSEC_LIMIT:
                log_autosend(customer_id, "checkpoint")
                return False, (f"checkpoint after {consec} consecutive "
                               "auto-sends — this one needs your approval")
        if CAP_SAMPLE_ACTIVE and random.random() < (CAP_SAMPLE_PCT / 100.0):
            return False, f"QC sample ({CAP_SAMPLE_PCT:g}%) — routed for your review"
        return True, "ok"
    except Exception as e:
        log("evaluate_caps error:", repr(e))
        return False, "cap-check error — routed to approval (fail-closed)"


def caps_status_text():
    """Human-readable cap status for the /caps command + daily digest."""
    used = cap_daily_count()
    auton = _count("SELECT count(*) FROM (SELECT DISTINCT ON (customer_id) mode "
                   "FROM conversation_modes ORDER BY customer_id, id DESC) s "
                   "WHERE mode='autonomous'")
    today = _count("SELECT count(*) FROM autonomous_sends WHERE kind='auto' "
                   f"AND sent_at >= {DUBAI_MIDNIGHT}") or 0
    chk = _count("SELECT count(*) FROM autonomous_sends WHERE kind='checkpoint' "
                 f"AND sent_at >= {DUBAI_MIDNIGHT}") or 0
    return "\n".join([
        "🧮 Autonomous-mode safety caps",
        "",
        f"1. Daily cap: {'ON' if CAP_DAILY_ACTIVE else 'OFF'} — "
        f"{used}/{CAP_DAILY_LIMIT} auto-sends used today (Dubai)",
        f"2. Per-conversation: {'ON' if CAP_CONSEC_ACTIVE else 'OFF'} — "
        f"checkpoint every {CAP_CONSEC_LIMIT} consecutive",
        f"3. QC sampling: {'ON' if CAP_SAMPLE_ACTIVE else 'OFF'} — "
        f"{CAP_SAMPLE_PCT:g}% of auto-sends routed to approval",
        "",
        f"Conversations in autonomous mode: {auton if auton is not None else '?'}",
        f"Today: {today} auto-sent, {chk} checkpoints",
    ])


# --- customer facts (feature-header) ---------------------------------------

# (moved to review.py — re-exported at top of file)


def canonicalize_cid(cid):
    """Resolve a customer_id to its canonical form via the
    customer_facts.merged_into pointer (migration 006).

    Returns the canonical cid the system should read/write under. If
    the row has merged_into NULL, the cid IS canonical and is returned
    unchanged. If the row doesn't exist (new customer), returns the
    input unchanged. Follows at most 3 hops defensively — production
    should only ever have 1-hop chains.

    Fail-open: any DB hiccup returns the input cid so a transient
    error doesn't reroute writes to '' or stale targets."""
    if not cid:
        return cid
    current = cid
    for _ in range(3):
        cid_e = current.replace("'", "''")
        try:
            out, err = _psql(
                "SELECT COALESCE(merged_into,'') FROM customer_facts "
                f"WHERE customer_id = '{cid_e}'", timeout=5)
        except Exception as e:
            log(f"canonicalize_cid err for {cid!r}: {e!r}")
            return current
        if err:
            return current
        line = (out or "").strip()
        if not line:
            return current  # no row → input cid is its own canonical
        target = (line.splitlines()[0] or "").strip()
        if not target:
            return current  # merged_into NULL → already canonical
        current = target
    return current


def get_customer_facts(customer_id):
    """The customer_facts row as a dict, or None if absent / on error.
    Degrades to None so the header logic never breaks over a DB read.

    Resolves through merged_into pointer so a read for a non-canonical
    cid returns the canonical row's facts."""
    customer_id = canonicalize_cid(customer_id)
    cid = (customer_id or "").replace("'", "''")
    sql = ("SELECT name, dates, yachts, party_size, message_count "
           f"FROM customer_facts WHERE customer_id = '{cid}'")
    try:
        out, err = _psql(sql)
        if err:
            log("customer_facts fetch failed:", err)
            return None
        line = (out or "").strip()
        if not line:
            return None
        p = line.split("|")
        if len(p) < 5:
            return None
        return {"name": p[0], "dates": p[1], "yachts": p[2],
                "party_size": p[3],
                "message_count": int(p[4]) if p[4].strip().isdigit() else 0}
    except Exception as e:
        log("customer_facts fetch error:", repr(e))
        return None


def upsert_customer_facts(customer_id, name, facts):
    """UPSERT a customer_facts row. INSERT -> message_count 1; ON CONFLICT ->
    message_count = existing + 1 (atomic in SQL — no read-modify-write race).
    Returns (new_message_count, None) or (None, error).

    Resolves through merged_into so writes always land on the canonical
    row — the duplicate's data path automatically heals when the next
    message arrives under its old cid."""
    customer_id = canonicalize_cid(customer_id)
    sql = (
        "INSERT INTO customer_facts (customer_id, name, dates, yachts, "
        "party_size, message_count, updated_at) VALUES ("
        + ", ".join([_lit(customer_id), _lit(name), _lit(facts.get("dates")),
                     _lit(facts.get("yachts")), _lit(facts.get("party_size"))])
        + ", 1, now()) ON CONFLICT (customer_id) DO UPDATE SET "
        "name = EXCLUDED.name, dates = EXCLUDED.dates, "
        "yachts = EXCLUDED.yachts, party_size = EXCLUDED.party_size, "
        "message_count = customer_facts.message_count + 1, updated_at = now() "
        "RETURNING message_count"
    )
    try:
        out, err = _psql(sql)
        if err:
            return None, err
        v = (out or "").strip().splitlines()
        return (int(v[0]) if v and v[0].strip().isdigit() else None), None
    except Exception as e:
        return None, repr(e)


def extract_customer_facts(incoming_message, history):
    """Hermes call: extract {name,dates,yachts,party_size} from the message +
    history. Returns a dict, or None on timeout/error/bad output — the caller
    then falls back to cached facts."""
    q = (
        "TASK: From the WhatsApp conversation below, extract the customer's "
        "current yacht-charter booking facts for an internal CRM header. "
        "Return ONLY a JSON object with exactly these keys, using an empty "
        'string "" for anything not yet known (never guess):\n'
        '{"name":"","dates":"","yachts":"","party_size":""}\n'
        "- name: the customer's first/full name if they have given it\n"
        '- dates: charter date(s) of interest, short (e.g. "Sat Dec 14")\n'
        "- yachts: every yacht name discussed, comma-separated\n"
        '- party_size: group size (e.g. "6-8 guests")\n'
        "Consider the WHOLE conversation, not just the latest line. Output "
        "only the JSON object — no markdown fences, no commentary.\n\n"
        "--- CONVERSATION ---\n" + (history or "(no prior history)") +
        "\n\n--- LATEST MESSAGE ---\n" + (incoming_message or ""))
    try:
        rc, out, err, elapsed = run_hermes(q, timeout=FACTS_EXTRACT_TIMEOUT)
    except subprocess.TimeoutExpired:
        log(f"extract_customer_facts: hermes timeout {FACTS_EXTRACT_TIMEOUT}s")
        return None
    except Exception as e:
        log("extract_customer_facts: hermes error", repr(e))
        return None
    if rc != 0:
        log(f"extract_customer_facts: hermes rc={rc} err={(err or '')[:200]!r}")
        return None
    parsed, _ = extract_json(out)
    if not isinstance(parsed, dict):
        log("extract_customer_facts: no JSON in hermes output")
        return None
    return {k: str(parsed.get(k) or "").strip()
            for k in ("name", "dates", "yachts", "party_size")}


# --- pattern-recognition analyzer (used by /lead-analyze-disregard +
#     /pipeline-analyze hourly importance ranker) ----------------------------
ANALYZE_LEAD_TIMEOUT = int(os.environ.get("BRIDGE_ANALYZE_TIMEOUT", "45"))

# UAE working hours (Asia/Dubai = UTC+4, no DST). Used by /pipeline-analyze
# cron to skip overnight runs — keeps the Hermes spend in business hours.
# (moved to review.py — re-exported at top of file)


def hermes_analyze_lead(customer_id, history, facts, message_count=0,
                        silent_hours=None):
    """Pattern-recognize a chat. Returns:
      {verdict, importance_score, reasoning, suggested_action, elapsed_ms}
    verdict ∈ {'close','keep_open'} — is the lead still convertible?
    importance_score ∈ 0..100 — how worth pursuing right now (0 if close).
    reasoning: ≤25w explanation of the verdict + score.
    suggested_action: ≤25w next play if keep_open (empty if close).

    Returns None on Hermes timeout/error so callers can fall back to the
    deterministic score_lead path (never auto-closes on bad output)."""
    nm = (facts or {}).get("name") or _name_fallback(customer_id)
    yachts = (facts or {}).get("yachts") or "(none discussed)"
    dates = (facts or {}).get("dates") or "(no date)"
    party = (facts or {}).get("party_size") or "(unknown)"
    sh = (f"{silent_hours:.1f}h" if isinstance(silent_hours, (int, float))
          else "(unknown)")
    q = (
        "TASK: You are analyzing a Dubriani yacht-charter sales "
        "conversation to help the sales operator. Decide TWO things:\n"
        "  (a) Is there still a way to CONVERT this customer? "
        "(verdict: 'keep_open' if yes, 'close' if not)\n"
        "  (b) How IMPORTANT is this lead RIGHT NOW for the operator's "
        "attention? (importance_score: integer 0-100; "
        "100 = drop-everything-and-reply, 0 = no chance, ignore)\n\n"
        "PATTERN RECOGNITION RUBRIC — STRICT CASCADING ORDER. "
        "Evaluate rules 1→8 in order. The FIRST RULE THAT MATCHES "
        "determines the score range and CAPS it; subsequent positive "
        "signals CANNOT override that cap. Do not blend; do not "
        "weight 'engagement strength' against earlier rules. Apply "
        "the hard cap.\n\n"
        "  RULE 1 — SUPPLIER / VENDOR PITCH → verdict=close, score=0.\n"
        "    Customer introduces themselves as providing a service "
        "('we offer', 'our agency', 'i'm a designer/photographer/"
        "caterer/AI', 'collaboration', 'partnership offer to provide "
        "X'). They are NOT trying to book a yacht. Close immediately.\n\n"
        "  RULE 2 — SPAM / INCONSISTENT / SUSPICIOUS → verdict=close "
        "OR score≤30 (hard cap).\n"
        "    Any of:\n"
        "    - Date is 9+ months in the future without explicit "
        "advance-booking context (e.g. 'April 2028' from a May 2026 "
        "chat = suspicious; 'wedding 1 yr out' = legitimate).\n"
        "    - Customer-supplied data contradicts physics/logic "
        "(28 guests on a 15-pax yacht; 'today' + 'tomorrow' both "
        "claimed for the same booking; mixing 5+ unrelated yacht "
        "classes with no clear preference).\n"
        "    - Repeated probing questions about pricing/availability "
        "WITHOUT booking signals or contact details.\n"
        "    - Customer asks about yachts NOT in our catalog and "
        "demands specs.\n"
        "    Hard cap at 30. If clearly malicious (competitor probe, "
        "scam attempt), verdict=close, score=0.\n\n"
        "  RULE 3 — B2B / PARTNERSHIP INQUIRY → verdict=keep_open, "
        "score=10-25 (HARD CAP — do not exceed regardless of "
        "engagement).\n"
        "    ANY of these signals ANYWHERE in the conversation:\n"
        "    - 'partner', 'partnership', 'agency', 'broker'\n"
        "    - 'own boat', 'our yacht', 'we operate', 'we charter out'\n"
        "    - 'manage our', 'long-term arrangement'\n"
        "    - 'father/uncle is the owner/captain'\n"
        "    - 'we want to work with you', 'fleet collaboration'\n"
        "    - Booking date is 6+ months out paired with multiple "
        "deal structures or commercial language.\n"
        "    OPERATOR POLICY: B2B leads are managed separately by "
        "Zayn outside this channel. They ALWAYS rank below retail "
        "customers in /review. Strong engagement does NOT lift them "
        "out of this band. Suggested action: 'B2B inquiry — surface "
        "to Zayn for management when convenient, not a sales "
        "priority.'\n\n"
        "  RULE 4 — EXPLICIT REJECTION → verdict=close, score=0.\n"
        "    Customer explicitly said 'not interested', 'found another "
        "operator', 'cancelled trip', 'no longer needed', OR repeated "
        "price rejection 3+ times with no flexibility, OR ghosted "
        "14+ days after a clear rejection signal.\n\n"
        "  RULE 5 — TIRE-KICKER / MULTI-VENDOR BLAST → "
        "verdict=keep_open, score=30-45 (hard cap).\n"
        "    Copy-paste shopping template, asks for 5+ yachts at "
        "once, 'best rate'/'best price' phrasing, format suggests "
        "message sent to many operators in parallel. Price-sensitive "
        "but no commitment signals.\n\n"
        "  RULE 6 — DROP EVERYTHING → score=85-100.\n"
        "    All of these qualify:\n"
        "    - Booking date is today/tomorrow + customer engaged (any "
        "yacht). Date urgency trumps almost everything.\n"
        "    - Strong booking signals: dates fixed, group set, asked "
        "for payment link, ready to pay.\n"
        "    - Large-yacht booking (100+ ft tier: AK Royalty, Mila, "
        "Cante, Luna, Notorious, Sapphire, Athena, Skyfall, Tatti, "
        "Odysea, Royal Mirage, Asya, Finesse, Carina, Haigan) within "
        "14 days AND customer engaged.\n\n"
        "  RULE 7 — ACTIVE NEGOTIATION → score=65-85.\n"
        "    - Paylink-sent-silent (>2h): score 70-80. Suggest "
        "gentle follow-up.\n"
        "    - Active negotiation with fixed dates + group: 65-80.\n\n"
        "  RULE 8 — WAITING / FIRST CONTACT → score=45-65 (EXPLAIN "
        "WHY THEY WENT SILENT).\n"
        "    - Quoted-then-silent 12h+, no rejection. Suggest 'have "
        "you given up on booking?' nudge.\n"
        "    - First-time inquiry, hasn't fully replied yet → 50-65.\n"
        "    Reasoning MUST hypothesize the reason for silence "
        "(price hesitation? comparing? lost interest? busy?).\n\n"
        "  RULE 9 — COLD lead (no engagement weeks, no explicit "
        "reject) → score=20-35.\n\n"
        "REASONING REQUIREMENTS (the 'reasoning' field):\n"
        "  - Identify the SPECIFIC pattern matched, not generic 'hot "
        "signals'.\n"
        "  - If customer went silent >6h, hypothesize the LIKELY REASON "
        "(price hesitation, lost interest, comparing competitors, busy, "
        "etc.) — operator needs this to plan their nudge.\n"
        "  - Mention WHERE the chat last ended (e.g. 'after we quoted "
        "AED 4,200', 'after payment link sent', 'after asking party "
        "size').\n"
        "  - Never use the phrase 'hot signals'. Be specific.\n\n"
        "Output ONLY a JSON object with these exact keys:\n"
        '{"verdict":"keep_open","importance_score":0,'
        '"reasoning":"","suggested_action":""}\n'
        "  - verdict: 'keep_open' or 'close' (string)\n"
        "  - importance_score: integer 0-100. MUST be 0 if verdict='close'.\n"
        "  - reasoning: ≤30 words. Pattern matched + where chat ended + "
        "(if silent) likely reason for silence.\n"
        "  - suggested_action: ≤25 words. Specific next play if "
        "verdict='keep_open'. For paylink-sent-silent: 'send gentle "
        "follow-up'. For tire-kicker: 'don't waste premium options, "
        "anchor a deal'. Empty string if verdict='close'.\n\n"
        f"--- CUSTOMER FACTS ---\n"
        f"Name: {nm}\n"
        f"Yachts discussed: {yachts}\n"
        f"Date(s): {dates}\n"
        f"Party size: {party}\n"
        f"Message count: {message_count}\n"
        f"Silent for: {sh}\n\n"
        f"--- CONVERSATION ---\n{history or '(no history available)'}"
    )
    try:
        rc, out, err, elapsed = run_hermes(q, timeout=ANALYZE_LEAD_TIMEOUT)
    except subprocess.TimeoutExpired:
        log(f"hermes_analyze_lead: timeout {ANALYZE_LEAD_TIMEOUT}s "
            f"cid={customer_id}")
        return None
    except Exception as e:
        log("hermes_analyze_lead: error", repr(e))
        return None
    if rc != 0:
        log(f"hermes_analyze_lead: rc={rc} cid={customer_id} "
            f"err={(err or '')[:200]!r}")
        return None
    parsed, _ = extract_json(out)
    if not isinstance(parsed, dict):
        log(f"hermes_analyze_lead: no JSON cid={customer_id}")
        return None
    verdict = str(parsed.get("verdict") or "").strip().lower()
    if verdict not in ("close", "keep_open"):
        # Defensive: bad JSON should never trigger an auto-close.
        verdict = "keep_open"
    try:
        score = int(parsed.get("importance_score") or 0)
    except (TypeError, ValueError):
        score = 0
    score = max(0, min(100, score))
    if verdict == "close":
        score = 0  # invariant
    return {
        "verdict": verdict,
        "importance_score": score,
        "reasoning": str(parsed.get("reasoning") or "").strip()[:300],
        "suggested_action": str(
            parsed.get("suggested_action") or "").strip()[:300],
        "elapsed_ms": elapsed,
    }


# _md_escape moved to util.py (re-exported at top of server.py for
# backward compat).


def refresh_customer_facts_from_waha(customer_id):
    """Pull WAHA history for a customer + re-run extraction over the FULL
    conversation, then upsert. Returns the merged facts dict, or None on
    error / no-history. Shared by /refresh-facts (operator on-demand),
    /review (auto-heal for empty critical facts), and /pipeline-analyze
    (hourly cron — refresh-before-score)."""
    try:
        waha = waha_fetch_history(customer_id, limit=30)
        if (waha or {}).get("err") or not (waha or {}).get("count"):
            return None
        extracted = extract_customer_facts(
            waha["last_message"], waha["history"])
        cached = get_customer_facts(customer_id)
        merged = (_merge_facts(cached, extracted) if extracted
                  else _merge_facts(cached, None))
        if not (merged.get("name") or "").strip() and waha.get("push_name"):
            merged["name"] = waha["push_name"]
        upsert_customer_facts(customer_id,
                              merged.get("name") or "", merged)
        return merged
    except Exception as e:
        log(f"refresh_customer_facts_from_waha cid={customer_id!r} "
            f"err={e!r}")
        return None


# (moved to review.py — re-exported at top of file)


# --- drafting --------------------------------------------------------------

def load_system_prompt():
    try:
        with open(SYSTEM_PROMPT_PATH, encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


def build_query(p):
    """Compose the single -q query string from the request payload."""
    parts = []
    sp = load_system_prompt()
    if sp:
        parts.append(sp)
        parts.append("=" * 60)
    parts.append(
        "TASK: You are drafting a WhatsApp reply for Dubriani Yachts, in the "
        "persona and rules defined above. This is an internal drafting tool — "
        "a human operator reviews and approves every draft before it is sent."
    )

    name = (p.get("customer_name") or "the customer").strip()
    hist = (p.get("history") or "").strip()
    if hist:
        parts.append("\n--- CONVERSATION SO FAR ---\n" + hist)
    else:
        parts.append("\n--- This is a NEW conversation — no prior history. ---")

    parts.append(
        f"\n--- NEW MESSAGE FROM {name} ---\n"
        + (p.get("incoming_message") or "").strip()
    )

    rules = fetch_behavior_rules(p.get("customer_id"))
    if rules:
        parts.append(
            "\n--- ACTIVE BEHAVIOR RULES (learned corrections — must follow) ---\n"
            + "\n".join("- " + r for r in rules)
        )

    is_refine = p.get("mode") == "refine" and (p.get("refine_instruction") or "").strip()
    if is_refine:
        parts.append(
            "\n--- REVISION REQUESTED BY THE OPERATOR ---\n"
            "Your previous draft needs changes. Operator instruction: "
            + p["refine_instruction"].strip()
            + "\nProduce a revised reply that applies this instruction."
            "\n\nALSO judge whether this instruction expresses a DURABLE "
            "preference that should shape FUTURE drafts (a lasting style or "
            "policy preference, or a fact about this customer) — as opposed to "
            "a one-off tweak to this single message. If it IS durable, add a "
            "\"suggested_rule\" field to your JSON: {\"text\": \"<concise "
            "imperative rule, 25 words max>\", \"scope\": \"global\" if it "
            "should apply to all customers, or \"customer\" if only to this "
            "one}. If it is a one-off, omit suggested_rule entirely."
        )

    if not is_refine:
        parts.append(
            "\n--- TRIGGER DETECTION ---\n"
            "Judge whether the customer's newest message contains a TIME-BOUND "
            "COMMITMENT or PROMISE — e.g. \"I'll pay tomorrow\", \"let me confirm "
            "with my wife by Saturday\", \"call me Monday\", \"send the deposit "
            "tonight\". If it does, add a \"detected_trigger\" field to your JSON: "
            "{\"type\": \"<short label, e.g. payment_promised | callback_promised "
            "| decision_pending>\", \"reminder_hours\": <integer hours from now "
            "when Zayn should follow up>, \"context\": \"<one line>\", "
            "\"confidence\": <0.0-1.0>}. If there is no time-bound commitment, "
            "omit detected_trigger."
        )

    if not is_refine:
        parts.append(
            "\n--- CONVERSATION HEALTH ---\n"
            "Assess how this conversation is going and add a \"health\" field to "
            "your JSON: {\"score\": one of \"good\" (engaged, progressing), "
            "\"warm\" (active, not yet committed), \"at_risk\" (stalling, "
            "hesitation, price pushback, or gone quiet), \"cold\" (likely lost); "
            "\"reason\": \"<one short line>\"}. Always include health."
        )

    if is_refine:
        extra = "optionally plus \"suggested_rule\""
    else:
        extra = "plus \"health\", optionally plus \"detected_trigger\""
    parts.append(
        "\n--- RESPOND NOW ---\n"
        "Produce your reply using the EXACT JSON output format defined in your "
        "instructions above (the object with \"messages\" and \"notes_for_zayn\", "
        + extra + "). Output only that single JSON object — no markdown fences, "
        "no commentary before or after it."
    )
    return "\n".join(parts)


def build_improve_query(p):
    """Compose the -q query for a background improvement pass on an existing
    draft (FR-5). Hermes reviews the draft and returns a better version only
    if it can meaningfully improve it; otherwise it echoes the draft back."""
    parts = []
    sp = load_system_prompt()
    if sp:
        parts.append(sp)
        parts.append("=" * 60)
    parts.append(
        "TASK: A first-draft WhatsApp reply for Dubriani Yachts has already "
        "been written and is awaiting operator review. CRITICALLY REVIEW that "
        "draft against Maria's persona and rules above and the conversation. "
        "Improve it ONLY if you can make it meaningfully better — a stronger "
        "opening, better rule adherence, a higher chance of conversion, or a "
        "fixed mistake. If the draft is already good, leave it unchanged."
    )
    name = (p.get("customer_name") or "the customer").strip()
    hist = (p.get("history") or "").strip()
    if hist:
        parts.append("\n--- CONVERSATION SO FAR ---\n" + hist)
    else:
        parts.append("\n--- This is a NEW conversation — no prior history. ---")
    parts.append(
        f"\n--- NEWEST MESSAGE FROM {name} ---\n"
        + (p.get("incoming_message") or "").strip()
    )
    rules = fetch_behavior_rules(p.get("customer_id"))
    if rules:
        parts.append(
            "\n--- ACTIVE BEHAVIOR RULES (learned corrections — must follow) ---\n"
            + "\n".join("- " + r for r in rules)
        )
    cur = p.get("current_draft")
    if isinstance(cur, list):
        cur = "\n\n".join(str(m) for m in cur)
    parts.append("\n--- CURRENT DRAFT (under review) ---\n" + str(cur or "").strip())
    parts.append(
        "\n--- RESPOND NOW ---\n"
        "Output ONLY one JSON object — no markdown fences, no commentary:\n"
        '{"improved": true|false, "messages": ["<reply message>", ...], '
        '"note": "<if improved: one line on what you changed and why; if not: '
        'one line on why the draft is already good>"}\n'
        'Set "improved": true ONLY when your messages are a genuine improvement '
        'on the current draft. If you would not change anything meaningful, set '
        '"improved": false and echo the current draft text back in messages.'
    )
    return "\n".join(parts)


def build_learn_query(p):
    """Ask Hermes whether an operator's correction is a DURABLE behaviour rule
    (FR-5 learning loop) versus a one-off tweak."""
    fb = (p.get("feedback") or "").strip()
    draft = p.get("draft_text")
    if isinstance(draft, list):
        draft = "\n\n".join(str(m) for m in draft)
    name = (p.get("customer_name") or "the customer").strip()
    return "\n".join([
        "TASK: An operator (Zayn) reviewed a drafted WhatsApp reply for "
        "Dubriani Yachts and gave a correction. Decide whether that correction "
        "is a DURABLE preference that should shape FUTURE drafts — a lasting "
        "style or policy preference, or a fact about this customer — as opposed "
        "to a one-off tweak to this single message.",
        f"\nThe draft (for {name}) was:\n" + str(draft or "").strip(),
        "\nThe operator's correction was:\n" + fb,
        "\nRespond with ONLY one JSON object — no fences, no commentary:\n"
        '{"is_rule": true|false, "rule_text": "<if is_rule: a concise '
        'imperative rule, 25 words max>", "scope": "global" (applies to all '
        'customers) or "customer" (only this one), "reasoning": "<one line>"}\n'
        'If the correction is a one-off tweak, return {"is_rule": false}.',
    ])


# run_hermes / extract_json / extract_session moved to hermes_calls.py
# (re-exported at the top of server.py for backward compat).


# ============================================================================
# Pipeline Review — labels, signals, confidence dampening, sameday interrupt
# (see docs/pipeline-review-plan.md §1, §2a–§2c)
# ============================================================================

# LABELS frozenset, signal regexes, _LABEL_RANK, _HARD_DEMOTE_SIGNALS,
# _TIER_BELOW, confidence-dampening constants moved to labels.py
# (re-exported at top of server.py for backward compat).

# Sameday-interrupt cooldown — one ping per customer per 4 h.
SAMEDAY_INTERRUPT_TTL = int(os.environ.get("SAMEDAY_INTERRUPT_TTL", "14400"))

# Hourly sweep — process at most N customers per run; overflow next hour.
HOURLY_SWEEP_BATCH_LIMIT = int(os.environ.get("HOURLY_SWEEP_BATCH_LIMIT", "200"))
HOURLY_SWEEP_HERMES_CAP = int(os.environ.get("HOURLY_SWEEP_HERMES_CAP", "30"))

# Hourly pipeline analyzer — Hermes-driven importance ranking over active
# leads. Cap=100 covers a healthy pipeline in one run; with parallel=5
# workers and ~6s per customer, that's ~120s wall-clock worst-case, well
# inside the 600s cron timeout. Rotation (oldest-analyzed-first) still
# applies — if total pipeline ever exceeds the cap, the cap-spillover
# gets picked up next hour.
PIPELINE_ANALYZE_CAP = int(os.environ.get("PIPELINE_ANALYZE_CAP", "100"))
PIPELINE_ANALYZE_WORKERS = int(os.environ.get("PIPELINE_ANALYZE_WORKERS", "5"))

# /review inline auto-heal — for customers with missing critical facts
# (no name AND no yacht), refresh from WAHA history before rendering.
# Bounded so /review latency stays under 10s even with a stale cohort.
# 5 parallel refreshes × ~5s per Hermes extract = ~5-7s wall-clock.
# (moved to review.py — re-exported at top of file)

# Proactive follow-up engine — see docs/cowork-targeted-integration-plan.md
# (follow-up engine section). Default ON; operator can flip to disable.
FOLLOWUP_ENGINE_ENABLED = _envflag("FOLLOWUP_ENGINE_ENABLED", "true")
FOLLOWUP_BATCH_LIMIT = int(os.environ.get("FOLLOWUP_BATCH_LIMIT", "10"))
# Max proactive follow-ups sent BEFORE the customer responds. After
# this many sent in a row without engagement, the engine treats the
# customer as exhausted and skips them. Resets to 0 on the next
# customer_message event. Set via env to tune without redeploy.
FOLLOWUP_CAP = int(os.environ.get("FOLLOWUP_CAP", "2"))

# /review report rendering caps (Telegram 4096-char limit safe).
# (moved to review.py — re-exported at top of file)

# _TIER_BELOW, _LABEL_RANK, _HARD_DEMOTE_SIGNALS moved to labels.py
# (re-exported at top of server.py for backward compat).


def get_correction_count(auto_signal, window_days=CORRECTION_WINDOW_DAYS):
    """Count label_corrections rows for this signal in the last N days.
    Returns 0 on DB error — fail-open (don't dampen if we can't read)."""
    sig = (auto_signal or "").replace("'", "''")
    out, err = _psql(
        f"SELECT count(*) FROM label_corrections WHERE auto_signal = '{sig}' "
        f"AND created_at > now() - interval '{int(window_days)} days'"
    )
    if err:
        return 0
    try:
        return int((out or "0").strip().splitlines()[0])
    except (ValueError, IndexError):
        return 0


def compute_confidence(auto_signal):
    """Confidence ∈ [CONFIDENCE_FLOOR, 1.0] from recent corrections."""
    n = get_correction_count(auto_signal)
    raw = 1.0 - (n / CORRECTION_DAMPENING_DIVISOR)
    return max(CONFIDENCE_FLOOR, min(1.0, raw))


def _has_recent_payment_intent(customer_id, hours=24):
    """True if customer_triggers has payment_* or booking_intent in last N h."""
    cid = (customer_id or "").replace("'", "''")
    out, err = _psql(
        f"SELECT count(*) FROM customer_triggers WHERE customer_id = '{cid}' "
        "AND type IN ('payment_promised','payment_link_sent','booking_intent') "
        f"AND created_at > now() - interval '{int(hours)} hours'"
    )
    if err:
        return False
    try:
        return int((out or "0").strip().splitlines()[0]) > 0
    except (ValueError, IndexError):
        return False


def _has_recent_payment_link_sent(customer_id, hours=48):
    """True if autonomous_sends has a payment_link_sent row for this
    customer within the last N hours. Used by the payment-confirmation
    chat-signal detection — only trust 'paid' / 'done' / 'transferred'
    language when we ACTUALLY sent them a link recently."""
    cid = (customer_id or "").replace("'", "''")
    out, err = _psql(
        "SELECT count(*) FROM autonomous_sends "
        f"WHERE customer_id = '{cid}' AND kind = 'payment_link_sent' "
        f"AND sent_at > now() - interval '{int(hours)} hours'"
    )
    if err:
        return False
    try:
        return int((out or "0").strip().splitlines()[0]) > 0
    except (ValueError, IndexError):
        return False


# _MONTH_NUM + _parse_booking_date moved to labels.py
# (re-exported at top of server.py for backward compat).


def _is_past_booking_date(dates_str):
    """True if the parsed date is in the past by more than 1 day."""
    import datetime as _dt
    d = _parse_booking_date(dates_str)
    if d is None:
        return False
    return (_dt.date.today() - d).days >= 1


def compute_label(latest_message, facts):
    """Match signal heuristics against latest message + cached facts.
    Returns (target_label, signal, evidence). NO DB writes. NO dampening
    (caller applies that)."""
    msg = latest_message or ""
    customer_id = (facts or {}).get("customer_id", "")
    mc = (facts or {}).get("message_count", 0) or 0
    yachts = ((facts or {}).get("yachts") or "").strip()
    dates = ((facts or {}).get("dates") or "").strip()

    # Payment-confirmed chat signal takes HIGHEST priority — overrides
    # everything (including past-date COLD). Only fires when:
    #   (a) the customer's latest message matches a confirm phrase, AND
    #   (b) we logged a payment_link_sent for them in the last 48h.
    # Both gates needed — a casual 'i paid for parking yesterday' without
    # a recent link can't false-positive to CONFIRMED. Operator can still
    # always /label override.
    if customer_id and PAYMENT_CONFIRMED_RE.search(msg) \
            and _has_recent_payment_link_sent(customer_id, hours=48):
        m = PAYMENT_CONFIRMED_RE.search(msg)
        evidence = msg[max(0, m.start() - 10):m.end() + 20].strip()
        return ("CONFIRMED", "payment_confirmed_chat", evidence[:200])

    # Past-date demotion takes precedence over all OTHER positive signals.
    # The booking date has come and gone — the customer is either a
    # missed sale or a future re-engagement candidate; either way it
    # should NOT sit at the top of /review as HOT.
    if dates and _is_past_booking_date(dates):
        return ("COLD", "date_passed", dates[:200])

    if customer_id and _has_recent_payment_intent(customer_id):
        return ("HOT", "payment_intent", "trigger in last 24h")

    m = MONEY_RE.search(msg)
    if m:
        snippet = msg[max(0, m.start() - 10):m.end() + 30].strip()
        return ("HOT", "money_mentioned", snippet[:200])

    m = LETS_DO_IT_RE.search(msg)
    if m:
        return ("HOT", "lets_do_it", m.group(0)[:200])

    if SAME_DAY_RE.search(msg) and (YACHT_KEYWORD_RE.search(msg) or yachts):
        return ("HOT", "same_day_booking", msg[:200])

    if yachts and "," in yachts and mc >= 4 and "?" not in msg:
        return ("HOT", "multi_yacht_engaged", yachts[:200])

    if PRICING_INQUIRED_RE.search(msg):
        return ("WARM", "pricing_inquired", msg[:200])

    if dates:
        return ("WARM", "date_asked_no_commit", dates[:200])

    if mc >= 5:
        return ("WARM", "engaged_5plus", f"msg_count={mc}")

    return ("NEW", "new_window", f"msg_count={mc}")


def get_current_label_row(customer_id):
    """Read label state + a few denorm fields. Returns dict or None.
    Resolves merged_into so a read for a non-canonical cid returns
    the canonical row's label state."""
    customer_id = canonicalize_cid(customer_id)
    cid = (customer_id or "").replace("'", "''")
    sql = (
        "SELECT label, "
        "COALESCE(to_char(label_updated_at,'YYYY-MM-DD\"T\"HH24:MI:SSOF'),''), "
        "COALESCE(to_char(label_locked_until,'YYYY-MM-DD\"T\"HH24:MI:SSOF'),''), "
        "COALESCE(message_count,0), COALESCE(name,''), "
        "COALESCE(yachts,''), COALESCE(dates,'') "
        f"FROM customer_facts WHERE customer_id = '{cid}'"
    )
    out, err = _psql(sql)
    if err or not (out or "").strip():
        return None
    parts = (out.splitlines() or [""])[0].split("|")
    if len(parts) < 7:
        return None
    try:
        mc = int((parts[3].strip() or "0"))
    except ValueError:
        mc = 0
    return {
        "customer_id": customer_id,
        "label": parts[0].strip(),
        "label_updated_at": parts[1].strip(),
        "label_locked_until": parts[2].strip(),
        "message_count": mc,
        "name": parts[4].strip(),
        "yachts": parts[5].strip(),
        "dates": parts[6].strip(),
    }


def apply_label_transition(customer_id, from_label, to_label, signal,
                           evidence, message_count, created_by="system"):
    """UPDATE customer_facts.label + INSERT customer_label_history.
    Resolves merged_into so the label always lands on the canonical
    row regardless of which cid the caller used."""
    customer_id = canonicalize_cid(customer_id)
    cid = (customer_id or "").replace("'", "''")
    upd = (
        f"UPDATE customer_facts SET label = {_lit(to_label)}, "
        f"label_updated_at = now() WHERE customer_id = '{cid}'; "
    )
    fl = _lit(from_label) if from_label else "NULL"
    ins = (
        "INSERT INTO customer_label_history "
        "(customer_id, from_label, to_label, signal, evidence, "
        "message_count, created_by) VALUES "
        f"('{cid}', {fl}, {_lit(to_label)}, {_lit(signal or '')}, "
        f"{_lit(evidence or '')}, {int(message_count or 0)}, "
        f"{_lit(created_by)})"
    )
    return _psql(upd + ins)


def upsert_conversation_state(customer_id, event):
    """Per-event timestamp updater. event ∈ {customer_message, operator_reply,
    nudge_drafted, draft_posted}. Atomic UPSERT via ON CONFLICT.
    draft_posted is Redis-only (sets the draft:posted:<id> flag the
    sameday-interrupt check reads — no DB row needed).
    Resolves merged_into so all timing state lands on the canonical
    customer's row."""
    customer_id = canonicalize_cid(customer_id)
    cid = (customer_id or "").replace("'", "''")
    if event == "draft_posted":
        _redis(["SET", f"draft:posted:{customer_id}", "1", "EX", "86400"])
        return ("ok", None)
    if event == "customer_message":
        sql = (
            "INSERT INTO conversation_state "
            "(customer_id, last_customer_message_at, followup_count, "
            "updated_at) "
            f"VALUES ('{cid}', now(), 0, now()) "
            "ON CONFLICT (customer_id) DO UPDATE "
            # Reset followup_count to 0 — customer engaged, the
            # proactive-followup cap resets for the next silence.
            "SET last_customer_message_at = now(), "
            "    followup_count = 0, "
            "    updated_at = now()"
        )
    elif event == "operator_reply":
        sql = (
            "INSERT INTO conversation_state "
            "(customer_id, last_operator_reply_at, reengage_attempts, updated_at) "
            f"VALUES ('{cid}', now(), 0, now()) "
            "ON CONFLICT (customer_id) DO UPDATE "
            "SET last_operator_reply_at = now(), reengage_attempts = 0, "
            "    updated_at = now()"
        )
    elif event == "nudge_drafted":
        sql = (
            "INSERT INTO conversation_state "
            "(customer_id, last_nudge_drafted_at, followup_count, "
            "updated_at) "
            f"VALUES ('{cid}', now(), 1, now()) "
            "ON CONFLICT (customer_id) DO UPDATE "
            "SET last_nudge_drafted_at = now(), "
            "    reengage_attempts = conversation_state.reengage_attempts + "
            "      CASE WHEN conversation_state.last_customer_message_at IS NOT NULL "
            "            AND conversation_state.last_customer_message_at "
            "                < now() - interval '24 hours' "
            "           THEN 1 ELSE 0 END, "
            # Increment cap counter — bounds the proactive engine to
            # FOLLOWUP_CAP unsolicited follow-ups per silence window.
            "    followup_count = conversation_state.followup_count + 1, "
            "    updated_at = now()"
        )
    else:
        return ("", f"unknown event: {event}")
    return _psql(sql)


def _silence_window_for(label, silent_hrs):
    """Map (label, silence-hours) to a ghost-recovery window key, or None.
    Mirrors the trigger table in the proactive-follow-up engine spec."""
    if label in ("HOT", "NEEDS_ATTENTION"):
        if 0.5 <= silent_hrs <= 2.0:
            return "hot_30m_2h"
        if 2.0 < silent_hrs <= 24.0:
            return "hot_2h_24h"
    if label == "WARM":
        if 24.0 <= silent_hrs <= 72.0:
            return "warm_24h_72h"
    if label == "COLD":
        if 72.0 <= silent_hrs <= 168.0:  # 3-7 days
            return "cold_lastshot"
    return None


def scan_followup_eligibility():
    """Return up to FOLLOWUP_BATCH_LIMIT customers eligible for a proactive
    follow-up this sweep. Each item: {customer_id, name, label,
    silence_hours, silence_window}. Skip-gates checked in SQL where possible,
    in Python where atomicity matters (Redis draft:posted)."""
    if not FOLLOWUP_ENGINE_ENABLED:
        return []
    # Single SELECT pulls everything we need; LATERAL pick of latest mode.
    sql = (
        "SELECT cs.customer_id, "
        "COALESCE(cf.name, ''), "
        "cf.label, "
        "EXTRACT(EPOCH FROM (now() - cs.last_customer_message_at))/3600, "
        "(cs.last_operator_reply_at > cs.last_customer_message_at) AS we_replied, "
        "(cf.label_locked_until > now()) AS locked, "
        "COALESCE(cm.mode, 'approval'), "
        "(cs.last_nudge_drafted_at > cs.last_customer_message_at) AS already_drafted, "
        # Cap counter — proactive engine bounds itself to FOLLOWUP_CAP
        # unsolicited follow-ups before exhausting; resets on customer
        # message (see upsert_conversation_state).
        "COALESCE(cs.followup_count, 0) "
        "FROM conversation_state cs "
        "LEFT JOIN customer_facts cf USING (customer_id) "
        "LEFT JOIN LATERAL ("
        "  SELECT mode FROM conversation_modes "
        "  WHERE customer_id = cs.customer_id "
        "  ORDER BY id DESC LIMIT 1"
        ") cm ON TRUE "
        "WHERE cs.last_customer_message_at IS NOT NULL "
        "  AND (cs.last_operator_reply_at IS NULL "
        "       OR cs.last_operator_reply_at < cs.last_customer_message_at) "
        "  AND now() - cs.last_customer_message_at > interval '30 minutes' "
        "  AND now() - cs.last_customer_message_at < interval '7 days' "
        "  AND COALESCE(cs.followup_count, 0) < " + str(FOLLOWUP_CAP) + " "
        "ORDER BY cs.last_customer_message_at ASC "
        "LIMIT 80"
    )
    out, err = _psql(sql, timeout=15)
    if err:
        log("followup_scan err:", err)
        return []
    candidates = []
    for line in (out or "").strip().splitlines():
        parts = line.split("|")
        if len(parts) < 9:
            continue
        cid = parts[0].strip()
        if not cid:
            continue
        try:
            silent_hrs = float(parts[3].strip())
        except (ValueError, IndexError):
            continue
        label = parts[2].strip() or "NEW"
        locked = parts[5].strip().lower() == "t"
        mode = parts[6].strip()
        already_drafted = parts[7].strip().lower() == "t"
        try:
            followup_count = int(parts[8].strip() or "0")
        except (ValueError, IndexError):
            followup_count = 0
        # Skip-gates (Python-side; SQL already filtered the timing band)
        if locked:
            continue
        if mode == "autonomous":
            continue
        if already_drafted:
            continue
        # Defensive Python-side gate — SQL already filters, but keep this
        # for any future code path that bypasses scan_followup_eligibility.
        if followup_count >= FOLLOWUP_CAP:
            continue
        # Skip if a normal customer-message draft is still pending operator
        # action (Redis flag set by Send Draft to Telegram).
        red_out, _ = _redis(["GET", f"draft:posted:{cid}"])
        if (red_out or "").strip():
            continue
        window = _silence_window_for(label, silent_hrs)
        if not window:
            continue
        candidates.append({
            "customer_id": cid,
            "name": parts[1].strip(),
            "label": label,
            "silence_hours": round(silent_hrs, 2),
            "silence_window": window,
        })
        if len(candidates) >= FOLLOWUP_BATCH_LIMIT:
            break
    return candidates


# (moved to review.py — re-exported at top of file)


def _seconds_since(ts_str):
    """Parse 'YYYY-MM-DD HH:MI:SS+TZ' to seconds-ago. None if blank/error."""
    s = (ts_str or "").strip()
    if not s:
        return None
    try:
        import datetime as _dt
        # Postgres uses '+00' or '+0000'; normalise to ISO
        s2 = s.replace(" ", "T")
        if s2.endswith("+00"):
            s2 = s2[:-3] + "+0000"
        if "+" not in s2[-6:] and "-" not in s2[-6:]:
            s2 += "+0000"
        dt = _dt.datetime.fromisoformat(s2[:19]).replace(
            tzinfo=_dt.timezone.utc)
        now = _dt.datetime.now(_dt.timezone.utc)
        return max(0, int((now - dt).total_seconds()))
    except Exception:
        return None


def read_lead_summary(filter_label=None):
    """Return list of dicts (one per customer) ready for scoring.
    filter_label ∈ {None, 'hot', 'warm', 'cold', 'all'} (None == all)."""
    where = ""
    fl = (filter_label or "").lower()
    if fl == "hot":
        where = "WHERE label IN ('HOT','NEEDS_ATTENTION')"
    elif fl == "warm":
        where = "WHERE label = 'WARM'"
    elif fl == "cold":
        where = "WHERE label = 'COLD'"
    sql = (
        "SELECT customer_id, COALESCE(name,''), label, "
        "COALESCE(to_char(label_updated_at,'YYYY-MM-DD HH24:MI:SSOF'),''), "
        "COALESCE(to_char(label_locked_until,'YYYY-MM-DD HH24:MI:SSOF'),''), "
        "(label_locked_until > now()) AS lock_active, "
        "COALESCE(message_count,0), COALESCE(yachts,''), COALESCE(dates,''), "
        "COALESCE(party_size,''), "
        "COALESCE(to_char(last_customer_message_at,'YYYY-MM-DD HH24:MI:SSOF'),''), "
        "COALESCE(to_char(last_operator_reply_at,'YYYY-MM-DD HH24:MI:SSOF'),''), "
        "COALESCE(to_char(last_review_seen_at,'YYYY-MM-DD HH24:MI:SSOF'),''), "
        "COALESCE(to_char(last_nudge_drafted_at,'YYYY-MM-DD HH24:MI:SSOF'),''), "
        "COALESCE(to_char(last_payment_link_at,'YYYY-MM-DD HH24:MI:SSOF'),''), "
        "COALESCE(to_char(last_payment_promised_at,'YYYY-MM-DD HH24:MI:SSOF'),''), "
        "COALESCE(to_char(last_booking_intent_at,'YYYY-MM-DD HH24:MI:SSOF'),''), "
        "COALESCE(to_char(last_rejection_at,'YYYY-MM-DD HH24:MI:SSOF'),''), "
        "COALESCE(last_rejection_kind,''), "
        "COALESCE(recent_notes,''), "
        # Hermes importance fields (NULL until first /pipeline-analyze run).
        "COALESCE(importance_score::text,''), "
        "COALESCE(importance_reasoning,''), "
        "COALESCE(suggested_action,''), "
        "COALESCE(to_char(importance_analyzed_at,'YYYY-MM-DD HH24:MI:SSOF'),'') "
        f"FROM v_lead_summary {where}"
    )
    out, err = _psql(sql, timeout=20)
    if err:
        log("read_lead_summary err:", err)
        return []
    rows = []
    for line in (out or "").strip().splitlines():
        parts = line.split("|")
        if len(parts) < 20:
            continue
        try:
            mc = int((parts[6].strip() or "0"))
        except ValueError:
            mc = 0
        try:
            imp = int(parts[20].strip()) if len(parts) > 20 and parts[20].strip() else None
        except ValueError:
            imp = None
        row = {
            "customer_id": parts[0].strip(),
            "name": parts[1].strip(),
            "label": parts[2].strip(),
            "label_updated_at": parts[3].strip(),
            "label_locked_until": parts[4].strip(),
            "label_locked_active": parts[5].strip().startswith("t"),
            "message_count": mc,
            "yachts": parts[7].strip(),
            "dates": parts[8].strip(),
            "party_size": parts[9].strip(),
            "last_customer_message_at": parts[10].strip(),
            "last_customer_message_at_seconds": _seconds_since(parts[10]),
            "last_operator_reply_at_seconds": _seconds_since(parts[11]),
            "last_review_seen_at_seconds": _seconds_since(parts[12]),
            "last_nudge_drafted_at_seconds": _seconds_since(parts[13]),
            "last_payment_link_at_seconds": _seconds_since(parts[14]),
            "last_payment_promised_at_seconds": _seconds_since(parts[15]),
            "last_booking_intent_at": parts[16].strip(),
            "last_rejection_at_seconds": _seconds_since(parts[17]),
            "last_rejection_kind": parts[18].strip(),
            "recent_notes": parts[19].strip(),
            "importance_score": imp,
            "importance_reasoning": parts[21].strip() if len(parts) > 21 else "",
            "suggested_action": parts[22].strip() if len(parts) > 22 else "",
            "importance_analyzed_at_seconds":
                _seconds_since(parts[23]) if len(parts) > 23 else None,
        }
        rows.append(row)
    return rows


# (moved to review.py — re-exported at top of file)
def sanitize_draft_messages(messages):
    """Strip any hallucinated payment URLs / placeholders from LLM-written
    drafts. The system prompt forbids the LLM from pasting `pay.nomodapp.com`
    (the real Nomod URL gets appended deterministically by Build Payment
    Message when should_send_payment is true). But the LLM still occasionally
    writes it — operator-visible regression. This deterministic post-pass
    guarantees the draft never carries a bare/invented URL.

    Returns (sanitized_messages_list, did_strip: bool)."""
    if not isinstance(messages, list):
        return messages, False
    bad_patterns = [
        re.compile(r"pay\.nomodapp\.com[/\w?=&%-]*", re.IGNORECASE),
        re.compile(r"https?://pay\.nomodapp\.com[/\w?=&%-]*", re.IGNORECASE),
        re.compile(r"\[link\]", re.IGNORECASE),
        re.compile(r"\[payment\s*link\]", re.IGNORECASE),
        re.compile(r"\[insert\s*link\]", re.IGNORECASE),
    ]
    cleaned = []
    stripped = False
    for m in messages:
        if not isinstance(m, str):
            cleaned.append(m)
            continue
        out = m
        for pat in bad_patterns:
            if pat.search(out):
                stripped = True
                out = pat.sub("", out)
        # Tidy double spaces / stray colons left over after substring removal
        out = re.sub(r"(here'?s the (payment )?link[^:]*:\s*)$",
                     "want me to send the payment link to lock it in?",
                     out, flags=re.IGNORECASE)
        out = re.sub(r"\s+:\s*[.!?]", ".", out)  # ": ." -> "."
        out = re.sub(r"\s{2,}", " ", out).strip()
        # If the message became empty or weird-trailing, default fall-back
        if not out or len(out) < 6:
            out = "want me to send the payment link to lock it in?"
        cleaned.append(out)
    return cleaned, stripped


# (moved to review.py — re-exported at top of file)


# (moved to review.py — re-exported at top of file)


def mark_review_seen(customer_ids):
    """UPDATE conversation_state.last_review_seen_at = now() for the listed
    customers. Batched into one UPDATE for efficiency."""
    if not customer_ids:
        return None, None
    quoted = ",".join("'" + cid.replace("'", "''") + "'" for cid in customer_ids)
    sql = (
        "INSERT INTO conversation_state (customer_id, last_review_seen_at, updated_at) "
        f"SELECT cid, now(), now() FROM unnest(ARRAY[{quoted}]) AS cid "
        "ON CONFLICT (customer_id) DO UPDATE "
        "SET last_review_seen_at = now(), updated_at = now()"
    )
    return _psql(sql)


def _humanize_signal(signal, evidence, created_by):
    """Translate a raw label-history signal string into operator English.

    Inputs like 'auto:money_mentioned' / 'hourly_sweep:cold_decay' /
    'manual:/label' / 'manual:/snooze' / 'manual:/feedback' →
    'They mentioned money/budget' / 'No activity for 7+ days — went cold'
    / 'You manually set the label' etc.
    """
    sig = (signal or "").strip()
    ev = (evidence or "").strip()
    is_manual = sig.startswith("manual:") or created_by == "operator"
    key = sig.split(":", 1)[-1]  # strip auto:/hourly_sweep:/manual: prefix

    descriptions = {
        # auto + hourly_sweep signal types
        "payment_intent":          "Strong booking intent detected",
        "money_mentioned":         "Mentioned money/budget",
        "lets_do_it":              "Indicated they want to book",
        "same_day_booking":        "Asked about same-day booking",
        "multi_yacht_engaged":     "Engaged on multiple yachts",
        "pricing_inquired":        "Asked about pricing",
        "date_asked_no_commit":    "Discussed dates, no commitment yet",
        "engaged_5plus":           "5+ messages exchanged — engaged",
        "new_window":              "Early conversation",
        "cold_decay":              "Went cold (7+ days silent)",
        "no_facts_row":            "(no customer facts on file)",
        "locked":                  "Label currently locked",
        "hourly_noop":             "(no change on sweep)",
        # manual signal types
        "/label":                  "You manually set the label",
        "/snooze":                 (
            "You snoozed " + ev.replace("snoozed ", "").strip()
            if ev else "You snoozed this lead"
        ),
        "/feedback":               "Operator feedback applied",
    }
    desc = descriptions.get(key)
    if desc is None:
        # Unknown signal — fall back to a clean form.
        if is_manual:
            return f"Manual update ({key})"
        return f"Signal: {key}"
    if is_manual and not desc.startswith("You "):
        return f"You: {desc}"
    return desc


def sameday_interrupt_check(customer_id, name, dates):
    """(interrupt_required, alert_text). Blocks if a draft has been posted
    (Redis key draft:posted:<cid>) or if we've already alerted in the last
    4 h (interrupt:fired:<cid>). Arms the cooldown on a fresh fire."""
    cid = (customer_id or "").strip()
    if not cid:
        return (False, None)
    out, _err = _redis(["GET", f"draft:posted:{cid}"])
    if (out or "").strip():
        return (False, None)
    out, _err = _redis(["GET", f"interrupt:fired:{cid}"])
    if (out or "").strip():
        return (False, None)
    _redis(["SET", f"interrupt:fired:{cid}", "1",
            "EX", str(SAMEDAY_INTERRUPT_TTL)])
    n = (name or "").strip() or cid[:14]
    d = (dates or "").strip() or "today/tonight"
    return (True, f"⚡ SAME-DAY ASK — {n} · {d} · we haven't drafted yet")


# --- HTTP ------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        # Force the response onto the socket NOW — without this, bytes
        # sit in BufferedWriter until the handler returns. Most callers
        # don't notice, but handle_nomod_webhook needs Nomod to see the
        # 200 BEFORE its background processing continues. Universal +
        # harmless for normal handlers.
        try:
            self.wfile.flush()
        except (OSError, BrokenPipeError):
            # Client disconnected mid-response — don't crash any
            # background work that may follow this send().
            pass

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"status": "ok", "service": "hermes-bridge"})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path not in ("/draft", "/improve", "/learn", "/rules",
                             "/autosend-check", "/save-rule", "/set-mode",
                             "/caps", "/autosend-state", "/customer-facts",
                             "/debounce", "/payment-link", "/feedback",
                             "/label-eval", "/conversation-state",
                             "/hourly-sweep",
                             "/review", "/draft-followup",
                             "/info", "/label", "/snooze",
                             "/queue",
                             "/followup-action",
                             "/refresh-facts",
                             "/draft-freshness",
                             "/autonomous-log",
                             "/poll-payments",
                             "/lead-analyze-disregard",
                             "/pipeline-analyze",
                             "/nomod-webhook"):
            self._send(404, {"error": "not found"})
            return
        # /nomod-webhook is the ONLY endpoint without X-Bridge-Token —
        # auth is via svix HMAC signature (Nomod can't send our internal
        # token). The handler MUST verify the signature itself.
        if self.path != "/nomod-webhook" and (
                not TOKEN or self.headers.get("X-Bridge-Token") != TOKEN):
            self._send(401, {"error": "unauthorized"})
            return
        # Read raw body. Webhook needs raw text for HMAC verification —
        # parsing JSON would alter byte order / whitespace and break
        # the signature check. All other endpoints parse it as JSON
        # below.
        try:
            n = int(self.headers.get("Content-Length", "0"))
            raw_body = (self.rfile.read(n).decode("utf-8")
                        if n > 0 else "{}")
        except Exception as e:
            self._send(400, {"error": f"bad request: {e}"})
            return
        # Webhook path: dispatch BEFORE json.loads to preserve raw bytes.
        if self.path == "/nomod-webhook":
            handle_nomod_webhook(
                dict(self.headers), raw_body, self._send)
            return
        try:
            payload = json.loads(raw_body) if raw_body else {}
        except json.JSONDecodeError as e:
            self._send(400, {"error": f"bad request: {e}"})
            return
        if self.path == "/save-rule":
            handle_save_rule(payload, self._send)
        elif self.path == "/improve":
            handle_improve(payload, self._send)
        elif self.path == "/learn":
            handle_learn(payload, self._send)
        elif self.path == "/rules":
            handle_rules(payload, self._send)
        elif self.path == "/autosend-check":
            handle_autosend_check(payload, self._send)
        elif self.path == "/set-mode":
            handle_set_mode(payload, self._send)
        elif self.path == "/caps":
            self._send(200, {"ok": True, "text": caps_status_text()})
        elif self.path == "/autosend-state":
            handle_autosend_state(payload, self._send)
        elif self.path == "/customer-facts":
            handle_customer_facts(payload, self._send)
        elif self.path == "/debounce":
            handle_debounce(payload, self._send)
        elif self.path == "/payment-link":
            handle_payment_link(payload, self._send)
        elif self.path == "/feedback":
            handle_feedback(payload, self._send)
        elif self.path == "/label-eval":
            handle_label_eval(payload, self._send)
        elif self.path == "/conversation-state":
            handle_conversation_state(payload, self._send)
        elif self.path == "/hourly-sweep":
            handle_hourly_sweep(payload, self._send)
        elif self.path == "/review":
            handle_review(payload, self._send)
        elif self.path == "/draft-followup":
            handle_draft_followup(payload, self._send)
        elif self.path == "/info":
            handle_info(payload, self._send)
        elif self.path == "/label":
            handle_label(payload, self._send)
        elif self.path == "/snooze":
            handle_snooze(payload, self._send)
        elif self.path == "/queue":
            handle_queue(payload, self._send)
        elif self.path == "/followup-action":
            handle_followup_action(payload, self._send)
        elif self.path == "/refresh-facts":
            handle_refresh_facts(payload, self._send)
        elif self.path == "/draft-freshness":
            handle_draft_freshness(payload, self._send)
        elif self.path == "/autonomous-log":
            handle_autonomous_log(payload, self._send)
        elif self.path == "/poll-payments":
            handle_poll_payments(payload, self._send)
        elif self.path == "/lead-analyze-disregard":
            handle_lead_analyze_disregard(payload, self._send)
        elif self.path == "/pipeline-analyze":
            handle_pipeline_analyze(payload, self._send)
        else:
            handle_draft(payload, self._send)

    # (moved to routes.py — handle_<name>(payload, self._send))
def main():
    if not TOKEN:
        log("FATAL: BRIDGE_TOKEN not set — refusing to start")
        sys.exit(1)
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    log(f"hermes-bridge listening on 0.0.0.0:{PORT} "
        f"(hermes timeout {HERMES_TIMEOUT}s)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
