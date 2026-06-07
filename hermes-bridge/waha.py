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
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request


# --- credentials + base URL ----------------------------------------
WAHA_API_KEY = os.environ.get("WAHA_API_KEY", "")

# WAHA_BASE precedence + SELF-HEALING container IP.
#
# WAHA's :3000 is NOT published to the host, so the bridge reaches WAHA by
# its docker-bridge container IP — which docker REASSIGNS whenever the
# container is recreated. Production incident 2026-05-30: a stack restart
# moved WAHA 172.18.0.4 -> .5 and caddy took over .4; the pinned
# WAHA_BASE then pointed at caddy -> ConnectionRefused on EVERY bridge
# WAHA call (history/pushname/phone/flags/reconcile silently degraded for
# hours — Hermes was drafting with no chat history). To make this class
# of failure self-correct, we resolve the live container IP via
# `docker inspect` (the same docker CLI db.py already shells into for
# psql/redis — no new dependency), cache it, and re-resolve on any
# connection-level failure.
#
# An explicit WAHA_BASE that is NOT a raw container IP (e.g. a hostname
# like https://waha.<domain> or a published 127.0.0.1:<port>) is honoured
# verbatim — it's already stable, so we don't second-guess the operator.
WAHA_BASE_ENV = os.environ.get("WAHA_BASE", "").rstrip("/")
WAHA_CONTAINER = os.environ.get("WAHA_CONTAINER", "n8n-waha-1")
WAHA_PORT = os.environ.get("WAHA_PORT", "3000")

_CONTAINER_IP_URL_RE = re.compile(r"^https?://(?:\d{1,3}\.){3}\d{1,3}:\d+$")
_WAHA_BASE_CACHE = {"base": "", "exp": 0.0}
_WAHA_BASE_TTL = 60.0  # seconds


def _resolve_waha_container_ip():
    """Return n8n-waha-1's current docker-bridge IPAddress, or '' on any
    failure (docker missing, container down, timeout). Non-raising."""
    try:
        r = subprocess.run(
            ["docker", "inspect", "-f",
             "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
             WAHA_CONTAINER],
            capture_output=True, text=True, timeout=5)
        ip = (r.stdout or "").strip()
        return ip if (r.returncode == 0 and ip) else ""
    except Exception:
        return ""


def waha_base(force_refresh=False):
    """Resolve the WAHA base URL, auto-healing the container IP.

    Precedence:
      1. A STABLE explicit WAHA_BASE (hostname / published-port URL) wins.
      2. Live container IP via docker inspect (cached _WAHA_BASE_TTL s).
      3. WAHA_BASE_ENV (even a container IP) as a last-resort fallback
         when docker inspect can't answer (e.g. docker not yet up on boot).
    """
    if WAHA_BASE_ENV and not _CONTAINER_IP_URL_RE.match(WAHA_BASE_ENV):
        return WAHA_BASE_ENV
    now = time.time()
    if (not force_refresh and _WAHA_BASE_CACHE["base"]
            and _WAHA_BASE_CACHE["exp"] > now):
        return _WAHA_BASE_CACHE["base"]
    ip = _resolve_waha_container_ip()
    if ip:
        base = "http://%s:%s" % (ip, WAHA_PORT)
        _WAHA_BASE_CACHE["base"] = base
        _WAHA_BASE_CACHE["exp"] = now + _WAHA_BASE_TTL
        return base
    return _WAHA_BASE_CACHE["base"] or WAHA_BASE_ENV


# Back-compat constant for callers that import WAHA_BASE directly. Runtime
# transport (_waha_get/_waha_post) calls waha_base() so it always uses the
# live value; this snapshot is just a sane default resolved at import.
WAHA_BASE = waha_base() or WAHA_BASE_ENV

# In-process cache for WAHA pushName lookups — keyed by cid, value is
# (push_name, expires_at_ts). 5-minute TTL is plenty; WAHA's chat list
# rarely changes mid-conversation and a bridge restart purges. Prevents
# hammering WAHA on every inbound message (Customer Facts fires per-msg).
_WAHA_PUSHNAME_CACHE = {}
_WAHA_PUSHNAME_TTL = 300  # seconds

# System / non-contact display labels that WAHA returns for non-customer
# chats — filter them out as pushName candidates.
_WAHA_SYSTEM_NAMES = ("WhatsApp Business", "Dubriani admin chat")


