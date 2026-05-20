#!/usr/bin/env python3
"""Hermes Bridge — thin HTTP wrapper around the Hermes Agent.

Endpoints (all POST except /health; POSTs require X-Bridge-Token):
  GET  /health      liveness probe
  POST /draft       customer context -> Hermes draft (JSON {messages, notes_for_zayn})
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

SESSION_RE = re.compile(r"session_id:\s*(\S+)")
FENCE_RE = re.compile(r"```(?:json)?", re.IGNORECASE)
VALID_SCOPES = ("global", "customer", "scenario", "tier")


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

    extra = "\"suggested_rule\"" if is_refine else "\"detected_trigger\""
    parts.append(
        "\n--- RESPOND NOW ---\n"
        "Produce your reply using the EXACT JSON output format defined in your "
        "instructions above (the object with \"messages\" and \"notes_for_zayn\", "
        "optionally plus " + extra + "). Output only that single JSON object — "
        "no markdown fences, no commentary before or after it."
    )
    return "\n".join(parts)


def run_hermes(query):
    cmd = [HERMES, "chat", "-q", query, "-Q",
           "--source", "tool", "--yolo", "-t", "memory"]
    env = dict(os.environ)
    env["PATH"] = os.path.dirname(HERMES) + os.pathsep + env.get("PATH", "")
    t0 = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          timeout=HERMES_TIMEOUT, env=env)
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
        if self.path not in ("/draft", "/save-rule"):
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
        log(f"draft OK customer={payload.get('customer_name')!r} "
            f"mode={payload.get('mode', 'initial')} msgs={len(messages)} "
            f"rule_suggested={sugg is not None} trigger={trig_id} "
            f"elapsed={elapsed}ms session={sid}")
        self._send(200, {
            "ok": True,
            "messages": messages,
            "notes_for_zayn": notes,
            "suggested_rule": sugg,
            "detected_trigger": trig if trig_id else None,
            "trigger_id": trig_id,
            "raw": blob,
            "session_id": sid,
            "elapsed_ms": elapsed,
        })

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
