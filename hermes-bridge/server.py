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
                             "/debounce"):
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
        else:
            self._send(400, {"ok": False,
                             "error": "action must be arm|disarm|get"})

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