def _waha_post(path, body, timeout=30):
    """POST against WAHA REST API. Returns
    (parsed_json_or_None, err_str_or_None). Always non-raising.
    Used for sendText / sendImage / sendFile / etc. On a connection-level
    failure (the container IP may have drifted) we re-resolve the base
    once via waha_base(force_refresh=True) and retry before giving up."""
    if not WAHA_API_KEY:
        return None, "WAHA_API_KEY not configured"
    data = json.dumps(body).encode("utf-8")
    for attempt in range(2):
        base = waha_base(force_refresh=(attempt == 1))
        if not base:
            return None, "WAHA base URL unresolved"
        try:
            req = urllib.request.Request(
                base + path, data=data, method="POST",
                headers={"X-Api-Key": WAHA_API_KEY,
                         "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8")), None
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode("utf-8")[:300]
            except Exception:
                detail = "?"
            return None, f"WAHA HTTP {e.code}: {detail}"
        except (urllib.error.URLError, OSError) as e:
            if attempt == 0:
                continue  # re-resolve container IP and retry once
            return None, f"WAHA POST failed: {e!r}"
        except Exception as e:
            return None, f"WAHA POST failed: {e!r}"
    return None, "WAHA POST failed: exhausted retries"


# --- send-boundary scrub: internal operator notes/names never reach a customer
#
# 2026-06-06 incident (Xeno Accounts): a customer received a file caption
# containing the verbatim internal note "...per operator rule 23 (Zayn to
# also send ... manually)". "Zayn" is the OPERATOR (the /draft response field
# is `notes_for_zayn`). The leak path was a file caption (sendFile + sendText
# fallback) that bypassed sanitize_draft_messages(). scrub_outbound() runs at
# THIS send boundary, so EVERY customer-facing path (text / file caption /
# image caption) is covered. Telegram operator cards use a different path
# (_tg_post) and are intentionally NOT scrubbed (the operator must see notes).
_INTERNAL_MARKERS = (r"operator", r"rule\s*\d+", r"manually", r"internal",
                     r"to also send", r"do not send", r"notes?_for_",
                     r"n8n", r"backend")
# A parenthetical/bracketed segment containing any internal marker.
_PAREN_INTERNAL_RE = re.compile(
    r"[\(\[][^\)\]]*(?:%s)[^\)\]]*[\)\]]" % "|".join(_INTERNAL_MARKERS), re.I)
# Bare "per operator rule 23" / "operator rule 7" citations.
_OPRULE_RE = re.compile(r"\b(?:per\s+)?operator\s+rule\s*\d+\b", re.I)
# An internal notes block: "notes_for_zayn: ..." (to end of message).
_NOTES_BLOCK_RE = re.compile(r"\bnotes?_for_\w+.*", re.I | re.S)
# The operator-card pen emoji notes line.
_NOTE_EMOJI_RE = re.compile(r"\s*\U0001F4DD[^\n]*")
# Operator / internal names that must never appear in a customer message.
_INTERNAL_NAMES = ("zayn",)

# Canonical Dubai Harbour map pins (operator 2026-06-06: "when sending location
# it should ALWAYS send this pin"). The n8n prompt had 6 stray pins incl. a fake
# 'DubrianDubaiHarbour'; the bot sent whichever it pattern-matched. This guard
# rewrites ANY non-canonical map link to the canonical DROP-OFF pin so a
# wrong/old/placeholder pin can never reach a customer. The canonical parking
# pin is preserved (it is in the allowed set).
DROP_OFF_PIN = "https://maps.app.goo.gl/1aT4Vtqwe71FKi3AA"
PARKING_PIN = "https://maps.app.goo.gl/kP8dHqhnrizYVxAw8"
_CANONICAL_PINS = (DROP_OFF_PIN, PARKING_PIN)
_MAPS_URL_RE = re.compile(
    r"https?://(?:maps\.app\.goo\.gl/[^\s)]+"
    r"|(?:www\.)?google\.[a-z.]+/maps[^\s)]*"
    r"|goo\.gl/maps/[^\s)]+"
    r"|maps\.google\.[a-z.]+/[^\s)]*)", re.I)


def _canonicalize_pins(text):
    """Replace any NON-canonical map link with the canonical DROP-OFF pin so a
    wrong/old/placeholder pin can never reach a customer. Canonical pins
    (drop-off + parking) are left untouched. Pure; None-safe."""
    if not text:
        return text

    def _sub(m):
        url = m.group(0).rstrip(".,);!?")
        return m.group(0) if url in _CANONICAL_PINS else DROP_OFF_PIN
    return _MAPS_URL_RE.sub(_sub, text)


