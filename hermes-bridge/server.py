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

HOME = os.path.expanduser("~")
BRIDGE_DIR = os.path.join(HOME, "hermes-bridge")
SYSTEM_PROMPT_PATH = os.path.join(BRIDGE_DIR, "system-prompt.md")
HERMES = os.path.join(HOME, ".local", "bin", "hermes")

TOKEN = os.environ.get("BRIDGE_TOKEN", "")
PORT = int(os.environ.get("BRIDGE_PORT", "8788"))
HERMES_TIMEOUT = int(os.environ.get("BRIDGE_HERMES_TIMEOUT", "120"))

PG_CONTAINER = os.environ.get("BRIDGE_PG_CONTAINER", "n8n-postgres-1")
PG_USER = os.environ.get("BRIDGE_PG_USER", "hermes_rw")
PG_DB = os.environ.get("BRIDGE_PG_DB", "n8n")
REDIS_CONTAINER = os.environ.get("BRIDGE_REDIS_CONTAINER", "n8n-redis-1")
AUTOSEND_TTL = int(os.environ.get("BRIDGE_AUTOSEND_TTL", "3600"))
DEBOUNCE_TTL = int(os.environ.get("BRIDGE_DEBOUNCE_TTL", "120"))
QUEUE_TTL = int(os.environ.get("BRIDGE_QUEUE_TTL", "86400"))  # 24h — drafts auto-expire

# --- /feedback (operator behavioural-feedback command) ---------------------
FEEDBACK_TTL = int(os.environ.get("BRIDGE_FEEDBACK_TTL", "600"))
FEEDBACK_MAX_GLOBAL = int(os.environ.get("FEEDBACK_MAX_GLOBAL", "20"))
FEEDBACK_MAX_SCENARIO = int(os.environ.get("FEEDBACK_MAX_SCENARIO", "5"))
FEEDBACK_MAX_PER_CUSTOMER = int(os.environ.get("FEEDBACK_MAX_PER_CUSTOMER", "5"))


def _envflag(key, default):
    return os.environ.get(key, default).strip().lower() in ("true", "1", "yes")


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
WAHA_API_KEY = os.environ.get("WAHA_API_KEY", "")
WAHA_BASE = os.environ.get("WAHA_BASE", "").rstrip("/")

NOMOD_API_KEY = os.environ.get("NOMOD_API_KEY", "")
NOMOD_API_BASE = os.environ.get("NOMOD_API_BASE",
                                "https://api.nomod.com/v1").rstrip("/")
PAYMENTS_ENABLED = _envflag("PAYMENTS_ENABLED", "true")

SESSION_RE = re.compile(r"session_id:\s*(\S+)")
FENCE_RE = re.compile(r"```(?:json)?", re.IGNORECASE)
VALID_SCOPES = ("global", "customer", "scenario", "tier")

# --- customer facts (feature-header) ---------------------------------------
FACTS_EXTRACT_TIMEOUT = int(os.environ.get("BRIDGE_FACTS_TIMEOUT", "15"))
# yacht keywords worth gating extraction on — curated from system-prompt.md §7
YACHT_NAMES = (
    "satoshi", "enigma", "aurora", "azimut", "sunseeker", "ferretti",
    "pershing", "benetti", "beneteau", "galeon", "riva", "princess",
    "lamborghini", "sanlorenzo", "maiora", "baglietto", "elan", "elise",
    "diana", "zenith", "bliss", "von dutch", "cabo", "belle", "monaco",
    "cante", "carina", "haigan", "zirve", "luna", "notorious", "asya",
    "zeta", "dolce vita", "tatti", "sapphire", "odysea", "royalty",
    "mila", "athena", "skyfall", "finesse", "sofiya", "eclipse",
    "royal mirage", "yacht",
)
_FACTS_DATE_RE = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|january|february|"
    r"march|april|june|july|august|september|october|november|december|"
    r"mon|tue|wed|thu|fri|sat|sun|monday|tuesday|wednesday|thursday|friday|"
    r"saturday|sunday|today|tomorrow|tonight|weekend|week|month)\b",
    re.IGNORECASE)
_FACTS_NAME_RE = re.compile(
    r"\b(i'?m |i am |my name|this is |call me |name'?s )", re.IGNORECASE)
_FACTS_BOOK_RE = re.compile(
    r"\b(book|booking|reserve|charter|deposit|confirm|pay|payment|guests?|"
    r"pax|people|persons?|birthday|proposal|anniversary|wedding|corporate)\b",
    re.IGNORECASE)


def log(*a):
    print(time.strftime("%Y-%m-%dT%H:%M:%S"), *a, flush=True)


# --- Postgres (behavior_rules) ---------------------------------------------

