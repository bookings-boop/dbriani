#!/usr/bin/env python3
"""HubSpot read-side (Layer 1) — inject RETURNING-customer CRM context into drafts.

Live-reads a customer's HubSpot contact at draft time (keyed by phone) and renders a
compact, operator-facing RETURNING CUSTOMER block. The block is appended to
`behavioral_context().formatted` — the single string the live n8n drafter (node
"Claude AI") AND the `build_query` fallback both consume on every customer message.

This MIRRORS profile_lookup.py in shape and safety, but reads HubSpot LIVE (so context
stays fresh as the CRM is updated) instead of a static on-disk index.

SAFETY (parity with profile_lookup.py):
  - Gated by env HUBSPOT_LOOKUP_ENABLED (default OFF). OFF -> context_block() returns
    "" and `formatted` is byte-for-byte unchanged; no token read, no API call.
  - FAIL-OPEN: every public call is wrapped so it can NEVER raise into the draft path.
    Missing token / no match / HTTP non-2xx / timeout / JSON error / anything -> ""
    (the draft proceeds unchanged).
  - Honors the Layer-3 exclusion guard (hermes_exclusion_guards, fail-closed) BEFORE any
    enrichment — staff/crew/agent numbers are never treated as customers.
  - Hard 1.0s timeout on the HubSpot call (stdlib urllib — no new dependency).
  - Short in-process TTL cache keyed by normalized phone (caches the "no match" empty
    result too) — behavioral_context() runs on EVERY message, so this stops re-hitting
    HubSpot within a conversation.
  - NEVER quotes internal figures: drops "Last quote:"-style lines and scrubs
    AED-adjacent amounts, bare 'k' amounts, "<n> each", and price parentheticals; ends
    with a hard "context only — never quote figures" footer.
  - The token is read once from os.environ; it is NEVER logged and NEVER placed in any
    error string (urllib errors carry the URL + status, not request headers).

Single returning-customer context source: keep PROFILE_LOOKUP_ENABLED and
KNOWN_CUSTOMER_PROFILE_ENABLED OFF so blocks never stack.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import Callable, Dict, Optional, Tuple

_API_BASE = "https://api.hubapi.com"
_SEARCH_PATH = "/crm/v3/objects/contacts/search"
_TIMEOUT_S = 1.0
_CACHE_TTL_S = 300.0
_CACHE_MAX = 512
_PROPS = [
    "firstname", "lastname", "phone", "customer_type", "hermes_summary",
    "hermes_interaction_context",
    "hermes_data_source", "hermes_data_quality_flag",
    "last_quote_amount_aed", "last_quote_yacht", "primary_objection",
]

# normalized-phone -> (expires_at_monotonic, rendered_block_or_empty)
_CACHE: Dict[str, Tuple[float, str]] = {}

_PLACEHOLDER_NAMES = {"unknown", "none", "null", "n/a", ""}


# ── env / token ─────────────────────────────────────────────────────────────
def enabled() -> bool:
    """Feature gate. Default OFF — OFF means context_block() is a pure no-op."""
    return os.environ.get("HUBSPOT_LOOKUP_ENABLED", "").strip().lower() in (
        "1", "true", "yes", "on")


def _ctx_read_enabled() -> bool:
    """Sub-gate (default OFF) for ALSO surfacing the live `hermes_interaction_context`
    field (written by cron-hubspot-sync) alongside the audited `hermes_summary`. OFF ->
    behaviour is byte-for-byte the legacy summary-only block. Flipped ON once the
    write-side sync has begun populating interaction_context, closing the read-side gap
    where freshly-synced context was invisible to Hermes."""
    return os.environ.get("HUBSPOT_CTX_READ_ENABLED", "").strip().lower() in (
        "1", "true", "yes", "on")


def _token() -> str:
    return (os.environ.get("HUBSPOT_TOKEN") or "").strip()


# ── identity → phone key (identical rules to profile_lookup / server) ─────────
def normalize_phone(s: Optional[str]) -> str:
    """Bare-digit normalize, mirroring server._normalize_phone (drop leading 00;
    UAE-local 0xxxxxxxxx -> 971..., 5xxxxxxxx -> 9715...)."""
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


def _cid_to_phone(cid: str, resolver: Optional[Callable[[str], str]]) -> str:
    s = (cid or "").strip()
    if not s:
        return ""
    if s.endswith("@lid"):
        return (resolver(s) or "") if resolver else ""   # @lid hash -> resolve via WAHA
    if s.endswith("@c.us"):
        return s.split("@", 1)[0]
    return s


def _is_real_name(name) -> bool:
    if not isinstance(name, str):
        return False
    return name.strip().lower() not in _PLACEHOLDER_NAMES


def _full_name(props: Dict) -> str:
    fn = (props.get("firstname") or "").strip()
    ln = (props.get("lastname") or "").strip()
    return (fn + " " + ln).strip()


# ── no-figure scrub ───────────────────────────────────────────────────────────
# Design note (audited 2026-06-08 against 60 real summaries): in this corpus any
# comma-grouped number ("4,500") or 4-7 digit integer is ALWAYS money — guest
# counts, durations, berths, model numbers (Princess 60) and capacities are 1-3
# digits, and YEARS are 19xx/20xx (protected). So those shapes are stripped on
# sight; bare 3-digit numbers are stripped ONLY when adjacent to a price word.
#
# Drop whole "Last quote / Quote / Price / Paid / Revenue:" lines — pure figures.
_LINE_DROP_RE = re.compile(
    r"(?im)^\s*[-*|\s]*(?:last\s+quote|quote(?:d)?|price|amount\s+paid|paid|revenue|spend)\s*[:|].*$")

# A parenthetical that is wholly price talk: "(wanted 300k, asked for 350k)", "(1500 each)".
_PRICE_PAREN_RE = re.compile(
    r"(?i)\s*\((?=[^)]*\d)[^)]*\b(?:k\b|each\b|aed|dhs?|dirhams?|wanted|asked|quote|cost|price)[^)]*\)")

# Currency / crypto / forex unit tokens, and symbol prefixes.
_CUR = (r"(?:aed|dhs?|dirhams?|درهم|usdt|usdc|usd|eur|euros?|gbp|pounds?|"
        r"btc|eth|sats?)")
_SYM = r"(?:\$|€|£|₿)"
# Optional leading connector so "for 430k" / "at AED 5,000" strip the connector too.
_CONN = r"(?:\b(?:for|at|around|about|approx\.?|of|~)\s+)?"

# Ranges with a money unit: "4-5k", "4,000-5,000 AED", "5k to 10k", "AED 5,000-6,000".
_RANGE_RE = re.compile(
    r"(?i)" + _CONN + r"(?:" + _CUR + r"\s*)?"
    r"\d[\d,]*(?:\.\d+)?\s*k?\s*(?:[-–]|to)\s*"
    r"\d[\d,]*(?:\.\d+)?\s*(?:k\b|" + _CUR + r")")
# Symbol-prefixed: "$5,000", "$5k", "£300".
_SYM_RE = re.compile(r"(?i)" + _SYM + r"\s?\d[\d,]*(?:\.\d+)?\s*k?\b")
# Currency-adjacent either order, optional k / decimals: "AED 6,426", "AED 5k",
# "5,000 dirhams", "0.5 BTC", "5000 USDT", "500 AED".
_AED_RE = re.compile(
    r"(?i)" + _CONN +
    r"(?:" + _CUR + r"\s*\.?\s*\d[\d,]*(?:\.\d+)?\s*k?"
    r"|\d[\d,]*(?:\.\d+)?\s*k?\s*" + _CUR + r")")
# Bare 'k'-suffixed amount: "300k", "50k", "1.5k".
_K_RE = re.compile(r"(?i)" + _CONN + r"\d[\d,]*(?:\.\d+)?\s*k\b")
# Comma-grouped thousands: "4,500", "7,776", "10,000" (the leak class found in audit).
_COMMA_RE = re.compile(r"(?i)" + _CONN + r"\d{1,3}(?:,\d{3})+(?:\.\d+)?\b")
# "1500 each".
_EACH_RE = re.compile(r"(?i)\d[\d,]*\s+each\b")
# Bare 4-7 digit integer that is NOT a year (19xx/20xx) and not a unit count.
_BIGINT_RE = re.compile(
    r"(?i)" + _CONN +
    r"(?<![\d.])(?!(?:19|20)\d\d\b)\d{4,7}"
    r"(?!\s?(?:pax|pp|ppl|guests?|mins?|hrs?|hours?|people|ft|kg|km))\b")
# 3-digit number glued to a price word ("500 discount", "deposit 600") — keep the word.
_PW = (r"(?:aed|dhs?|dirhams?|price|cost|costs?|paid|pay|quote[d]?|budget|cash|"
       r"deposit|balance|refund|discount|voucher|charge|fee)")
_PW_NUM_RE = re.compile(r"(?i)(" + _PW + r"\W{0,6})\d{3}(?!\d)")   # word then 3-digit
_NUM_PW_RE = re.compile(r"(?i)(?<![\d.])\d{3}(\W{0,6}" + _PW + r")")  # 3-digit then word

# Bare 3-digit number (Dubriani hourly rates are 3-digit, e.g. Bliss 900) that is NOT a
# unit count and NOT part of a YYYY-MM-DD date. interaction_context is fed to the live
# drafter and may have been written by tools (context_map.js, the Jun-14 backfill) that
# did NOT pre-strip rates, so the read-side strips bare 3-digit figures defensively. The
# date guard (?<![\d.\-]) / (?![\d.\-]) keeps "2026-06-14" intact.
_CTX_RATE_RE = re.compile(
    r"(?i)(?<![\d.\-])\d{3}(?![\d.\-])"
    r"(?!\s?(?:pax|pp|ppl|guests?|people|ft|feet|hrs?|hours?|mins?|nights?|days?|"
    r"cabins?|berths?|knots?|kg|km|m)\b)")


def _scrub_money(text: str) -> str:
    """Remove price figures from the summary while keeping non-financial context.
    Conservative: over-stripping loses nuance (safe); under-stripping would leak a
    price (unsafe), so when in doubt this removes."""
    if not isinstance(text, str) or not text.strip():
        return ""
    out = []
    for line in text.split("\n"):
        if _LINE_DROP_RE.match(line):
            continue
        line = _PRICE_PAREN_RE.sub("", line)
        line = _RANGE_RE.sub("", line)
        line = _SYM_RE.sub("", line)
        line = _AED_RE.sub("", line)
        line = _K_RE.sub("", line)
        line = _COMMA_RE.sub("", line)
        line = _EACH_RE.sub("", line)
        line = _BIGINT_RE.sub("", line)
        line = _PW_NUM_RE.sub(r"\1", line)               # keep price word, drop 3-digit
        line = _NUM_PW_RE.sub(r"\1", line)
        line = re.sub(r"\s{2,}", " ", line)              # collapse double spaces
        line = re.sub(r"\s+([;,.])", r"\1", line)         # " ;" -> ";"
        line = re.sub(r"\(\s*\)", "", line)               # empty "()" leftover
        line = re.sub(r"(?i)\b(?:for|at|of|around|about|approx\.?|from|to|vs\.?)\s*"
                      r"([;,.)]|$)", r"\1", line)         # dangling connector
        line = line.rstrip(" ;,-\t")
        if re.match(r"(?i)^[-*|\s]*[\w /-]{1,20}:\s*$", line):
            continue                                      # label with no value left
        if line.strip():
            out.append(line)
    return "\n".join(out).strip()


# ── HubSpot read (live, 1s timeout, never raises, never logs the token) ───────
def _search_payload(phone_digits: str) -> Dict:
    plus = "+" + phone_digits
    return {
        # multiple filterGroups are OR-ed: match phone stored with or without '+'
        "filterGroups": [
            {"filters": [{"propertyName": "phone", "operator": "EQ", "value": plus}]},
            {"filters": [{"propertyName": "phone", "operator": "EQ", "value": phone_digits}]},
        ],
        "properties": _PROPS,
        "limit": 1,
    }


def _http_search(phone_digits: str) -> Optional[Dict]:
    """One HubSpot contacts search by phone. Returns the first contact's properties
    dict, or None. NEVER raises; NEVER logs the token."""
    tok = _token()
    if not tok or not phone_digits:
        return None
    try:
        body = json.dumps(_search_payload(phone_digits)).encode("utf-8")
        req = urllib.request.Request(
            _API_BASE + _SEARCH_PATH, data=body, method="POST",
            headers={"Authorization": "Bearer " + tok,
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as r:
            if not (200 <= getattr(r, "status", 0) < 300):
                return None
            data = json.loads(r.read().decode("utf-8") or "{}")
        results = data.get("results") or []
        if not results:
            return None
        props = results[0].get("properties")
        return props if isinstance(props, dict) else None
    except Exception:  # noqa: BLE001 — timeout/HTTP/JSON/anything -> no enrichment
        return None


# ── short TTL cache (caches the empty "no match" result too) ──────────────────
def _cache_get(key: str) -> Optional[str]:
    """Return the cached block (possibly "") or None if not cached / expired."""
    v = _CACHE.get(key)
    if not v:
        return None
    expires, block = v
    if time.monotonic() >= expires:
        _CACHE.pop(key, None)
        return None
    return block


def _cache_put(key: str, block: str) -> None:
    if len(_CACHE) >= _CACHE_MAX:
        for k in sorted(_CACHE, key=lambda k: _CACHE[k][0])[:max(1, _CACHE_MAX // 10)]:
            _CACHE.pop(k, None)
    _CACHE[key] = (time.monotonic() + _CACHE_TTL_S, block)


def cache_clear() -> None:
    """Test/ops helper — drop all cached lookups."""
    _CACHE.clear()


# ── render ────────────────────────────────────────────────────────────────────
def _build_block(phone_key: str) -> str:
    props = _http_search(phone_key)
    if not props:
        return ""
    # skip data flagged low-quality (don't inject known-bad context)
    if str(props.get("hermes_data_quality_flag", "")).strip().lower() in ("true", "1", "yes"):
        return ""
    name = _full_name(props)
    if not _is_real_name(name):
        return ""
    summary = _scrub_money((props.get("hermes_summary") or "").strip())
    # Fresh live rollup (written by cron-hubspot-sync) — only surfaced when the
    # sub-gate is ON. When OFF, ctx is "" and the block is the legacy summary-only form.
    # Scrub money AND bare 3-digit rates (this field feeds the drafter; not all writers
    # pre-strip). hermes_summary is left on its legacy _scrub_money path (unchanged).
    ctx = ""
    if _ctx_read_enabled():
        ctx = _scrub_money((props.get("hermes_interaction_context") or "").strip())
        ctx = re.sub(r"\s{2,}", " ", _CTX_RATE_RE.sub("", ctx)).strip(" ·;,-")
    if not summary and not ctx:
        return ""  # nothing usable left after scrub
    ctype = (props.get("customer_type") or "").strip()
    head_type = f"  |  Type: {ctype}" if ctype else ""
    lines = [
        "--- RETURNING CUSTOMER — internal CRM context (HubSpot) ---",
        f"Name: {name}{head_type}",
        "Known/returning client — acknowledge the relationship; don't re-ask basics already on file.",
    ]
    if summary:
        lines += ["Behavioral context from prior dealings:", summary]
    if ctx:
        lines += ["Latest interaction summary:", ctx]
    lines.append(
        "(Context only. NEVER quote any past price, amount, berth number, or internal "
        "figure to the customer — these are private historical notes. State nothing as "
        "fact unless it appears in THIS conversation or is listed above.)")
    return "\n".join(lines)


def context_block(cid: str,
                  phone_resolver: Optional[Callable[[str], str]] = None) -> str:
    """Render the RETURNING-customer block for the draft prompt, or "".

    "" when: the feature is disabled, the number is excluded, no phone resolves, no
    HubSpot contact is found, the contact is flagged low-quality, the scrubbed summary
    is empty, or ANY error occurs (fail-open)."""
    try:
        if not enabled():
            return ""
        # Layer-3 exclusion guard (shared block-list, fail-closed). Imported lazily so
        # an import error here falls through to the outer handler -> "" (no injection).
        import hermes_exclusion_guards as _eg
        if _eg.is_excluded(cid, lid_resolver=phone_resolver):
            return ""
        key = normalize_phone(_cid_to_phone(cid, phone_resolver))
        if not key:
            return ""
        cached = _cache_get(key)
        if cached is not None:
            return cached
        block = _build_block(key)
        _cache_put(key, block)
        return block
    except Exception:  # noqa: BLE001 — must never break the draft path
        return ""


if __name__ == "__main__":
    print(f"hubspot_lookup: enabled={enabled()} token_present={bool(_token())} "
          f"cache_size={len(_CACHE)}")