def scrub_outbound(text):
    """Remove internal operator notes/names from a customer-facing message.
    Returns the cleaned string (may be empty). Never raises. Applied at the
    WAHA send boundary so no send path can leak internal content."""
    if not text:
        return ""
    s = str(text)
    before = s
    s = _NOTES_BLOCK_RE.sub("", s)
    s = _NOTE_EMOJI_RE.sub("", s)
    s = _PAREN_INTERNAL_RE.sub("", s)
    s = _OPRULE_RE.sub("", s)
    for _nm in _INTERNAL_NAMES:
        s = re.sub(r"\b%s\b" % re.escape(_nm), "", s, flags=re.I)
    # rewrite any wrong/stray map pin to the canonical drop-off pin
    s = _canonicalize_pins(s)
    # tidy whitespace + dangling connectors left by removals
    s = re.sub(r"[ \t]{2,}", " ", s)
    s = re.sub(r"[ \t]+([\n.,;:!?])", r"\1", s)
    s = re.sub(r"(?:\s[-–—])+\s*(?=\n|$)", "", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    s = s.strip()
    if s != before.strip():
        try:
            print("[scrub_outbound] stripped internal content from an "
                  "outbound message", file=sys.stderr, flush=True)
        except Exception:
            pass
    return s


def waha_send_file(customer_id, file_url, caption="", filename=None):
    """POST /api/sendFile — send a generic file (PDF, doc, etc.) to a
    WhatsApp customer. Pass file_url (publicly fetchable) or a
    local /-prefixed path for files served by the bridge.
    Returns (ok, err)."""
    if not customer_id or not file_url:
        return False, "customer_id and file_url required"
    caption = scrub_outbound(caption)  # never leak internal notes in a caption
    body = {
        "session": "default",
        "chatId": customer_id,
        "file": {"url": file_url},
        "caption": caption or "",
    }
    if filename:
        body["file"]["filename"] = filename
    resp, err = _waha_post("/api/sendFile", body)
    if err:
        return False, err
    return True, None


def waha_send_image(customer_id, image_url, caption=""):
    """POST /api/sendImage — send an image with optional caption.
    Returns (ok, err)."""
    if not customer_id or not image_url:
        return False, "customer_id and image_url required"
    caption = scrub_outbound(caption)  # never leak internal notes in a caption
    body = {
        "session": "default",
        "chatId": customer_id,
        "file": {"url": image_url},
        "caption": caption or "",
    }
    resp, err = _waha_post("/api/sendImage", body)
    if err:
        return False, err
    return True, None


def waha_send_text(customer_id, text):
    """POST /api/sendText — send a plain text message. Used by the
    /send-file fallback path when WAHA's CORE tier refuses media
    sends (422 'Plus version' error): we degrade gracefully by
    sending the customer a text message containing the Drive link.
    Returns (ok, err)."""
    if not customer_id or not text:
        return False, "customer_id and text required"
    text = scrub_outbound(text)  # never leak internal notes/operator names
    if not text:
        # message was ENTIRELY internal content — do not send a blank
        return False, "blocked: message empty after internal-content scrub"
    body = {
        "session": "default",
        "chatId": customer_id,
        "text": text,
    }
    resp, err = _waha_post("/api/sendText", body)
    if err:
        return False, err
    return True, None


def _waha_get(path, timeout=12):
    """GET against WAHA REST API. Returns
    (parsed_json_or_None, err_str_or_None). Always non-raising — the
    bridge degrades to cached state when WAHA is unreachable so we
    never want this to bubble an exception up to the http server. On a
    connection-level failure (the container IP may have drifted) we
    re-resolve the base once and retry before giving up."""
    if not WAHA_API_KEY:
        return None, "WAHA_API_KEY not configured"
    for attempt in range(2):
        base = waha_base(force_refresh=(attempt == 1))
        if not base:
            return None, "WAHA base URL unresolved"
        try:
            req = urllib.request.Request(
                base + path, headers={"X-Api-Key": WAHA_API_KEY})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8")), None
        except urllib.error.HTTPError as e:
            return None, (f"WAHA {e.code}: "
                          f"{(e.read() or b'').decode('utf-8', 'replace')[:160]}")
        except (urllib.error.URLError, OSError) as e:
            if attempt == 0:
                continue  # re-resolve container IP and retry once
            return None, f"WAHA req failed: {e!r}"
        except Exception as e:
            return None, f"WAHA req failed: {e!r}"
    return None, "WAHA req failed: exhausted retries"


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
    push_name = ""
    # PRIMARY: the contact's WhatsApp display name (pushname). This is the
    # real name the customer set (e.g. 'Lamia', 'Sid'). The chats-list
    # 'name' field is usually just the phone number, which masks the real
    # name — 2026-05-29: many "nameless" leads actually HAD a pushname we
    # never read. Prefer pushname > shortName > name.
    contact, cerr = _waha_get(
        "/api/contacts?session=default&contactId=" + cid)
    if not cerr and isinstance(contact, dict):
        for key in ("pushname", "shortName", "name"):
            pn = (contact.get(key) or "").strip()
            if pn and pn not in _WAHA_SYSTEM_NAMES:
                push_name = pn
                break
    # FALLBACK: the chats-list name (covers cases where the contact
    # endpoint is empty but the chat carries a saved name).
    if not push_name:
        chats, err = _waha_get("/api/default/chats?limit=200")
        if err or not isinstance(chats, list):
            return ""  # don't poison the cache on transient failure
        for c in chats:
            sid = (c.get("_serialized")
                   or (c.get("id") or {}).get("_serialized"))
            if sid == cid:
                pn = (c.get("name") or "").strip()
                if pn and pn not in _WAHA_SYSTEM_NAMES:
                    push_name = pn
                break
    _WAHA_PUSHNAME_CACHE[cid] = (push_name, now_ts + _WAHA_PUSHNAME_TTL)
    return push_name


# cid -> phone resolution cache. The lid<->phone mapping is stable, so a
# longer TTL than pushName is safe.
_WAHA_PHONE_CACHE = {}
_WAHA_PHONE_TTL = 600  # seconds


def phone_for_cid(customer_id):
    """Resolve a WhatsApp customer_id to a human-readable phone number,
    for OPERATOR VERIFICATION before a send. Two id shapes:
      - '<digits>@c.us' → the digits ARE the phone → '+<digits>'
      - '<lid>@lid'     → opaque privacy id (NOT a phone); resolve via
        WAHA's LID API: GET /api/default/lids/<lid>@lid →
        {"lid":"…@lid","pn":"<digits>@c.us"}.
    Returns '+<digits>' on success, '' if unresolvable. Cached 10 min;
    only positive resolutions are cached so a transient WAHA miss retries.

    Why this exists — production incident 2026-05-28: an /assist nudge
    fuzzy-matched a name to the WRONG customer and was sent to a
    different number. The operator couldn't catch it because the card
    showed only a name / opaque @lid. Showing the resolved phone lets the
    operator verify the recipient before tapping Send."""
    cid = (customer_id or "").strip()
    if not cid:
        return ""
    now_ts = time.time()
    hit = _WAHA_PHONE_CACHE.get(cid)
    if hit and hit[1] > now_ts:
        return hit[0]
    phone = ""
    if cid.endswith("@c.us"):
        digits = cid.split("@", 1)[0]
        phone = "+" + digits if digits.isdigit() else ""
    elif cid.endswith("@lid"):
        lid = cid.split("@", 1)[0]
        data, err = _waha_get(f"/api/default/lids/{lid}@lid")
        if not err and isinstance(data, dict):
            pn = (data.get("pn") or "").split("@", 1)[0]
            if pn.isdigit():
                phone = "+" + pn
    if phone:  # cache positive resolutions only
        _WAHA_PHONE_CACHE[cid] = (phone, now_ts + _WAHA_PHONE_TTL)
    return phone


def lid_to_cus(lid_cid):
    """Resolve a '<lid>@lid' customer_id to its authoritative
    '<digits>@c.us' identity via WAHA's LID endpoint
    (GET /api/default/lids/<lid>@lid -> {"pn": "<digits>@c.us"}).
    Returns '' when unresolved.

    Why not /api/contacts: that endpoint's `.id` is ABSENT for WhatsApp
    *Business* contacts (it returns a businessProfile blob), so the
    identity-reconcile silently skipped every business dup. The LID
    endpoint returns `pn` for personal AND business contacts alike."""
    lid = (lid_cid or "").strip()
    if not lid.endswith("@lid"):
        return ""
    data, err = _waha_get("/api/default/lids/" + lid)
    if err or not isinstance(data, dict):
        return ""
    pn = (data.get("pn") or "").strip()
    return pn if pn.endswith("@c.us") else ""


# ── Country flags for /review ──────────────────────────────────────
# Phone calling-code → flag emoji. Longest-prefix match (codes are 1-3
# digits). Covers Dubriani's actual markets (GCC + UK/EU/US + South
# Asia + Africa + CIS). Unknown codes fall back to a neutral flag.
_CALLING_CODE_FLAG = {
    "971": "🇦🇪", "966": "🇸🇦", "974": "🇶🇦", "973": "🇧🇭",
    "965": "🇰🇼", "968": "🇴🇲", "967": "🇾🇪", "961": "🇱🇧",
    "962": "🇯🇴", "963": "🇸🇾", "964": "🇮🇶", "972": "🇮🇱",
    "98": "🇮🇷", "20": "🇪🇬", "212": "🇲🇦", "213": "🇩🇿",
    "216": "🇹🇳", "218": "🇱🇾", "249": "🇸🇩", "252": "🇸🇴",
    "234": "🇳🇬", "254": "🇰🇪", "27": "🇿🇦", "251": "🇪🇹",
    "44": "🇬🇧", "353": "🇮🇪", "33": "🇫🇷", "49": "🇩🇪",
    "39": "🇮🇹", "34": "🇪🇸", "351": "🇵🇹", "31": "🇳🇱",
    "32": "🇧🇪", "41": "🇨🇭", "43": "🇦🇹", "30": "🇬🇷",
    "90": "🇹🇷", "7": "🇷🇺", "380": "🇺🇦", "994": "🇦🇿",
    "995": "🇬🇪", "998": "🇺🇿", "48": "🇵🇱", "40": "🇷🇴",
    "1": "🇺🇸", "91": "🇮🇳", "92": "🇵🇰", "880": "🇧🇩",
    "94": "🇱🇰", "977": "🇳🇵", "93": "🇦🇫", "86": "🇨🇳",
    "65": "🇸🇬", "60": "🇲🇾", "62": "🇮🇩", "63": "🇵🇭",
    "66": "🇹🇭", "84": "🇻🇳", "81": "🇯🇵", "82": "🇰🇷",
    "61": "🇦🇺", "64": "🇳🇿",
    # Baltics, Nordics, Central/SE Europe, CIS — common Dubriani EU clientele
    "370": "🇱🇹", "371": "🇱🇻", "372": "🇪🇪", "358": "🇫🇮",
    "46": "🇸🇪", "47": "🇳🇴", "45": "🇩🇰", "354": "🇮🇸",
    "420": "🇨🇿", "421": "🇸🇰", "36": "🇭🇺", "359": "🇧🇬",
    "385": "🇭🇷", "386": "🇸🇮", "381": "🇷🇸", "382": "🇲🇪",
    "389": "🇲🇰", "355": "🇦🇱", "357": "🇨🇾", "356": "🇲🇹",
    "352": "🇱🇺", "375": "🇧🇾", "373": "🇲🇩", "374": "🇦🇲",
    "377": "🇲🇨", "350": "🇬🇮",
    # Asia-Pacific & others
    "852": "🇭🇰", "886": "🇹🇼", "853": "🇲🇴", "960": "🇲🇻",
    "673": "🇧🇳", "855": "🇰🇭", "856": "🇱🇦", "95": "🇲🇲",
    "976": "🇲🇳",
}

# Bulk lid→phone map, cached. /review resolves many @lid customers at
# once, so we fetch the whole map in ONE WAHA call instead of per-cid.
_LID_MAP_CACHE = {"map": {}, "exp": 0.0}
_LID_MAP_TTL = 600  # seconds


def _get_lid_phone_map():
    """Return {'<lid>@lid': '<digits>'} from WAHA, cached 10 min. Empty
    dict on any WAHA error (callers degrade to no-flag)."""
    now_ts = time.time()
    if _LID_MAP_CACHE["map"] and _LID_MAP_CACHE["exp"] > now_ts:
        return _LID_MAP_CACHE["map"]
    rows, err = _waha_get("/api/default/lids?limit=10000")
    if err or not isinstance(rows, list):
        return _LID_MAP_CACHE["map"]  # keep stale map rather than wipe
    m = {}
    for r in rows:
        lid = (r.get("lid") or "").strip()
        pn = (r.get("pn") or "").split("@", 1)[0]
        if lid and pn.isdigit():
            m[lid] = pn
    if m:
        _LID_MAP_CACHE["map"] = m
        _LID_MAP_CACHE["exp"] = now_ts + _LID_MAP_TTL
    return m


def country_flag_for_cid(customer_id):
    """Flag emoji for a customer based on their phone country code.
    @c.us → digits are the phone; @lid → resolve via the cached lid map.
    Returns '' when the phone can't be resolved (no flag rather than a
    wrong one)."""
    cid = (customer_id or "").strip()
    if not cid:
        return ""
    if cid.endswith("@c.us"):
        digits = cid.split("@", 1)[0]
    elif cid.endswith("@lid"):
        digits = _get_lid_phone_map().get(cid, "")
    else:
        digits = "".join(c for c in cid if c.isdigit())
    digits = "".join(c for c in digits if c.isdigit())
    if not digits:
        return ""
    for length in (3, 2, 1):
        flag = _CALLING_CODE_FLAG.get(digits[:length])
        if flag:
            return flag
    return "🏳️"  # resolved a number but unknown country code


def display_phone_for_cid(customer_id):
    """'+<digits>' for a cid using the SAME cached sources as the country flag
    (@c.us digits; @lid via the cached bulk lid map) — cheap for /review which
    resolves many leads at once. '' when unresolvable. Lets the operator find a
    lead by NUMBER, not just name (operator 2026-05-31)."""
    cid = (customer_id or "").strip()
    if cid.endswith("@c.us"):
        d = cid.split("@", 1)[0]
        return "+" + d if d.isdigit() else ""
    if cid.endswith("@lid"):
        d = _get_lid_phone_map().get(cid, "")
        return "+" + d if (d and d.isdigit()) else ""
    return ""


_SESSION_HEALTH = {"ok": True, "status": "", "exp": 0.0}


def waha_session_ok(ttl=30.0):
    """(ok, status) for the default WAHA session, cached `ttl`s so it is cheap
    to call on the draft path. ok=True only when status == 'WORKING'. A probe
    error → (False, 'UNREACHABLE'). Advisory only — callers WARN, never block.
    `_waha_get` already retries + self-heals the container IP, so an error here
    means WAHA is genuinely not answering (operator 2026-05-31)."""
    now = time.time()
    if now < _SESSION_HEALTH["exp"]:
        return _SESSION_HEALTH["ok"], _SESSION_HEALTH["status"]
    data, err = _waha_get("/api/sessions/default", timeout=5)
    if err:
        ok, status = False, "UNREACHABLE"
    elif isinstance(data, dict):
        status = str(data.get("status") or "").upper()
        ok = (status == "WORKING")
    else:
        ok, status = False, "UNKNOWN"
    _SESSION_HEALTH.update(ok=ok, status=status, exp=now + ttl)
    return ok, status


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


def _waha_msg_id(m):
    """Extract a stable provider message id from a WAHA message dict, or None.
    WAHA's `id` is sometimes a bare string and sometimes an object with a
    `_serialized` form — normalise both."""
    i = m.get("id")
    if isinstance(i, dict):
        i = i.get("_serialized") or i.get("id")
    i = (str(i).strip() if i not in (None, "") else "")
    return i or None


def waha_fetch_raw(customer_id, limit=100):
    """RAW message list for the durable conversation store (migration 010) —
    the top-up half of build_analyzer_history + the WAHA seed half of
    scripts/backfill_conversation_messages.py.

    Returns a list of {ts:int, direction:'in'|'out', body:str, msg_id:str|None},
    oldest-first (body may be empty — callers that build transcript filter
    empties). Unlike waha_fetch_history this does NOT format / truncate / cap —
    it surfaces the structured messages so the durable store can window them
    itself. NON-RAISING: any error / no-history -> [] (the caller then keeps its
    durable rows or falls back, so a WAHA outage never breaks analysis)."""
    try:
        msgs, err = _waha_get(
            f"/api/default/chats/{customer_id}/messages?"
            f"limit={limit}&downloadMedia=false")
        if err or not isinstance(msgs, list) or not msgs:
            return []
        out = []
        for m in sorted(msgs, key=lambda x: x.get("timestamp", 0)):
            if not isinstance(m, dict):
                continue
            out.append({
                "ts": int(m.get("timestamp") or 0),
                "direction": "out" if m.get("fromMe") else "in",
                "body": (m.get("body") or ""),
                "msg_id": _waha_msg_id(m),
            })
        return out
    except Exception:
        return []