def _psql(sql, timeout=12):
    """Run one SQL statement via docker exec; return (stdout, err_or_None)."""
    r = subprocess.run(
        ["docker", "exec", PG_CONTAINER, "psql", "-U", PG_USER, "-d", PG_DB,
         "-tA", "-c", sql],
        capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        return None, r.stderr.strip()[:200]
    return r.stdout, None


def _redis(args, timeout=8):
    """Run one redis-cli command via docker exec; return (stdout, err_or_None)."""
    r = subprocess.run(
        ["docker", "exec", REDIS_CONTAINER, "redis-cli"] + list(args),
        capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        return None, r.stderr.strip()[:200]
    return r.stdout, None


def _lit(v):
    """SQL string literal (or NULL) — escapes single quotes."""
    if v is None or v == "":
        return "NULL"
    return "'" + str(v).replace("'", "''") + "'"


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
    Returns (ok, err)."""
    if not isinstance(draft, dict):
        return False, "draft must be an object"
    did = (draft.get("id") or "").strip()
    cid = (draft.get("customer_phone") or "").strip()
    if not did or not cid:
        return False, "id + customer_phone required"
    ts = int(time.time() * 1000)
    _, err = _redis(["SET", _draft_key(did), json.dumps(draft),
                     "EX", str(QUEUE_TTL)])
    if err:
        return False, err
    status = (draft.get("status") or "pending")
    if status == "pending":
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
    Single-threaded Redis makes this atomic-enough for our load."""
    if not did:
        return None, "draft_id required"
    d, err = _draft_get(did)
    if err:
        return None, err
    if not d:
        return None, "draft not found"
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


def nomod_create_link(amount, summary, customer_name):
    """Create a Nomod payment link. Returns (link_url, link_id, err).

    Currency is hard-coded AED — never taken from the LLM. No expiry_date
    (Nomod default). The booking summary is the single line item + the note;
    customer_name goes in the link title."""
    if not NOMOD_API_KEY:
        return None, None, "NOMOD_API_KEY not configured on the bridge"
    summary = (summary or "Yacht charter").strip()
    title = ("Dubriani Yachts — " + customer_name).strip() \
        if customer_name else "Dubriani Yachts"
    body = json.dumps({
        "currency": "AED",
        "items": [{"name": summary[:200],
                   "amount": "%.2f" % amount, "quantity": 1}],
        "title": title[:50],
        "note": summary[:280],
    }).encode("utf-8")
    req = urllib.request.Request(
        NOMOD_API_BASE + "/links", data=body, method="POST",
        headers={"X-API-KEY": NOMOD_API_KEY,
                 "Content-Type": "application/json",
                 # Nomod's Cloudflare WAF 403s the default Python-urllib UA
                 "User-Agent": "DubrianiHermesBridge/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            resp = json.loads(r.read().decode("utf-8"))
        url = resp.get("url")
        if not url:
            return None, None, "Nomod response had no link url"
        return url, resp.get("id"), None
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode("utf-8"))
            msg = ((detail.get("error") or {}).get("message")
                   or detail.get("detail") or json.dumps(detail))
        except Exception:
            msg = "HTTP %s" % e.code
        return None, None, "Nomod %s: %s" % (e.code, msg)
    except Exception as e:
        return None, None, "Nomod request failed: %r" % e


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


def classify_feedback(text):
    """Hermes classifier call. Returns the parsed dict or None on failure."""
    if not text or not text.strip():
        return None
    try:
        rc, out, _, _ = run_hermes(FEEDBACK_CLASSIFIER_PROMPT + text.strip(),
                                   timeout=15)
    except Exception as e:
        log("feedback classify hermes error:", repr(e))
        return None
    if rc != 0:
        log("feedback classify rc=", rc)
        return None
    parsed, _ = extract_json(out)
    if not isinstance(parsed, dict):
        return None
    cls = (parsed.get("classification") or "").upper().strip()
    if cls not in ("CUSTOMER_NOTE", "GLOBAL_RULE", "SCENARIO_RULE"):
        return None
    return {
        "classification": cls,
        "customer_name": (parsed.get("customer_name") or "").strip(),
        "scenario": (parsed.get("scenario") or "").strip(),
        "text": (parsed.get("text") or "").strip(),
        "summary": (parsed.get("summary") or "").strip(),
    }


def _waha_get(path, timeout=12):
    """GET against WAHA REST API. Returns (parsed_json_or_None, err_str_or_None).
    Always non-raising."""
    if not (WAHA_API_KEY and WAHA_BASE):
        return None, "WAHA_API_KEY/WAHA_BASE not configured"
    try:
        req = urllib.request.Request(
            WAHA_BASE + path,
            headers={"X-Api-Key": WAHA_API_KEY})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8")), None
    except urllib.error.HTTPError as e:
        return None, f"WAHA {e.code}: {(e.read() or b'').decode('utf-8', 'replace')[:160]}"
    except Exception as e:
        return None, f"WAHA req failed: {e!r}"


def waha_fetch_history(customer_id, limit=30):
    """Pull last N messages from WAHA + pushName. Returns dict:
    {history: '...', last_message: '...', push_name: '...', count: N, err: None|str}."""
    msgs, err = _waha_get(
        f"/api/default/chats/{customer_id}/messages?limit={limit}&downloadMedia=false")
    if err:
        return {"history": "", "last_message": "", "push_name": "",
                "count": 0, "err": err}
    if not isinstance(msgs, list) or not msgs:
        return {"history": "First contact, no prior messages.",
                "last_message": "", "push_name": "", "count": 0, "err": None}
    # Get pushName from chat list
    chats, _ = _waha_get("/api/default/chats?limit=50")
    push_name = ""
    if isinstance(chats, list):
        for c in chats:
            sid = c.get("_serialized") or (c.get("id") or {}).get("_serialized")
            if sid == customer_id:
                pn = (c.get("name") or "").strip()
                # Skip pushNames that aren't useful display names
                if pn and not pn.startswith("+") and pn not in ("WhatsApp Business",
                                                                "Dubriani admin chat"):
                    push_name = pn
                break
    # Build history string (oldest first, max last 20 with body)
    msgs_sorted = sorted(msgs, key=lambda x: x.get("timestamp", 0))
    with_body = [m for m in msgs_sorted if (m.get("body") or "").strip()]
    if not with_body:
        return {"history": "First contact, no prior messages.",
                "last_message": "", "push_name": push_name,
                "count": 0, "err": None}
    now_ts = int(time.time())
    lines = []
    for m in with_body[-20:-1]:
        who = "Dubriani" if m.get("fromMe") else "Customer"
        secs = max(0, now_ts - (m.get("timestamp") or now_ts))
        ago = (f"{secs // 60}m" if secs < 5400 else
               f"{secs // 3600}h" if secs < 129600 else
               f"{secs // 86400}d")
        body = (m.get("body") or "").replace("\n", " ").strip()[:240]
        lines.append(f'{who} ({ago} ago): "{body}"')
    history = "\n".join(lines) if lines else "First contact, no prior messages."
    last = (with_body[-1].get("body") or "").strip()[:500]
    return {"history": history, "last_message": last,
            "push_name": push_name, "count": len(with_body), "err": None}


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

def _facts_extract_gate(incoming_message):
    """Heuristic: should this (non-first) message trigger a fresh extraction?
    True if it plausibly carries a new fact — a digit, a known yacht keyword,
    a date/time word, a self-introduction, or a booking keyword."""
    m = (incoming_message or "").lower()
    if not m:
        return False
    if any(ch.isdigit() for ch in m):
        return True
    if any(y in m for y in YACHT_NAMES):
        return True
    return bool(_FACTS_DATE_RE.search(m) or _FACTS_NAME_RE.search(m)
                or _FACTS_BOOK_RE.search(m))


def build_customer_header(facts):
    """facts: {name,dates,yachts,party_size,message_count} -> header string.
    Each optional line shows only when its fact is non-empty; the name line,
    the message-count line and the divider are always present."""
    f = facts or {}
    name = str(f.get("name") or "").strip()
    lines = ["\U0001F464 " + (name or "New contact")]
    if str(f.get("dates") or "").strip():
        lines.append("\U0001F4C5 Interested in: " + str(f["dates"]).strip())
    if str(f.get("yachts") or "").strip():
        lines.append("\U0001F6E5️ Looking at: " + str(f["yachts"]).strip())
    if str(f.get("party_size") or "").strip():
        lines.append("\U0001F465 Party size: " + str(f["party_size"]).strip())
    try:
        mc = int(f.get("message_count") or 0)
    except (TypeError, ValueError):
        mc = 0
    lines.append("\U0001F522 Message #" + str(mc) + " in conversation")
    lines.append("─" * 30)
    return "\n".join(lines)


def get_customer_facts(customer_id):
    """The customer_facts row as a dict, or None if absent / on error.
    Degrades to None so the header logic never breaks over a DB read."""
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
    Returns (new_message_count, None) or (None, error)."""
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


def _merge_facts(cached, extracted):
    """Merge rule: a non-empty extracted field overwrites; an empty extracted
    field keeps the cached value — a failed/partial extraction never erases a
    known fact."""
    cached = cached or {}
    out = {}
    for k in ("name", "dates", "yachts", "party_size"):
        ev = str((extracted or {}).get(k, "") or "").strip()
        out[k] = ev or str(cached.get(k) or "")
    return out


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


def run_hermes(query, timeout=None):
    if timeout is None:
        timeout = HERMES_TIMEOUT
    cmd = [HERMES, "--profile", "default", "chat", "-q", query, "-Q",
           "--source", "tool", "--yolo", "-t", "memory"]
    log("hermes call:", " ".join(c for c in cmd if c != query))
    env = dict(os.environ)
    env["PATH"] = os.path.dirname(HERMES) + os.pathsep + env.get("PATH", "")
    t0 = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          timeout=timeout, env=env)
    return proc.returncode, proc.stdout, proc.stderr, int((time.time() - t0) * 1000)


def extract_json(stdout):
    """Pull the outermost {...} JSON object out of Hermes stdout."""
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
    m = SESSION_RE.search("\n".join(streams))
    return m.group(1) if m else None


# ============================================================================
# Pipeline Review — labels, signals, confidence dampening, sameday interrupt
# (see docs/pipeline-review-plan.md §1, §2a–§2c)
# ============================================================================

LABELS = frozenset({
    "NEW", "WARM", "HOT", "NEEDS_ATTENTION", "COLD",
    "PAUSED_SPAM", "PAUSED_B2B", "PAUSED_PERSONAL",
})

# Signal regexes (compiled module-level).
MONEY_RE = re.compile(
    r"\b(AED|aed|price|budget|cost|how\s*much|cheap|expensive)\b"
    r"|\$\d|\b\d{4,}\b",
    re.IGNORECASE,
)
LETS_DO_IT_RE = re.compile(
    r"(let'?s\s+(do\s+it|book|lock)|i'?ll\s+take\s+it|"
    r"sounds\s+(good|great)[,\s]+book|book\s+it)",
    re.IGNORECASE,
)
SAME_DAY_RE = re.compile(
    r"\b(today|tonight|right\s*now|now|asap|immediately|this\s+(afternoon|evening|night))\b",
    re.IGNORECASE,
)
PRICING_INQUIRED_RE = re.compile(
    r"\b(price|cost|how\s*much|rate|rates|charge|fee)\b",
    re.IGNORECASE,
)
YACHT_KEYWORD_RE = re.compile(
    r"\b(yacht|boat|satoshi|pershing|sunseeker|thunder|catamaran|cruise|charter)\b",
    re.IGNORECASE,
)

# Confidence dampening — see plan §2a step 4.
CORRECTION_WINDOW_DAYS = 90
CORRECTION_DAMPENING_DIVISOR = 5.0
CONFIDENCE_FLOOR = 0.2
CONFIDENCE_DEMOTE_THRESHOLD = 0.4

# Sameday-interrupt cooldown — one ping per customer per 4 h.
SAMEDAY_INTERRUPT_TTL = int(os.environ.get("SAMEDAY_INTERRUPT_TTL", "14400"))

# Hourly sweep — process at most N customers per run; overflow next hour.
HOURLY_SWEEP_BATCH_LIMIT = int(os.environ.get("HOURLY_SWEEP_BATCH_LIMIT", "200"))
HOURLY_SWEEP_HERMES_CAP = int(os.environ.get("HOURLY_SWEEP_HERMES_CAP", "30"))

# Proactive follow-up engine — see docs/cowork-targeted-integration-plan.md
# (follow-up engine section). Default ON; operator can flip to disable.
FOLLOWUP_ENGINE_ENABLED = _envflag("FOLLOWUP_ENGINE_ENABLED", "true")
FOLLOWUP_BATCH_LIMIT = int(os.environ.get("FOLLOWUP_BATCH_LIMIT", "10"))

# /review report rendering caps (Telegram 4096-char limit safe).
REVIEW_CAP_HOT = int(os.environ.get("REVIEW_CAP_HOT", "10"))
REVIEW_CAP_NEEDS_ATTENTION = int(os.environ.get("REVIEW_CAP_NEEDS_ATTENTION", "10"))
REVIEW_CAP_WARM = int(os.environ.get("REVIEW_CAP_WARM", "8"))
REVIEW_CAP_COLD = int(os.environ.get("REVIEW_CAP_COLD", "5"))

# Tier demotion when confidence < CONFIDENCE_DEMOTE_THRESHOLD.
_TIER_BELOW = {"HOT": "WARM", "WARM": "NEW", "NEW": "NEW", "COLD": "COLD"}


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


def compute_label(latest_message, facts):
    """Match signal heuristics against latest message + cached facts.
    Returns (target_label, signal, evidence). NO DB writes. NO dampening
    (caller applies that)."""
    msg = latest_message or ""
    customer_id = (facts or {}).get("customer_id", "")
    mc = (facts or {}).get("message_count", 0) or 0
    yachts = ((facts or {}).get("yachts") or "").strip()
    dates = ((facts or {}).get("dates") or "").strip()

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
    """Read label state + a few denorm fields. Returns dict or None."""
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
    """UPDATE customer_facts.label + INSERT customer_label_history."""
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
    sameday-interrupt check reads — no DB row needed)."""
    cid = (customer_id or "").replace("'", "''")
    if event == "draft_posted":
        _redis(["SET", f"draft:posted:{customer_id}", "1", "EX", "86400"])
        return ("ok", None)
    if event == "customer_message":
        sql = (
            "INSERT INTO conversation_state "
            "(customer_id, last_customer_message_at, updated_at) "
            f"VALUES ('{cid}', now(), now()) "
            "ON CONFLICT (customer_id) DO UPDATE "
            "SET last_customer_message_at = now(), updated_at = now()"
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
            "(customer_id, last_nudge_drafted_at, updated_at) "
            f"VALUES ('{cid}', now(), now()) "
            "ON CONFLICT (customer_id) DO UPDATE "
            "SET last_nudge_drafted_at = now(), "
            "    reengage_attempts = conversation_state.reengage_attempts + "
            "      CASE WHEN conversation_state.last_customer_message_at IS NOT NULL "
            "            AND conversation_state.last_customer_message_at "
            "                < now() - interval '24 hours' "
            "           THEN 1 ELSE 0 END, "
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
        "(cs.last_nudge_drafted_at > cs.last_customer_message_at) AS already_drafted "
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
        if len(parts) < 8:
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
        # Skip-gates (Python-side; SQL already filtered the timing band)
        if locked:
            continue
        if mode == "autonomous":
            continue
        if already_drafted:
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


def score_lead(row, now_dt):
    """Compute priority score per docs/pipeline-review-plan.md §2e step 3.
    Pure function; deterministic. row is the dict shape from _read_lead_summary.
    Negative scores → PAUSED/snoozed tail."""
    score = 0
    label = row.get("label") or "NEW"
    if label == "NEEDS_ATTENTION":
        score += 1000
    elif label == "HOT":
        score += 800
    elif label == "WARM":
        score += 500
    elif label == "NEW":
        score += 300
    elif label == "COLD":
        # "almost-bought" cold = had booking intent but no payment
        if row.get("last_booking_intent_at"):
            score += 100
        kind = row.get("last_rejection_kind")
        lra = row.get("last_rejection_at_seconds")  # seconds since rejection
        if kind == "rejected_price" and lra is not None and lra > 30 * 86400:
            score += 50
        if kind == "rejected_timing":
            score += 50  # always surfaceable if cold + rejected_timing
    if label.startswith("PAUSED_"):
        score -= 10000
    if row.get("label_locked_active"):
        score -= 500

    # urgency boosts
    cmsg = row.get("last_customer_message_at_seconds")  # silent secs
    orep = row.get("last_operator_reply_at_seconds")
    we_owe = (cmsg is not None and orep is not None and cmsg < orep and cmsg < 99999999)
    # "we owe a reply" really means: customer msg is more recent than our reply
    # AND it's been >30min since they spoke.
    if (cmsg is not None and cmsg < 99999999  # not epoch-fallback
            and (orep is None or orep > cmsg)
            and cmsg > 30 * 60):
        score += 300
    if label == "HOT" and cmsg is not None and cmsg > 2 * 3600:
        score += 200
    dates = (row.get("dates") or "").lower()
    sameday = ("today" in dates or "tonight" in dates)
    if sameday and (orep is None or (cmsg is not None and (orep > cmsg))):
        score += 400
    plink = row.get("last_payment_link_at_seconds")
    ppromised = row.get("last_payment_promised_at_seconds")
    if (plink is not None and ppromised is None and plink > 24 * 3600):
        score += 150

    # damping — keep nudge damping (don't keep re-pushing the same nudge
    # for 24h after operator already drafted one), but DROP review-seen
    # damping (that suppressed the only WARM lead just because the operator
    # looked at the report a moment ago — the report should be consistent
    # across consecutive /review calls).
    nudge = row.get("last_nudge_drafted_at_seconds")
    if nudge is not None and nudge < 24 * 3600:
        score -= 100

    return score


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
        "COALESCE(recent_notes,'') "
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
        }
        rows.append(row)
    return rows


def _fmt_dur(secs):
    """Human-readable '9h', '24m', '3d' etc. None → '—'."""
    if secs is None:
        return "—"
    if secs < 90:
        return f"{int(secs)}s"
    if secs < 90 * 60:
        return f"{int(secs / 60)}m"
    if secs < 36 * 3600:
        return f"{int(secs / 3600)}h"
    return f"{int(secs / 86400)}d"


def render_review(scored, totals, mode="ondemand"):
    """Return a dict with both the single-message rendering (kept for backward
    compat) AND a per-lead-cards rendering so the workflow can post one message
    per lead — each lead's 3-button inline keyboard then sits with that lead's
    text, instead of stacking 6× at the bottom of one big report.

    Returns:
      {
        telegram_text: <full single-message render — backward-compat header>,
        inline_keyboards: <all rows stacked, backward-compat>,
        per_lead_messages: [
          {text: '...', inline_keyboard: [[{text:'Draft nudge',...}, ...]]},
          ...
        ],
        header_text: <just the summary header, no lead lines>,
        mark_seen_ids: [...],
      }
    """
    sections = {
        "HOT":             {"items": [], "cap": REVIEW_CAP_HOT,
                            "header": "🔥 HOT — ready to close",
                            "emoji": "🔥"},
        "NEEDS_ATTENTION": {"items": [], "cap": REVIEW_CAP_NEEDS_ATTENTION,
                            "header": "⚠️ NEEDS ATTENTION",
                            "emoji": "⚠️"},
        "WARM":            {"items": [], "cap": REVIEW_CAP_WARM,
                            "header": "♨️ WARM — worth nudging",
                            "emoji": "♨️"},
        "NEW":             {"items": [], "cap": 5,
                            "header": "🌱 NEW — early conversations",
                            "emoji": "🌱"},
        "COLD":            {"items": [], "cap": REVIEW_CAP_COLD,
                            "header": "❄️ COLD — re-engage candidates",
                            "emoji": "❄️"},
    }
    pause_tail = []
    seen_ids = []
    for score, row in scored:
        label = row.get("label") or "NEW"
        if label.startswith("PAUSED_") or score < 0:
            pause_tail.append(row)
            continue
        if label not in sections:
            continue
        sections[label]["items"].append((score, row))
        seen_ids.append(row["customer_id"])

    # Sort NEW section by last_customer_message_at DESC NULLS LAST — most
    # recently active customer first. Score-based tie-breaking was letting
    # genuinely new contacts fall into the overflow behind older NEW leads.
    # Other sections keep their score-based order (their boosts already
    # encode recency via the "we owe a reply > 30min" rule).
    def _recency_key(item):
        secs = item[1].get("last_customer_message_at_seconds")
        return (secs is None, secs if secs is not None else 0)
    sections["NEW"]["items"].sort(key=_recency_key)

    # ---- Single-message render (backward compat) ----------------------------
    when = ("Scheduled review" if mode == "scheduled"
            else "On-demand review")
    header_lines = [f"📋 *Pipeline Review* — {when}",
                    (f"{totals.get('total', 0)} active · "
                     f"{totals.get('HOT', 0)} hot · "
                     f"{totals.get('NEEDS_ATTENTION', 0)} need attention · "
                     f"{totals.get('COLD', 0)} cold")]
    lines = list(header_lines) + [""]
    keyboards = []
    per_lead_messages = []

    for label_key in ("HOT", "NEEDS_ATTENTION", "WARM", "NEW", "COLD"):
        sect = sections[label_key]
        items = sect["items"]
        if not items:
            continue
        cap = sect["cap"]
        shown = items[:cap]
        overflow = len(items) - len(shown)
        lines.append(f"*{sect['header']}* ({len(items)})")
        for i, (score, row) in enumerate(shown, 1):
            why = _why_line(row, label_key)
            lead_body = (
                f"*{row.get('name') or _name_fallback(row.get('customer_id'))}* — "
                f"{(row.get('yachts') or 'no yacht set')} · "
                f"{(row.get('dates') or 'no date')} · "
                f"msg #{row.get('message_count')}\n"
                f"⏱ silent {_fmt_dur(row.get('last_customer_message_at_seconds'))}"
                f"  ·  {why}"
            )
            lines.append(f"{i}. " + lead_body.replace("\n", "\n   "))
            sid = row["customer_id"]
            kb = [[
                {"text": "💬 Draft nudge", "callback_data": f"nudge:{sid}"},
                {"text": "💤 Snooze 4h",   "callback_data": f"snz:{sid}:4h"},
                {"text": "ℹ️ Info",        "callback_data": f"inf:{sid}"},
            ]]
            keyboards.append(kb[0])
            # Per-lead card: section emoji prefix + lead body, plus its own kb.
            per_lead_messages.append({
                "text": f"{sect['emoji']} *{label_key}*\n{lead_body}",
                "inline_keyboard": kb,
                "customer_id": sid,
            })
        if overflow > 0:
            lines.append(
                f"   _+{overflow} more — `/review {label_key.lower()}` to see all_"
            )
        lines.append("")

    if pause_tail:
        lines.append(f"⏸ Paused/snoozed: {len(pause_tail)} — `/info` to see.")

    telegram_text = "\n".join(lines).strip()
    header_text = "\n".join(header_lines).strip()
    return {
        "telegram_text": telegram_text,
        "inline_keyboards": keyboards,
        "header_text": header_text,
        "per_lead_messages": per_lead_messages,
        "mark_seen_ids": seen_ids,
    }


def _name_fallback(customer_id):
    """Build a recognisable label from customer_id when the customer's name
    is missing. Shows the last 4 digits prefixed with '…'. No country-code
    guessing — @lid IDs aren't real E.164 numbers, and even @c.us IDs have
    variable-length CCs that would mislabel."""
    cid = (customer_id or "").strip()
    digits = "".join(c for c in cid.split("@")[0] if c.isdigit())
    if len(digits) < 4:
        return "(unknown)"
    return "…" + digits[-4:]


def _why_line(row, label_key):
    """Heuristic one-liner. v1: deterministic. Hermes-driven 'why' is a
    follow-up enhancement."""
    notes = []
    if row.get("last_payment_link_at_seconds") is not None:
        plink_h = int(row["last_payment_link_at_seconds"] / 3600)
        if row.get("last_payment_promised_at_seconds") is None and plink_h > 24:
            notes.append(f"payment link sent {plink_h}h ago, no commitment")
    if row.get("last_booking_intent_at") and label_key == "COLD":
        notes.append("almost-bought (hit booking intent before going cold)")
    if (row.get("last_rejection_kind") == "rejected_price"
            and label_key == "COLD"):
        notes.append("lost on price — try different angle")
    if (row.get("last_rejection_kind") == "rejected_timing"
            and label_key == "COLD"):
        notes.append("lost on timing — date may be relevant now")
    if not notes:
        if label_key == "HOT":
            notes.append("hot signals — push toward booking")
        elif label_key == "NEEDS_ATTENTION":
            notes.append("we owe a reply")
        elif label_key == "WARM":
            notes.append("engaged — value-add nudge could move it")
        elif label_key == "COLD":
            notes.append("worth a soft re-engagement message")
        else:
            notes.append("new conversation")
    return " · ".join(notes)


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
                             "/draft-freshness"):
            self._send(404, {"error": "not found"})
            return
        if not TOKEN or self.headers.get("X-Bridge-Token") != TOKEN:
            self._send(401, {"error": "unauthorized"})
            return
        try:
            n = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(n) or b"{}")
        except Exception as e:
            self._send(400, {"error": f"bad request: {e}"})
            return
        if self.path == "/save-rule":
            self._save_rule(payload)
        elif self.path == "/improve":
            self._improve(payload)
        elif self.path == "/learn":
            self._learn(payload)
        elif self.path == "/rules":
            self._rules(payload)
        elif self.path == "/autosend-check":
            self._autosend_check(payload)
        elif self.path == "/set-mode":
            self._set_mode(payload)
        elif self.path == "/caps":
            self._send(200, {"ok": True, "text": caps_status_text()})
        elif self.path == "/autosend-state":
            self._autosend_state(payload)
        elif self.path == "/customer-facts":
            self._customer_facts(payload)
        elif self.path == "/debounce":
            self._debounce(payload)
        elif self.path == "/payment-link":
            self._payment_link(payload)
        elif self.path == "/feedback":
            self._feedback(payload)
        elif self.path == "/label-eval":
            self._label_eval(payload)
        elif self.path == "/conversation-state":
            self._conversation_state(payload)
        elif self.path == "/hourly-sweep":
            self._hourly_sweep(payload)
        elif self.path == "/review":
            self._review(payload)
        elif self.path == "/draft-followup":
            self._draft_followup(payload)
        elif self.path == "/info":
            self._info(payload)
        elif self.path == "/label":
            self._label(payload)
        elif self.path == "/snooze":
            self._snooze(payload)
        elif self.path == "/queue":
            self._queue(payload)
        elif self.path == "/followup-action":
            self._followup_action(payload)
        elif self.path == "/refresh-facts":
            self._refresh_facts(payload)
        elif self.path == "/draft-freshness":
            self._draft_freshness(payload)
        else:
            self._draft(payload)

    def _draft(self, payload):
        if not (payload.get("incoming_message") or "").strip():
            self._send(400, {"ok": False, "error": "incoming_message is required"})
            return
        query = build_query(payload)
        try:
            rc, out, err, elapsed = run_hermes(query)
        except subprocess.TimeoutExpired:
            log(f"hermes TIMEOUT after {HERMES_TIMEOUT}s")
            self._send(502, {"ok": False, "error": "hermes timeout"})
            return
        except Exception as e:
            log("hermes EXEC ERROR", repr(e))
            self._send(502, {"ok": False, "error": f"hermes exec error: {e}"})
            return

        parsed, blob = extract_json(out)
        sid = extract_session(out, err)
        messages = parsed.get("messages") if isinstance(parsed, dict) else None
        ok = (rc == 0 and isinstance(messages, list) and len(messages) > 0)
        if not ok:
            log(f"hermes FAIL rc={rc} parsed={parsed is not None} err={err[:300]!r}")
            self._send(502, {"ok": False, "error": "hermes produced no parseable draft",
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
        self._send(200, {
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

    def _improve(self, payload):
        if not payload.get("current_draft"):
            self._send(400, {"ok": False, "error": "current_draft is required"})
            return
        query = build_improve_query(payload)
        try:
            rc, out, err, elapsed = run_hermes(query)
        except subprocess.TimeoutExpired:
            log(f"improve TIMEOUT after {HERMES_TIMEOUT}s")
            self._send(502, {"ok": False, "error": "hermes timeout"})
            return
        except Exception as e:
            log("improve EXEC ERROR", repr(e))
            self._send(502, {"ok": False, "error": f"hermes exec error: {e}"})
            return
        parsed, blob = extract_json(out)
        sid = extract_session(out, err)
        messages = parsed.get("messages") if isinstance(parsed, dict) else None
        ok = (rc == 0 and isinstance(messages, list) and len(messages) > 0)
        if not ok:
            log(f"improve FAIL rc={rc} parsed={parsed is not None} err={err[:200]!r}")
            self._send(502, {"ok": False,
                             "error": "hermes produced no parseable result",
                             "rc": rc, "raw": (blob or out)[:2000]})
            return
        improved = bool(parsed.get("improved"))
        note = str(parsed.get("note") or "")
        log(f"improve OK customer={payload.get('customer_name')!r} "
            f"improved={improved} msgs={len(messages)} "
            f"elapsed={elapsed}ms session={sid}")
        self._send(200, {
            "ok": True,
            "improved": improved,
            "messages": messages,
            "note": note,
            "raw": blob,
            "session_id": sid,
            "elapsed_ms": elapsed,
        })

    def _learn(self, payload):
        fb = (payload.get("feedback") or "").strip()
        if not fb:
            self._send(400, {"ok": False, "error": "feedback is required"})
            return
        query = build_learn_query(payload)
        try:
            rc, out, err, elapsed = run_hermes(query)
        except subprocess.TimeoutExpired:
            self._send(502, {"ok": False, "error": "hermes timeout"})
            return
        except Exception as e:
            self._send(502, {"ok": False, "error": f"hermes exec error: {e}"})
            return
        parsed, blob = extract_json(out)
        if not isinstance(parsed, dict):
            log(f"learn FAIL rc={rc} (no parseable JSON)")
            self._send(502, {"ok": False, "error": "hermes produced no result",
                             "rc": rc, "raw": (blob or out)[:1000]})
            return
        if not parsed.get("is_rule"):
            log(f"learn: feedback judged one-off elapsed={elapsed}ms")
            self._send(200, {"ok": True, "captured": False,
                             "reason": "feedback judged a one-off tweak"})
            return
        rule_text = (parsed.get("rule_text") or "").strip()
        if not rule_text:
            self._send(200, {"ok": True, "captured": False,
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
            self._send(502, {"ok": False, "error": "rule insert failed: " + str(e2)})
            return
        log(f"learn: rule captured id={rid} scope={scope} text={rule_text[:70]!r}")
        self._send(200, {"ok": True, "captured": True, "rule_id": rid,
                         "rule_text": rule_text, "scope": scope,
                         "reasoning": parsed.get("reasoning", ""),
                         "elapsed_ms": elapsed})

    def _rules(self, payload):
        action = (payload.get("action") or "list").strip().lower()
        if action == "list":
            rows, err = list_pending_rules()
            if rows is None:
                self._send(502, {"ok": False, "error": str(err)})
                return
            self._send(200, {"ok": True, "pending": rows, "count": len(rows)})
            return
        if action in ("activate", "discard"):
            fn = activate_rule if action == "activate" else discard_rule
            ok, err = fn(payload.get("rule_id"))
            if not ok:
                code = 400 if "integer" in str(err) else 404
                self._send(code, {"ok": False, "error": str(err)})
                return
            log(f"rule {action}d: id={payload.get('rule_id')}")
            self._send(200, {"ok": True, "action": action,
                             "rule_id": payload.get("rule_id")})
            return
        self._send(400, {"ok": False, "error": "action must be list|activate|discard"})

    def _save_rule(self, payload):
        text = (payload.get("rule_text") or "").strip()
        if not text:
            self._send(400, {"ok": False, "error": "rule_text is required"})
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
            self._send(502, {"ok": False, "error": "insert failed: " + str(err)})
            return
        log(f"rule saved id={rid} scope={scope} text={text[:70]!r}")
        self._send(200, {"ok": True, "rule_id": rid, "scope": scope})

    def _set_mode(self, payload):
        cid = (payload.get("customer_id") or "").strip()
        mode = (payload.get("mode") or "").strip().lower()
        by = (payload.get("activated_by") or "operator").strip()
        break_reason = (payload.get("break_reason") or "").strip() or None
        if not cid:
            self._send(400, {"ok": False, "error": "customer_id is required"})
            return
        if cid == "__ALL__":  # /manual kill switch
            ok, err = manual_killswitch()
            if not ok:
                log("manual killswitch failed:", err)
                self._send(502, {"ok": False, "error": str(err)})
                return
            log("MANUAL KILL SWITCH — all conversations -> approval")
            self._send(200, {"ok": True, "message": "all conversations set to approval"})
            return
        m, err = set_mode(cid, mode, by, break_reason)
        if m is None:
            log("set-mode failed:", err)
            self._send(502, {"ok": False, "error": str(err)})
            return
        log(f"conversation mode set: {cid} -> {m} (by {by})")
        self._send(200, {"ok": True, "customer_id": cid, "mode": m})

    def _autosend_check(self, payload):
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
        cid = (payload.get("customer_id") or "").strip()
        if not cid:
            self._send(400, {"ok": False, "error": "customer_id is required"})
            return
        commit = bool(payload.get("commit"))
        mode = get_mode(cid)
        if mode != "autonomous":
            self._send(200, {"ok": True, "mode": mode, "auto_send": False,
                             "reason": "conversation is not in autonomous mode"})
            return
        if not commit:
            # Gate probe — mode only. The workflow (Auto Is Autonomous) reads
            # just `mode` from this response; caps are the commit call's job.
            log(f"autosend-check GATE customer={cid} mode=autonomous "
                "(caps deferred to commit)")
            self._send(200, {"ok": True, "mode": mode, "auto_send": True,
                             "reason": "autonomous — caps evaluated at commit"})
            return
        ok, reason = evaluate_caps(cid)
        if ok:
            log_autosend(cid, "auto")
        log(f"autosend-check COMMIT customer={cid} mode=autonomous "
            f"auto_send={ok} reason={reason!r}")
        self._send(200, {"ok": True, "mode": mode, "auto_send": ok,
                         "reason": reason})

    def _autosend_state(self, payload):
        """BUG-1 fix: Redis-backed autonomous-send state. action = arm |
        disarm | get, keyed autosend:<draft_id>. The FR-4 autonomous branch
        uses this instead of n8n staticData, which is unreliable across the
        Auto Wait countdown."""
        action = (payload.get("action") or "").strip().lower()
        did = (payload.get("draft_id") or "").strip()
        if not did:
            self._send(400, {"ok": False, "error": "draft_id is required"})
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
                self._send(502, {"ok": False, "error": err})
                return
            log(f"autosend-state ARM {did}")
            self._send(200, {"ok": True, "action": "arm", "draft_id": did})
        elif action == "disarm":
            _redis(["DEL", key])  # DEL of a missing key is a harmless no-op
            log(f"autosend-state DISARM {did}")
            self._send(200, {"ok": True, "action": "disarm", "draft_id": did})
        elif action == "get":
            out, err = _redis(["GET", key])
            if err:
                log("autosend-state get failed:", err)
                self._send(502, {"ok": False, "error": err})
                return
            raw = (out or "").strip()
            data = None
            if raw:
                try:
                    data = json.loads(raw)
                except Exception:
                    data = None
            self._send(200, {"ok": True, "action": "get", "draft_id": did,
                             "armed": bool(data), "data": data})
        elif action == "update":
            # FR-5 BUG-3 fix: the improver rewrites the draft text on an armed
            # autonomous draft so the auto-send dispatches the improved copy,
            # not the arm-time snapshot. No-op (updated=false) if not armed —
            # an approval-mode draft has no key, so calling this is harmless.
            out, err = _redis(["GET", key])
            if err:
                log("autosend-state update GET failed:", err)
                self._send(502, {"ok": False, "error": err})
                return
            raw = (out or "").strip()
            if not raw:
                self._send(200, {"ok": True, "action": "update",
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
            self._send(200, {"ok": True, "action": "update",
                             "draft_id": did, "updated": True})
        else:
            self._send(400, {"ok": False,
                             "error": "action must be arm|disarm|get|update"})

    def _customer_facts(self, payload):
        """feature-header: maintain customer_facts + return a context header.
        Fail-safe — ANY error degrades to cached facts and still returns 200,
        so the workflow's draft card is never blocked by header logic."""
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
                self._send(200, {"ok": True, "extracted": False,
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
            new_count, err = upsert_customer_facts(cid, merged["name"], merged)
            if err:
                log("customer_facts upsert failed:", err)
            mc = new_count if isinstance(new_count, int) \
                else ((cached or {}).get("message_count", 0) + 1)
            hdr = build_customer_header({**merged, "message_count": mc})
            log(f"customer-facts cid={cid!r} extract={do_extract} msg#{mc}")
            self._send(200, {"ok": True, "extracted": bool(do_extract),
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
            self._send(200, {"ok": True, "extracted": False, "degraded": True,
                             "message_count": mc, "customer_header": hdr})

    def _debounce(self, payload):
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
        action = (payload.get("action") or "").strip().lower()
        phone = (payload.get("phone") or "").strip()
        if not phone:
            self._send(400, {"ok": False, "error": "phone is required"})
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
            self._send(200, {"ok": True, "phone": phone, "token": token})
            return
        if action == "flush":
            token = (payload.get("token") or "").strip()
            cur, err = _redis(["GET", seqkey])
            if err:
                log("debounce flush GET failed (fail-open):", err)
                self._send(200, {"ok": True, "latest": True, "degraded": True,
                                 "combined": "", "ids": [], "count": 0})
                return
            if (cur or "").strip() != token:
                # a newer message holds the token (or the window lapsed) —
                # this execution must not produce a draft
                self._send(200, {"ok": True, "latest": False})
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
            self._send(200, {"ok": True, "latest": True,
                             "combined": "\n".join(texts),
                             "ids": ids, "count": len(msgs)})
            return
        self._send(400, {"ok": False, "error": "action must be buffer|flush"})

    def _payment_link(self, payload):
        """Nomod minimal build: create a payment link for a confirmed booking.
        Fail-safe — ALWAYS returns 200 so the workflow's draft is never blocked;
        an ok:false response just means the card posts without a link."""
        cid = (payload.get("customer_id") or "").strip()
        cname = (payload.get("customer_name") or "").strip()
        summary = (payload.get("payment_summary") or "").strip()
        if not PAYMENTS_ENABLED:
            log("payment-link refused — PAYMENTS_ENABLED is off")
            self._send(200, {"ok": False, "error": "payments are disabled"})
            return
        try:
            amount = float(payload.get("amount"))
        except (TypeError, ValueError):
            amount = 0.0
        # basic input validation only — NOT a business cap (per plan)
        if amount <= 0:
            log("payment-link refused — invalid amount %r"
                % payload.get("amount"))
            self._send(200, {"ok": False,
                             "error": "invalid amount: %r"
                                      % payload.get("amount")})
            return
        url, lid, err = nomod_create_link(amount, summary, cname)
        if err:
            log("payment-link FAILED customer=%s amount=AED%.2f err=%s"
                % (cid, amount, err))
            self._send(200, {"ok": False, "error": err})
            return
        log("payment-link OK customer=%s amount=AED%.2f link_id=%s"
            % (cid, amount, lid))
        self._send(200, {"ok": True, "link_url": url, "link_id": lid,
                         "amount": amount})

    def _feedback(self, payload):
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
        action = (payload.get("action") or "").strip().lower()
        if action == "classify":
            text = (payload.get("text") or "").strip()
            if not text:
                self._send(200, {"ok": False, "error": "text is required"})
                return
            result = classify_feedback(text)
            if not result:
                log("feedback classify failed for:", text[:80])
                self._send(200, {"ok": False,
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
                self._send(200, {"ok": False, "error": "redis: " + err})
                return
            log("feedback classify -> %s proposal=%s"
                % (result["classification"], pid))
            self._send(200, {"ok": True, "proposal_id": pid, **result})
            return
        if action == "save":
            pid = (payload.get("proposal_id") or "").strip()
            if not pid:
                self._send(400, {"ok": False, "error": "proposal_id required"})
                return
            out, err = _redis(["GET", "feedback:" + pid])
            if err:
                self._send(502, {"ok": False, "error": "redis: " + err})
                return
            raw = (out or "").strip()
            if not raw:
                self._send(200, {"ok": False,
                                 "error": "proposal not found or expired"})
                return
            try:
                p = json.loads(raw)
            except Exception:
                self._send(200, {"ok": False, "error": "bad proposal data"})
                return
            cls = p.get("classification")
            if cls == "CUSTOMER_NOTE":
                cid = (p.get("customer_id") or "").strip()
                if not cid:
                    self._send(200, {"ok": False,
                                     "error": "no customer_id resolved",
                                     "matches": p.get("matches", [])})
                    return
                note = (p.get("text") or "").strip()
                sql = ("INSERT INTO customer_notes (customer_id, note_text) "
                       "VALUES (" + _lit(cid) + ", " + _lit(note)
                       + ") RETURNING id")
                rid_out, err2 = _psql(sql)
                if err2:
                    self._send(502, {"ok": False, "error": "db: " + err2})
                    return
                rid = ((rid_out or "").strip().splitlines() or [""])[0]
                feedback_apply_cap("customer_notes",
                                   FEEDBACK_MAX_PER_CUSTOMER,
                                   customer_id=cid)
                _redis(["DEL", "feedback:" + pid])
                log("feedback SAVED CUSTOMER_NOTE id=%s cid=%s" % (rid, cid))
                self._send(200, {"ok": True, "table": "customer_notes",
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
                    self._send(502, {"ok": False, "error": "db: " + err2})
                    return
                rid = ((rid_out or "").strip().splitlines() or [""])[0]
                feedback_apply_cap("behavior_rules", FEEDBACK_MAX_GLOBAL,
                                   scope="global")
                _redis(["DEL", "feedback:" + pid])
                log("feedback SAVED GLOBAL_RULE id=%s" % rid)
                self._send(200, {"ok": True, "table": "behavior_rules",
                                 "row_id": rid, "classification": cls,
                                 "summary": p.get("summary", "")})
                return
            if cls == "SCENARIO_RULE":
                scenario = (p.get("scenario") or "").strip()
                if not scenario:
                    self._send(200, {"ok": False,
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
                    self._send(502, {"ok": False, "error": "db: " + err2})
                    return
                rid = ((rid_out or "").strip().splitlines() or [""])[0]
                feedback_apply_cap("behavior_rules", FEEDBACK_MAX_SCENARIO,
                                   scope="scenario", scope_value=scenario)
                _redis(["DEL", "feedback:" + pid])
                log("feedback SAVED SCENARIO_RULE id=%s scenario=%s"
                    % (rid, scenario))
                self._send(200, {"ok": True, "table": "behavior_rules",
                                 "row_id": rid, "scenario": scenario,
                                 "classification": cls,
                                 "summary": p.get("summary", "")})
                return
            self._send(200, {"ok": False,
                             "error": "unknown classification: " + str(cls)})
            return
        if action == "discard":
            pid = (payload.get("proposal_id") or "").strip()
            if not pid:
                self._send(400, {"ok": False, "error": "proposal_id required"})
                return
            _redis(["DEL", "feedback:" + pid])
            log("feedback discarded %s" % pid)
            self._send(200, {"ok": True})
            return
        if action == "behavioral-context":
            cid = (payload.get("customer_id") or "").strip()
            ctx = behavioral_context(cid)
            self._send(200, {"ok": True, **ctx})
            return
        self._send(400, {"ok": False,
                         "error": "action must be "
                                  "classify|save|discard|behavioral-context"})

    # --- Pipeline Review handlers ------------------------------------------
    # See docs/pipeline-review-plan.md §2a, §2b. Both fail-open (always 200).

    def _update_last_analysis(self, customer_id, signal, confidence):
        """UPSERT conversation_state.last_analysis_{signal,confidence}.
        Idempotent. Called by _label_eval on every evaluation regardless of
        whether the label actually changed — keeps the analysis bookkeeping
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

    def _label_eval(self, payload):
        """POST /label-eval — silent per-message label updater + sameday
        interrupt detection. Always returns 200; fail-open."""
        cid = (payload.get("customer_id") or "").strip()
        msg = (payload.get("latest_message") or "").strip()
        skip_if_locked = bool(payload.get("skip_if_locked", True))
        if not cid:
            self._send(200, {"ok": False, "error": "customer_id required"})
            return
        try:
            row = get_current_label_row(cid)
            if row is None:
                # No customer_facts row yet — surface NEW without writing.
                # customer_facts upsert happens in _customer_facts on this
                # same message; we don't race it here.
                self._send(200, {
                    "ok": True, "customer_id": cid,
                    "label": "NEW", "previous_label": None,
                    "changed": False, "signal": "no_facts_row",
                    "confidence": 1.0, "evidence": "",
                    "interrupt_required": False, "alert_text": None,
                })
                return
            previous_label = row.get("label")

            # If locked (PAUSED via /label or /snooze), short-circuit.
            if skip_if_locked and row.get("label_locked_until"):
                cid_esc = cid.replace("'", "''")
                lock_out, _err = _psql(
                    "SELECT label_locked_until > now() FROM customer_facts "
                    f"WHERE customer_id = '{cid_esc}'"
                )
                if (lock_out or "").strip().startswith("t"):
                    self._send(200, {
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
            self._update_last_analysis(cid, sig, confidence)

            log(f"label-eval cid={cid!r} {previous_label}→{applied} "
                f"signal={sig} conf={confidence:.2f} changed={changed} "
                f"interrupt={interrupt_required}")
            self._send(200, {
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
            self._send(200, {"ok": False, "degraded": True, "error": str(e),
                             "label": None, "interrupt_required": False,
                             "alert_text": None})

    def _hourly_sweep(self, payload):
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
                self._send(200, {"ok": False, "degraded": True,
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
                        self._update_last_analysis(cid, "no_facts_row", 1.0)
                        continue
                    prev = row.get("label")
                    # Honor manual lock window.
                    cid_esc = cid.replace("'", "''")
                    lock_out, _err = _psql(
                        "SELECT label_locked_until > now() FROM customer_facts "
                        f"WHERE customer_id = '{cid_esc}'"
                    )
                    is_locked = (lock_out or "").strip().startswith("t")
                    if is_locked:
                        self._update_last_analysis(cid, "locked", 1.0)
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
                    if applied != prev:
                        _o, err = apply_label_transition(
                            cid, prev, applied,
                            f"hourly_sweep:{sig}", ev,
                            row.get("message_count", 0))
                        if err:
                            log(f"hourly_sweep transition err cid={cid!r}:", err)
                        else:
                            transitions += 1
                    self._update_last_analysis(cid, sig, confidence)
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
            self._send(200, {
                "ok": True, "scanned": scanned, "transitions": transitions,
                "hermes_calls": 0,  # v1: no Hermes-driven disambiguation
                "skipped_unchanged": skipped, "elapsed_ms": elapsed_ms,
                "eligible_followups": eligible_followups,
            })
        except Exception as e:
            log("hourly_sweep ERROR:", repr(e))
            self._send(200, {"ok": False, "degraded": True, "error": str(e),
                             "scanned": 0, "transitions": 0,
                             "hermes_calls": 0, "skipped_unchanged": 0,
                             "eligible_followups": [],
                             "elapsed_ms": int((_time.time() - t0) * 1000)})

    def _draft_freshness(self, payload):
        """POST /draft-freshness — check if a follow-up draft has gone stale
        (customer replied after the draft was generated). Returns
        {ok, stale: bool, is_followup, draft_ts, last_msg_ts, customer_id,
         draft_id, last_msg_preview, customer_name}. Fail-safe: returns
        stale=false on any error so Send chain isn't blocked by bridge issues."""
        did = (payload.get("draft_id") or "").strip()
        if not did:
            self._send(200, {"ok": False, "stale": False,
                             "error": "draft_id required"})
            return
        try:
            d, err = _draft_get(did)
            if err or not d:
                self._send(200, {"ok": False, "stale": False,
                                 "error": err or "draft not found"})
                return
            # Only enforce freshness for follow-up drafts. Normal customer-
            # message drafts were already generated FROM the latest history.
            if not d.get("is_followup"):
                self._send(200, {"ok": True, "stale": False,
                                 "is_followup": False, "draft_id": did})
                return
            cid = d.get("customer_phone", "")
            draft_ts = d.get("timestamp", "")
            cid_e = (cid or "").replace("'", "''")
            # Get customer's last_customer_message_at as epoch seconds
            out, _err = _psql(
                "SELECT EXTRACT(EPOCH FROM last_customer_message_at), "
                "to_char(last_customer_message_at, 'YYYY-MM-DD HH24:MI:SS') "
                f"FROM conversation_state WHERE customer_id = '{cid_e}'"
            )
            last_msg_epoch = 0.0
            last_msg_str = ""
            for line in (out or "").strip().splitlines():
                parts = line.split("|")
                if len(parts) >= 2:
                    try:
                        last_msg_epoch = float(parts[0].strip())
                    except ValueError:
                        last_msg_epoch = 0.0
                    last_msg_str = parts[1].strip()
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
            stale = (last_msg_epoch > 0 and draft_epoch > 0
                     and last_msg_epoch > draft_epoch)
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
            self._send(200, {
                "ok": True, "stale": stale, "is_followup": True,
                "draft_id": did, "customer_id": cid,
                "customer_name": cust_name,
                "draft_ts": draft_ts, "last_msg_ts": last_msg_str,
                "last_msg_preview": last_preview,
                "lag_seconds": int(last_msg_epoch - draft_epoch)
                               if (last_msg_epoch and draft_epoch) else 0,
            })
        except Exception as e:
            log("draft_freshness EXC:", repr(e))
            self._send(200, {"ok": True, "stale": False, "error": str(e)})

    def _refresh_facts(self, payload):
        """POST /refresh-facts — pull a customer's WAHA history and re-run
        Hermes extraction over the FULL conversation (not just one message).
        Body: {customer_id|name}. Returns the refreshed customer_facts row +
        the WAHA history summary. Operator-on-demand command."""
        cid = (payload.get("customer_id") or "").strip()
        name_query = (payload.get("name") or "").strip()
        if not cid and name_query:
            resolved, _ = resolve_customer_by_name(name_query)
            if resolved:
                cid = resolved
        if not cid:
            self._send(200, {"ok": False, "error": "customer_id or name required",
                             "telegram_text": "⚠️ Customer not found."})
            return
        try:
            waha = waha_fetch_history(cid, limit=30)
            if waha.get("err"):
                self._send(200, {"ok": False, "error": waha["err"],
                                 "telegram_text": f"⚠️ WAHA fetch failed: {waha['err']}"})
                return
            if waha["count"] == 0:
                self._send(200, {"ok": False,
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
            self._send(200, {
                "ok": True, "customer_id": cid,
                "facts": merged, "label": row.get("label"),
                "waha_count": waha["count"], "telegram_text": text,
            })
        except Exception as e:
            log("refresh_facts ERROR:", repr(e))
            self._send(200, {"ok": False, "degraded": True, "error": str(e),
                             "telegram_text": f"⚠️ Refresh failed: {e}"})

    def _followup_action(self, payload):
        """POST /followup-action — log a proactive-follow-up event to
        autonomous_sends.notes. Body: {customer_id, action, label?,
        silence_window?, silence_hours?, draft_text?}.
        action ∈ {drafted, sent, skipped, edited}.
        Fail-safe: always returns 200; logs but never raises."""
        cid = (payload.get("customer_id") or "").strip()
        action = (payload.get("action") or "").strip().lower()
        if not cid or action not in ("drafted", "sent", "skipped", "edited"):
            self._send(200, {"ok": False,
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
                self._send(200, {"ok": False, "degraded": True,
                                 "error": err[:200]})
                return
            log(f"followup-action cid={cid!r} action={action} "
                f"window={notes.get('silence_window')!r}")
            self._send(200, {"ok": True, "customer_id": cid, "action": action})
        except Exception as e:
            log("followup_action EXC:", repr(e))
            self._send(200, {"ok": False, "degraded": True, "error": str(e)})

    def _review(self, payload):
        """POST /review — read v_lead_summary, score, render Telegram report
        + inline keyboards. Always returns 200; fail-open."""
        mode = (payload.get("mode") or "ondemand").strip()
        filter_label = (payload.get("filter") or "all").strip().lower()
        try:
            rows = read_lead_summary(
                filter_label if filter_label in ("hot", "warm", "cold") else None)
            scored = sorted(((score_lead(r, None), r) for r in rows),
                            key=lambda t: t[0], reverse=True)
            totals = {"total": len(rows), "HOT": 0, "WARM": 0, "COLD": 0,
                      "NEW": 0, "NEEDS_ATTENTION": 0, "PAUSED": 0}
            for r in rows:
                lab = r.get("label") or "NEW"
                if lab.startswith("PAUSED_"):
                    totals["PAUSED"] += 1
                elif lab in totals:
                    totals[lab] += 1
            rendered = render_review(scored, totals, mode=mode)
            # Mark seen so the damping picks them up on the next /review.
            mark_review_seen(rendered.get("mark_seen_ids") or [])
            self._send(200, {
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
            self._send(200, {"ok": False, "degraded": True, "error": str(e),
                             "telegram_text": "⚠️ Review failed — bridge error.",
                             "inline_keyboards": []})

    def _draft_followup(self, payload):
        """POST /draft-followup — generate a follow-up draft via Hermes.
        Body: {customer_id, history?, customer_name?, silence_window?, silence_hours?}.
        Reuses build_query scaffold but injects a label-specific directive.

        If caller didn't supply history (proactive sweep + [Draft nudge]
        button both omit it), pull the last 10 messages from WAHA so
        Hermes always sees the live conversation context BEFORE drafting.
        Without this, follow-up drafts ignore in-progress negotiations
        and write off-context generic upsells."""
        cid = (payload.get("customer_id") or "").strip()
        history = payload.get("history") or ""
        if not cid:
            self._send(200, {"ok": False, "error": "customer_id required"})
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
                # phrasing for this exact window. Suppress upsells.
                phrase = GHOST_RECOVERY_PHRASES[silence_window]
                shrs = (f"{silence_hours:.1f}"
                        if isinstance(silence_hours, (int, float)) else "a while")
                directive = (
                    f"This is a PROACTIVE GHOST-RECOVERY follow-up — NOT a "
                    f"fresh sale. The customer has been silent for {shrs} "
                    f"hours (window: {silence_window}). Use the verified "
                    f"ghost-recovery phrasing for THIS window from your "
                    f"system prompt — specifically use: \"{phrase}\" (you may "
                    f"light-touch personalize but keep the phrase intact). "
                    f"DO NOT pitch add-ons, upsells, perks, or new options. "
                    f"DO NOT apologize for the silence. Send ONE short "
                    f"message, max 1 sentence."
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
                self._send(200, {"ok": False, "degraded": True,
                                 "error": f"hermes rc={rc}",
                                 "draft_text": "", "label": label})
                return
            # extract_json returns (parsed_dict, raw_blob_str) — unpack both.
            parsed, _blob = extract_json(out)
            draft_text = ""
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
                    draft_text = "\n".join(p for p in parts if p).strip()
                if not draft_text:
                    draft_text = (parsed.get("text") or "").strip()
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
            self._send(200, {
                "ok": True, "customer_id": cid, "label": label,
                "draft_text": draft_text,
                "approval_card_header": (
                    f"🔔 PROACTIVE FOLLOW-UP — {label.lower()}"),
                "session_id": extract_session(out, err),
            })
        except Exception as e:
            log("draft_followup ERROR:", repr(e))
            self._send(200, {"ok": False, "degraded": True, "error": str(e),
                             "draft_text": "", "label": "WARM"})

    def _resolve_target(self, payload):
        """Return (customer_id, error_text). On success: ('cid…@lid', None).
        On any miss: ('', '⚠️ ...'). Handles either:
          - {customer_id: '…@lid'}   — direct
          - {name: 'Mark'}           — name lookup via resolve_customer_by_name
        Note: resolve_customer_by_name returns (cid_or_None, matches[]) — we
        unpack the tuple correctly here so callers don't have to."""
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

    def _info(self, payload):
        """POST /info — operator-readable single-lead summary. Returns
        markdown text shaped for human reading (no raw signal names, no
        internal IDs unless useful). Fail-open, always 200."""
        cid, err = self._resolve_target(payload)
        if not cid:
            self._send(200, {"ok": False, "error": err,
                             "telegram_text": err})
            return
        try:
            row = get_current_label_row(cid)
            if row is None:
                self._send(200, {
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
            }.get(label, "•")
            name = row.get("name") or "Unknown"

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
            if row.get("party_size"):
                facts_bits.append(f"Party: *{row['party_size']}*")
            if facts_bits:
                lines.append("   " + " · ".join(facts_bits))
            lines.append(f"   {row.get('message_count', 0)} message(s) in this conversation")

            # silence + last reply
            if silent_secs is not None:
                lines.append(
                    f"   ⏱ Last customer message: {_fmt_dur(silent_secs)} ago "
                    f"({last_cust_dt})")
            if last_op_dt:
                lines.append(f"   ↳ Last operator reply: {last_op_dt}")

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

            self._send(200, {
                "ok": True, "customer_id": cid,
                "telegram_text": "\n".join(lines),
            })
        except Exception as e:
            log("info ERROR:", repr(e))
            self._send(200, {"ok": False, "degraded": True, "error": str(e),
                             "telegram_text": "⚠️ Info failed — bridge error."})

    def _label(self, payload):
        """POST /label — manual label override + record into label_corrections
        for self-improvement dampening."""
        new_label = (payload.get("label") or "").strip().upper()
        reason = (payload.get("reason") or "").strip()
        if new_label not in LABELS:
            self._send(200, {"ok": False,
                             "error": f"label must be one of {sorted(LABELS)}",
                             "telegram_text": f"⚠️ Bad label: {new_label!r}.\n"
                             "Allowed: NEW, WARM, HOT, NEEDS_ATTENTION, COLD, "
                             "PAUSED_SPAM, PAUSED_B2B, PAUSED_PERSONAL"})
            return
        cid, err = self._resolve_target(payload)
        if not cid:
            self._send(200, {"ok": False, "error": err,
                             "telegram_text": err})
            return
        try:
            row = get_current_label_row(cid)
            if row is None:
                self._send(200, {"ok": False,
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
            self._send(200, {
                "ok": True, "customer_id": cid,
                "previous_label": prev,
                "label": new_label,
                "telegram_text": f"✅ {name} → *{new_label}* (manual)",
            })
        except Exception as e:
            log("label ERROR:", repr(e))
            self._send(200, {"ok": False, "degraded": True, "error": str(e),
                             "telegram_text": "⚠️ Label update failed."})

    def _snooze(self, payload):
        """POST /snooze — set label_locked_until for a customer; don't change
        the label. Duration like '4h', '2d', '30m'."""
        duration = (payload.get("duration") or "").strip().lower()
        m = re.match(r"^(\d+)([mhd])$", duration)
        if not m:
            self._send(200, {"ok": False,
                             "error": "duration must match \\d+[mhd] (e.g. 4h, 2d, 30m)",
                             "telegram_text": f"⚠️ Bad duration: {duration!r}.  "
                             "Use 4h / 2d / 30m."})
            return
        cid, err = self._resolve_target(payload)
        if not cid:
            self._send(200, {"ok": False, "error": err,
                             "telegram_text": err})
            return
        try:
            n_val = int(m.group(1))
            unit = m.group(2)
            interval = {"m": "minutes", "h": "hours", "d": "days"}[unit]
            cid_e = cid.replace("'", "''")
            row = get_current_label_row(cid)
            if row is None:
                self._send(200, {"ok": False,
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
            self._send(200, {
                "ok": True, "customer_id": cid,
                "duration": duration,
                "telegram_text": f"💤 {name} snoozed for {duration}.",
            })
        except Exception as e:
            log("snooze ERROR:", repr(e))
            self._send(200, {"ok": False, "degraded": True, "error": str(e),
                             "telegram_text": "⚠️ Snooze failed."})

    def _queue(self, payload):
        """POST /queue — Redis-backed pendingQueue.
        See docs/pendingqueue-redis-migration-plan.md §3.
        Actions: save | get | update | mark | latest-for-customer | drop.
        Always 200; ok flag carries the outcome."""
        action = (payload.get("action") or "").strip().lower()

        if action == "save":
            draft = payload.get("draft") or {}
            ok, err = _draft_save(draft)
            self._send(200, {"ok": ok, "error": err,
                             "draft_id": (draft.get("id") if isinstance(draft, dict) else None)})
            return

        if action == "get":
            did = (payload.get("draft_id") or "").strip()
            if not did:
                self._send(200, {"ok": False, "error": "draft_id required",
                                 "draft": None, "found": False})
                return
            d, err = _draft_get(did)
            self._send(200, {"ok": True, "draft": d,
                             "found": d is not None, "error": err})
            return

        if action == "update":
            did = (payload.get("draft_id") or "").strip()
            d, err = _draft_update(did, payload.get("fields") or {})
            self._send(200, {"ok": d is not None, "draft": d, "error": err})
            return

        if action == "mark":
            did = (payload.get("draft_id") or "").strip()
            status = (payload.get("status") or "").strip()
            if not status:
                self._send(200, {"ok": False, "error": "status required",
                                 "draft": None})
                return
            d, err = _draft_update(did, {"status": status})
            self._send(200, {"ok": d is not None, "draft": d, "error": err})
            return

        if action == "latest-for-customer":
            cid = (payload.get("customer_id") or "").strip()
            want_status = (payload.get("status") or "").strip() or None
            d, err = _draft_latest_for_customer(cid, want_status)
            self._send(200, {"ok": True, "draft": d,
                             "found": d is not None, "error": err})
            return

        if action == "drop":
            did = (payload.get("draft_id") or "").strip()
            ok, err = _draft_drop(did)
            self._send(200, {"ok": ok, "error": err})
            return

        self._send(200, {"ok": False,
                         "error": ("action must be save|get|update|mark|"
                                   "latest-for-customer|drop")})

    def _conversation_state(self, payload):
        """POST /conversation-state — silent timestamp updater."""
        cid = (payload.get("customer_id") or "").strip()
        event = (payload.get("event") or "").strip()
        if not cid:
            self._send(200, {"ok": False, "error": "customer_id required"})
            return
        if event not in ("customer_message", "operator_reply",
                         "nudge_drafted", "draft_posted"):
            self._send(200, {"ok": False,
                             "error": "event must be customer_message|"
                                      "operator_reply|nudge_drafted|"
                                      "draft_posted"})
            return
        try:
            _out, err = upsert_conversation_state(cid, event)
            if err:
                log("conversation_state err:", err)
                self._send(200, {"ok": False, "degraded": True,
                                 "error": err[:200]})
                return
            self._send(200, {"ok": True, "customer_id": cid, "event": event})
        except Exception as e:
            log("conversation_state EXC:", repr(e))
            self._send(200, {"ok": False, "degraded": True, "error": str(e)})


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
