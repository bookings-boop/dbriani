#!/usr/bin/env python3
"""cron-intake-gap.py — NEVER-MISS intake reconciliation (auto-ingest + alert).

A lead reaches WAHA but can still vanish from the system of record: the
WAHA->n8n webhook can miss it (outage / container-IP drift), OR the n8n draft
pipeline drops it (the Claude node 400s on a credit-balance error and its error
branch skips the customer_facts upsert — RCA 2026-06-07, +971568241103 sat
invisible ~1h26m). The old net only ALERTED (manual /refresh), paged just the
top-150 of 585 chats, capped at 7 days, ran 4x/day, and FAILED OPEN (went silent
exactly when WAHA was down). This rewrite makes it a self-acting reconciliation:

  1. COMPLETE COVERAGE — page ALL WAHA chats (no limit=150 cap).
  2. IDENTITY-AWARE — fold @lid<->@c.us via WAHA's bulk lid->phone map before
     the absent-from-customer_facts check (no false gaps, no missed @lid drops).
  3. NO AGE CAP — a never-ingested lead never ages out of detection.
  4. AUTO-INGEST — for each gap, call the existing recovery path POST
     /refresh-facts so the lead lands in customer_facts + conversation_state
     immediately, THEN post a ONE-TAP Telegram card (💬 Draft reply / ℹ️ Info
     inline buttons routed by the existing callback router) — never a typed cmd.
  5. FAIL-CLOSED — if WAHA paging OR the customer_facts read fails, ALERT the
     operator that THE NET ITSELF is blind; never silently print+return.

Gentle on WAHA (the bulk burst degrades the session): paged fetch w/ a short
inter-page pause + a per-run recovery cap. Idempotent: a recovered lead joins
customer_facts and is canon-matched out next run; a still-unrecoverable gap
(e.g. WAHA history empty for an @lid) is re-alerted at most once per cooldown.

Config from ~/hermes-bridge/.env (same keys as the other crons). Stdlib only.

CRON CADENCE (operator/crontab change — NOT in this file): tighten from
`30 6,10,14,18 * * *` to `*/10 * * * *` (every 10 min; 5-15 is fine — it is a
cheap idempotent read). See deploy notes."""
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request

HOME = os.path.expanduser("~")
ENV = {}
try:
    for ln in open(os.path.join(HOME, "hermes-bridge", ".env")):
        ln = ln.strip()
        if "=" in ln and not ln.startswith("#"):
            k, v = ln.split("=", 1)
            ENV[k] = v
except FileNotFoundError:
    pass


def getenv(k, d=None):
    v = os.environ.get(k)
    return v if (v is not None and v != "") else ENV.get(k, d)


TG_TOKEN = getenv("ADMIN_TG_TOKEN", "")
CHAT_ID = getenv("ADMIN_CHAT_ID", "")
PG = getenv("BRIDGE_PG_CONTAINER", "n8n-postgres-1")
WAHA_KEY = getenv("WAHA_API_KEY", "")
BRIDGE_PORT = getenv("BRIDGE_PORT", "8788")
BRIDGE_TOKEN = getenv("BRIDGE_TOKEN", "")

# No age cap for the never-ingested case (None) — a missed lead can't age out.
MAX_AGE_DAYS = None
# Be gentle on the WAHA session: page size, inter-page pause, page ceiling.
PAGE_SIZE = int(getenv("INTAKE_PAGE_SIZE", "200") or "200")
PAGE_PAUSE = float(getenv("INTAKE_PAGE_PAUSE", "0.3") or "0.3")
MAX_PAGES = int(getenv("INTAKE_MAX_PAGES", "60") or "60")  # 60*200 = 12k chats
# Cap auto-recoveries per run so a first no-age-cap sweep can't burst WAHA /
# Anthropic; the rest are caught on the next (10-min) run. Idempotent, so safe.
RECOVER_CAP = int(getenv("INTAKE_RECOVER_CAP", "8") or "8")
# Don't re-alert the SAME still-unrecovered gap more than once per cooldown.
REALERT_COOLDOWN_H = float(getenv("INTAKE_REALERT_COOLDOWN_H", "6") or "6")
SEEN_PATH = os.path.join(HOME, "hermes-bridge", ".intake_recon_seen.json")
# Phantom suppression (default ON): a "missed lead" with no inbound message is a
# contradiction — nothing reached WAHA to be missed. When ON, a hollow WAHA stub
# that auto-ingest definitively cannot recover is logged silently instead of
# paging the operator, killing the recurring contentless @lid cards WITHOUT
# hiding a genuine dropped lead. Set INTAKE_PHANTOM_SUPPRESS_ENABLED=0 to restore
# the old always-alert behavior instantly. See _is_phantom().
PHANTOM_SUPPRESS_ENABLED = getenv("INTAKE_PHANTOM_SUPPRESS_ENABLED", "1") != "0"

