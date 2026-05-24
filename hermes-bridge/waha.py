#!/usr/bin/env python3
"""waha.py — WAHA REST API client.

WAHA is the WhatsApp HTTP layer (https://waha.devlike.pro). This module
wraps the small subset of its API we use:

  - GET /api/default/chats             — chat list (for pushName lookup)
  - GET /api/default/chats/<cid>/messages   — recent message history

Keep this module thin — it's just transport + a small in-process
pushName cache. Domain logic (extraction, scoring, sending) lives
upstream and calls into waha_fetch_history() etc.
"""
import json
import os
import time
import urllib.error
import urllib.request


# --- credentials + base URL ----------------------------------------
WAHA_API_KEY = os.environ.get("WAHA_API_KEY", "")
WAHA_BASE = os.environ.get("WAHA_BASE", "").rstrip("/")

# In-process cache for WAHA pushName lookups — keyed by cid, value is
# (push_name, expires_at_ts). 5-minute TTL is plenty; WAHA's chat list
# rarely changes mid-conversation and a bridge restart purges. Prevents
# hammering WAHA on every inbound message (Customer Facts fires per-msg).
_WAHA_PUSHNAME_CACHE = {}
_WAHA_PUSHNAME_TTL = 300  # seconds

# System / non-contact display labels that WAHA returns for non-customer
# chats — filter them out as pushName candidates.
_WAHA_SYSTEM_NAMES = ("WhatsApp Business", "Dubriani admin chat")


def _waha_get(path, timeout=12):
    """GET against WAHA REST API. Returns
    (parsed_json_or_None, err_str_or_None). Always non-raising — the
    bridge degrades to cached state when WAHA is unreachable so we
    never want this to bubble an exception up to the http server."""
    if not (WAHA_API_KEY and WAHA_BASE):
        return None, "WAHA_API_KEY/WAHA_BASE not configured"
    try:
        req = urllib.request.Request(
            WAHA_BASE + path,
            headers={"X-Api-Key": WAHA_API_KEY})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8")), None
    except urllib.error.HTTPError as e:
        return None, (f"WAHA {e.code}: "
                      f"{(e.read() or b'').decode('utf-8', 'replace')[:160]}")
    except Exception as e:
        return None, f"WAHA req failed: {e!r}"


def waha_lookup_push_name(customer_id):
    """Return a useful display name for the given customer_id by
    querying WAHA's /api/default/chats. Returns '' when WAHA is
    unconfigured, the chat isn't found, or the name is one of the
    system labels in _WAHA_SYSTEM_NAMES.

    Phone-string names (e.g. '+44 7869 651761') are KEPT — they match
    what the operator sees on their WhatsApp client, so they're a
    useful fallback when no real name is available.

    Cached in-process for 5 minutes per cid."""
    cid = (customer_id or "").strip()
    if not cid:
        return ""
    now_ts = time.time()
    hit = _WAHA_PUSHNAME_CACHE.get(cid)
    if hit and hit[1] > now_ts:
        return hit[0]
    chats, err = _waha_get("/api/default/chats?limit=200")
    if err or not isinstance(chats, list):
        # Don't poison the cache on transient failure
        return ""
    push_name = ""
    for c in chats:
        sid = c.get("_serialized") or (c.get("id") or {}).get("_serialized")
        if sid == cid:
            pn = (c.get("name") or "").strip()
            if pn and pn not in _WAHA_SYSTEM_NAMES:
                push_name = pn
            break
    _WAHA_PUSHNAME_CACHE[cid] = (push_name, now_ts + _WAHA_PUSHNAME_TTL)
    return push_name


def waha_fetch_history(customer_id, limit=30):
    """Pull last N messages from WAHA + pushName. Returns dict:
      {history: '...',     ← oldest-first, last 20 with non-empty body
       last_message: '...',← most recent message body (fromMe or not)
       push_name: '...',
       count: <int>,
       err: None|str}.

    history is shaped for Hermes consumption — 'Dubriani (Xh ago):
    "..."' / 'Customer (Xm ago): "..."' lines. last_message is kept
    for callers that only want the latest text (/info preview,
    customer facts extraction).
    """
    msgs, err = _waha_get(
        f"/api/default/chats/{customer_id}/messages?"
        f"limit={limit}&downloadMedia=false")
    if err:
        return {"history": "", "last_message": "", "push_name": "",
                "count": 0, "err": err}
    if not isinstance(msgs, list) or not msgs:
        return {"history": "First contact, no prior messages.",
                "last_message": "", "push_name": "", "count": 0,
                "err": None}
    # Get pushName from chat list
    chats, _ = _waha_get("/api/default/chats?limit=50")
    push_name = ""
    if isinstance(chats, list):
        for c in chats:
            sid = c.get("_serialized") or \
                (c.get("id") or {}).get("_serialized")
            if sid == customer_id:
                pn = (c.get("name") or "").strip()
                if pn and pn not in _WAHA_SYSTEM_NAMES:
                    push_name = pn
                break
    # Build history string (oldest first, max last 20 with body).
    msgs_sorted = sorted(msgs, key=lambda x: x.get("timestamp", 0))
    with_body = [m for m in msgs_sorted if (m.get("body") or "").strip()]
    if not with_body:
        return {"history": "First contact, no prior messages.",
                "last_message": "", "push_name": push_name,
                "count": 0, "err": None}
    now_ts = int(time.time())
    lines = []
    # Include the entire tail INCLUDING the most recent message. Earlier
    # code used `with_body[-20:-1]` which silently dropped the latest
    # message — /draft-followup only reads `history` (not `last_message`),
    # so the nudge draft never saw the customer's most recent reply.
    # That produced off-context drafts (operator: "nudge draft is not
    # considering his last 10 messages").
    for m in with_body[-20:]:
        who = "Dubriani" if m.get("fromMe") else "Customer"
        secs = max(0, now_ts - (m.get("timestamp") or now_ts))
        ago = (f"{secs // 60}m" if secs < 5400 else
               f"{secs // 3600}h" if secs < 129600 else
               f"{secs // 86400}d")
        body = (m.get("body") or "").replace("\n", " ").strip()[:240]
        lines.append(f'{who} ({ago} ago): "{body}"')
    history = ("\n".join(lines) if lines
               else "First contact, no prior messages.")
    last = (with_body[-1].get("body") or "").strip()[:500]
    return {"history": history, "last_message": last,
            "push_name": push_name, "count": len(with_body), "err": None}