sys.path.insert(0, os.path.join(HOME, "hermes-bridge"))
from intake import canon_phone, intake_gaps  # noqa: E402


def waha_base():
    """Live WAHA container IP (drift-proof, mirrors waha.py); caddy URL fallback."""
    try:
        r = subprocess.run(
            ["docker", "inspect", "-f",
             "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
             "n8n-waha-1"], capture_output=True, text=True, timeout=10)
        ip = r.stdout.strip()
        if ip:
            return "http://%s:3000" % ip
    except Exception as e:
        print("waha ip resolve err:", repr(e))
    return "https://waha.13-63-82-112.sslip.io"


def _waha_get(base, path):
    url = base.rstrip("/") + path
    req = urllib.request.Request(url, headers={"X-Api-Key": WAHA_KEY})
    with urllib.request.urlopen(req, timeout=40) as resp:
        return json.load(resp)


def waha_chats(base):
    """Page the ENTIRE WAHA chat overview (no limit=150 cap). Loops offset
    until a page returns < PAGE_SIZE rows OR yields no NEW cids (defends against
    a WAHA build that ignores `offset` and re-serves page 1). Gentle: a short
    pause between pages. Raises on a hard fetch failure so main() fails CLOSED."""
    seen, out, offset = set(), [], 0
    for _ in range(MAX_PAGES):
        page = _waha_get(
            base, "/api/default/chats/overview?limit=%d&offset=%d"
            % (PAGE_SIZE, offset))
        if not isinstance(page, list) or not page:
            break
        fresh = 0
        for c in page:
            cid = c.get("id")
            if isinstance(cid, dict):
                cid = cid.get("_serialized") or cid.get("id")
            cid = str(cid or "")
            if cid and cid not in seen:
                seen.add(cid)
                out.append(c)
                fresh += 1
        if len(page) < PAGE_SIZE or fresh == 0:
            break          # last page, or server ignored offset → stop
        offset += PAGE_SIZE
        if PAGE_PAUSE > 0:
            time.sleep(PAGE_PAUSE)
    return out


def lid_phone_map(base):
    """{'<lid>@lid': '<digits>'} from WAHA's bulk lids endpoint (ONE call,
    mirrors waha._get_lid_phone_map) so the canon resolver can fold @lid into
    its @c.us phone. Best-effort: {} on error (canon then keeps @lid distinct,
    which only over-flags — never misses)."""
    try:
        rows = _waha_get(base, "/api/default/lids?limit=10000")
    except Exception as e:
        print("lid-map fetch err (degrading to phone-only canon):", repr(e))
        return {}
    m = {}
    if isinstance(rows, list):
        for r in rows:
            lid = (r.get("lid") or "").strip()
            pn = (r.get("pn") or "").split("@", 1)[0]
            if lid and pn.isdigit():
                m[lid] = pn
    return m


def cf_ids():
    """Ingested customer_ids from customer_facts. Returns (set, ok). ok=False on
    a psql failure so the caller fails CLOSED (an empty set would otherwise flag
    EVERY chat as a gap = noise, AND mask that the net is blind)."""
    try:
        r = subprocess.run(
            ["docker", "exec", PG, "psql", "-U", "hermes_rw", "-d", "n8n",
             "-tA", "-c", "SELECT customer_id FROM customer_facts"],
            capture_output=True, text=True, timeout=25)
        if r.returncode != 0:
            print("psql failed:", r.stderr.strip()[:150])
            return set(), False
        return set(x.strip() for x in r.stdout.splitlines() if x.strip()), True
    except Exception as e:
        print("psql error:", repr(e))
        return set(), False


def tg_send(text, reply_markup=None):
    payload = {"chat_id": CHAT_ID, "text": text}
    if reply_markup is not None:
        payload["reply_markup"] = json.dumps(reply_markup)
    data = urllib.parse.urlencode(payload).encode()
    req = urllib.request.Request(
        "https://api.telegram.org/bot%s/sendMessage" % TG_TOKEN, data=data)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.load(resp).get("ok") is True
    except Exception as e:
        print("telegram send error:", repr(e))
        return False


def refresh_facts(cid):
    """Auto-ingest via the EXISTING recovery path (POST /refresh-facts) — lands
    the lead in customer_facts + conversation_state immediately (no manual
    /refresh). Returns the parsed bridge response, or {} on transport error."""
    if not BRIDGE_TOKEN:
        print("BRIDGE_TOKEN unset — cannot auto-ingest", cid)
        return {}
    body = json.dumps({"customer_id": cid}).encode("utf-8")
    req = urllib.request.Request(
        "http://127.0.0.1:%s/refresh-facts" % BRIDGE_PORT, data=body,
        headers={"Content-Type": "application/json",
                 "X-Bridge-Token": BRIDGE_TOKEN}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            return json.load(resp)
    except Exception as e:
        print("refresh-facts call err for %s: %s" % (cid, repr(e)))
        return {}


def _load_seen():
    try:
        with open(SEEN_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_seen(seen):
    try:
        with open(SEEN_PATH, "w") as f:
            json.dump(seen, f)
    except Exception as e:
        print("seen-cache write err (non-fatal):", repr(e))


def _is_phantom(g, resp):
    """True when a detected gap is NOT a real missed lead but a hollow WhatsApp
    contact stub that can never be actioned — so it should be logged silently
    rather than paged. Requires ALL of:
      • hollow WAHA overview — no usable timestamp (age_days hit intake.py's 1e9
        no-timestamp sentinel), no notifyName, and no last-message text;
      • a DEFINITIVE 'nothing to ingest' answer from auto-ingest — a non-empty
        bridge response with ok is False (handle_refresh_facts -> 'no messages in
        WAHA'). A transport error returns {} (no 'ok' key) and is NOT a phantom,
        so we still alert and fail toward surfacing.
    A genuinely dropped lead is never hollow: its message reached WAHA (the drop
    happened downstream in n8n), so the overview carries a body + timestamp.
    This therefore only matches a contact WAHA lists but has no message for."""
    try:
        age = float(g.get("age_days") or 0)
    except (TypeError, ValueError):
        age = 0.0
    hollow = (age >= 1e9
              and not (g.get("name") or "").strip()
              and not (g.get("body") or "").strip())
    definitive_no_msg = bool(resp) and (resp.get("ok") is False)
    return hollow and definitive_no_msg


def recovery_card(g, ingested_ok):
    """One-tap recovery card: identity + raw last inbound + inline buttons the
    EXISTING callback router already handles (nudge:<cid> drafts a reply via
    handle_draft_followup's owe-reply path; inf:<cid> shows the lead). Plain
    text (no Markdown) so a stray * / _ in the body can't break the card."""
    cid = g["cid"]
    nm = g["name"] or "(no name)"
    body = g["body"] or "(no text)"
    head = ("🆕 Missed lead recovered — ingested + drafting ready"
            if ingested_ok else
            "⚠️ Missed lead detected — auto-ingest INCOMPLETE (WAHA history "
            "empty?); tap to draft anyway")
    text = ("%s\n%s  ·  %s  ·  %sd old\nlast: %s\n"
            "Tap 💬 to draft a reply (one tap — no typing)."
            % (head, cid, nm, g["age_days"], body[:200]))
    markup = {"inline_keyboard": [[
        {"text": "💬 Draft reply", "callback_data": "nudge:" + cid},
        {"text": "ℹ️ Info", "callback_data": "inf:" + cid},
    ]]}
    return text, markup


def alert_blind(reason):
    """FAIL-CLOSED: the net itself is down — tell the operator instead of
    silently returning. Distinguishes 'couldn't check' from '0 gaps'."""
    msg = ("🚨 INTAKE NET BLIND — the never-miss reconciliation could not run: "
           "%s. A lead could be silently dropped right now. Check WAHA / the "
           "postgres container, then it self-recovers on the next run." % reason)
    print("BLIND:", reason)
    tg_send(msg)


def main():
    base = waha_base()

    # 1) FAIL-CLOSED on the WAHA paging fetch.
    try:
        chats = waha_chats(base)
    except Exception as e:
        alert_blind("WAHA chat fetch failed (%s)" % repr(e))
        return
    if not chats:
        alert_blind("WAHA returned 0 chats (session degraded?)")
        return

    # 2) FAIL-CLOSED on the customer_facts read (empty set != healthy).
    ingested, ok = cf_ids()
    if not ok:
        alert_blind("customer_facts read failed (postgres unreachable?)")
        return

    # 3) Identity-aware, no-age-cap gap detection.
    lmap = lid_phone_map(base)
    canon = lambda c: canon_phone(c, lmap)  # noqa: E731
    gaps = intake_gaps(chats, ingested, time.time(),
                       max_age_days=MAX_AGE_DAYS, canon=canon)
    print("intake-recon %s: scanned %d chat(s), %d gap(s)"
          % (time.strftime("%Y-%m-%d %H:%M"), len(chats), len(gaps)))
    if not gaps:
        return  # healthy — 0 gaps (distinct from the BLIND path above)

    # 4) AUTO-INGEST + one-tap card per gap (capped + cooldown-deduped).
    # gaps are sorted most-recent-first, so a genuinely NEW lead is always
    # processed before any old still-stuck one — the cap can't starve it.
    seen = _load_seen()
    now = time.time()
    cooldown = REALERT_COOLDOWN_H * 3600.0
    recovered = posted = skipped_cd = suppressed = 0
    for g in gaps:
        cid = g["cid"]
        # Cooldown: if we already recovered+alerted this cid recently and it is
        # STILL a gap (recovered leads drop out of cf next run, so a re-appearing
        # cid is the unrecoverable kind — e.g. empty WAHA history for an @lid),
        # don't re-ingest/re-alert every run. Fail-open: any cache hiccup →
        # _load_seen() returns {} → we attempt + alert (never silently suppress).
        if (now - seen.get(cid, 0)) < cooldown:
            skipped_cd += 1
            continue
        if recovered >= RECOVER_CAP:
            print("recover cap (%d) hit — remaining caught next run"
                  % RECOVER_CAP)
            break
        resp = refresh_facts(cid)
        ingested_ok = bool(resp.get("ok"))
        recovered += 1
        # Phantom guard: a hollow stub the bridge confirms has no message is not
        # a missed lead — log it silently and stamp `seen` so we also stop
        # re-attempting recovery every run, but never page the operator.
        if PHANTOM_SUPPRESS_ENABLED and _is_phantom(g, resp):
            suppressed += 1
            seen[cid] = now
            print("phantom-suppressed (hollow stub, no message): %s" % cid)
            continue
        text, markup = recovery_card(g, ingested_ok)
        if tg_send(text, markup):
            posted += 1
            seen[cid] = now
    # prune the cache to gaps still relevant (avoid unbounded growth)
    live = {g["cid"] for g in gaps}
    seen = {k: v for k, v in seen.items()
            if k in live and (now - v) < cooldown}
    _save_seen(seen)
    print("intake-recon: auto-ingested %d, posted %d card(s), "
          "phantom-suppressed %d, cooldown-skipped %d"
          % (recovered, posted, suppressed, skipped_cd))


if __name__ == "__main__":
    main()
