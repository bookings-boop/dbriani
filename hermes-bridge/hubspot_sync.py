#!/usr/bin/env python3
"""HubSpot write-side — log WhatsApp relationships to the CRM (summary + activity + linking).

The COUNTERPART to hubspot_lookup.py (read-side). Where the read-side pulls a
returning customer's CRM context into drafts, this writes the bridge's own view of
each conversation BACK to HubSpot so the CRM stays current as chats happen.

WHAT IT WRITES (settled design — operator, 2026-06-15):
  - A fresh relationship SUMMARY rollup -> contact property `hermes_interaction_context`
    ("Status: ... · Yachts: ... · Party: ... · Last contact: ..."). The audited
    migration `hermes_summary` is NEVER touched (it is richer + not regenerable).
  - A short activity NOTE on the contact's timeline (a SUMMARY of the interaction —
    NOT the raw transcript), associated to the contact (note->contact typeId 202).
  - A handful of descriptive structured props (booking_status / yachts_inquired /
    message_count / last_contact_date).
  - Phone-linking: match an existing contact by phone, else CREATE a new one.

WHAT IT NEVER DOES:
  - NEVER ships raw conversation_messages bodies to HubSpot (they stay in Postgres;
    the bridge is their sole writer). Only composed/scrubbed summaries leave the box.
  - NEVER writes money/lifecycle/deal fields, and NEVER overwrites hermes_summary /
    hermes_data_source (see _FORBIDDEN_PROPS + money_guard(), which RAISES).
  - NEVER quotes a figure: every composed string is money-scrubbed AND has bare 3-4
    digit numbers (Dubriani hourly rates) stripped (_scrub_writeside).
  - NEVER keys a contact by name — identity is phone only (@lid resolved via live WAHA).
  - NEVER creates a contact on a FAILED/transient search (would dup the CRM) — only on
    a search that definitively returned zero matches.

SAFETY:
  - Gated by env HUBSPOT_SYNC_ENABLED (default OFF). The orchestrator also takes an
    explicit dry_run flag (default True): a write happens only when the flag is on AND
    dry_run=False.
  - FAIL-SOFT per contact: sync_contact never raises; it returns an action record.
  - Honors the Layer-3 exclusion guard (staff/crew/agents are never synced).
  - HTTP/search are injectable so the pure logic is unit-testable without a network.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import Callable, Dict, List, Optional, Tuple

# Reuse the read-side's proven identity + money-scrub helpers (single source of truth).
from hubspot_lookup import (  # noqa: E402
    _cid_to_phone,
    _scrub_money,
    _token,
    normalize_phone,
)

_API_BASE = "https://api.hubapi.com"
_SEARCH_PATH = "/crm/v3/objects/contacts/search"
_CONTACTS_PATH = "/crm/v3/objects/contacts"
_NOTES_PATH = "/crm/v3/objects/notes"
_TIMEOUT_S = 10.0                      # writes tolerate more latency than the 1s read path
_NOTE_TO_CONTACT_TYPE_ID = 202         # HUBSPOT_DEFINED note -> contact association
_CTX_MAX = 600                         # hermes_interaction_context cap (matches context_map.js)
_NOTE_COOLDOWN_S = 6 * 3600.0          # don't spam the timeline: 1 note / 6h unless status changes

_PLACEHOLDER_NAMES = {"unknown", "none", "null", "n/a", "", "customer", "guest", "client"}

# Properties this module is FORBIDDEN to write. Hard belt against clobbering the audited
# migration summary or ever touching money / lifecycle. money_guard() RAISES on these.
_FORBIDDEN_PROPS = {
    "hermes_summary",           # operator: never overwrite the audited migration text
    "hermes_data_source",       # operator: never touch
    "hermes_data_quality_flag",
    "amount", "deal_currency_code", "dealstage", "pipeline", "closedate",
    "lifecyclestage", "hs_lead_status",
    "last_quote_amount_aed", "last_quote_yacht", "primary_objection",
}

# Bare 3-4 digit number that is NOT a 19xx/20xx year and NOT a legit unit count
# (guests/ft/hrs/...). Dubriani hourly rates are exactly 3-digit (e.g. Bliss 900), so
# these are stripped from write-side free-text on top of _scrub_money's 4-7 digit pass.
_WRITE_NUM_RE = re.compile(
    r"(?i)(?<![\d.])(?!(?:19|20)\d\d\b)\d{3,4}"
    r"(?!\s?(?:pax|pp|ppl|guests?|people|persons?|ft|feet|hrs?|hours?|mins?|minutes?|"
    r"nights?|days?|cabins?|berths?|knots?|kg|km|m)\b)")


# ── env / gate ────────────────────────────────────────────────────────────────
def enabled() -> bool:
    """Feature gate. Default OFF — OFF means the cron is a no-op (no reads, no writes)."""
    return os.environ.get("HUBSPOT_SYNC_ENABLED", "").strip().lower() in (
        "1", "true", "yes", "on")


# ── scrub (money + bare-rate) ───────────────────────────────────────────────────
def _scrub_writeside(text: Optional[str]) -> str:
    """Money-scrub (shared with the read-side) PLUS strip bare 3-4 digit figures —
    Dubriani hourly rates are 3-digit and the read-side re-injects interaction_context
    into drafts, so we over-strip rather than risk leaking a rate."""
    t = _scrub_money(str(text or ""))
    if not t:
        return ""
    t = _WRITE_NUM_RE.sub("", t)
    t = re.sub(r"\s{2,}", " ", t)
    t = re.sub(r"\s+([;,.·])", r"\1", t)
    return t.strip(" ·;,-\t")


# ── pure composition helpers (unit-tested, no network) ──────────────────────────
def derive_booking_status(facts: Dict) -> str:
    """Descriptive status from the live label + booked_yacht (mirrors
    context_scrub.js deriveBookingStatus). booked_yacht is the ONLY trustworthy
    "did they book" signal — dates/booking_date_abs are inquiry traps."""
    label = (str(facts.get("label") or "")).strip().upper()
    if (str(facts.get("booked_yacht") or "")).strip() or label == "CONFIRMED":
        return "booked"
    if label == "LOST":
        return "lost"
    if label == "DISREGARDED":
        return "disregarded"
    if (str(facts.get("yachts") or "").strip()
            and str(facts.get("dates") or "").strip()):
        return "quoted"
    return "enquiry"


def name_parts(name: Optional[str]) -> Tuple[str, str]:
    """(firstname, lastname) from a stored name, dropping digit/email/handle tokens
    and TitleCasing (mirrors loader_v2.js nameParts). Returns ("","") for a
    placeholder / unusable name (caller then sets no name)."""
    n = (name or "").strip()
    if not n or n.lower() in _PLACEHOLDER_NAMES:
        return ("", "")
    toks = [t for t in re.split(r"\s+", n)
            if t and not re.search(r"\d", t) and "@" not in t]
    if not toks:
        return ("", "")
    toks = [t[:1].upper() + t[1:] if t[:1].islower() else t for t in toks]
    first = toks[0]
    last = " ".join(toks[1:]) if len(toks) > 1 else ""
    return (first, last)


def _iso_date(raw: Optional[str]) -> str:
    """Postgres timestamptz string -> 'YYYY-MM-DD' (HubSpot date prop). '' if unparseable."""
    s = (str(raw or "")).strip()
    m = re.match(r"(\d{4}-\d{2}-\d{2})", s)
    return m.group(1) if m else ""


def _epoch_of(raw: Optional[str]) -> float:
    """Best-effort epoch seconds for a Postgres timestamptz; 0.0 if unparseable.
    Used only for note provenance (hs_timestamp), NOT for the cooldown clock."""
    s = (str(raw or "")).strip()
    if not s:
        return 0.0
    s = s.replace(" ", "T", 1)
    s = re.sub(r"([+-]\d{2})$", r"\1:00", s)   # '+00' -> '+00:00'
    for cand in (s, s + "+00:00", re.sub(r"[+-]\d{2}:?\d{2}$", "", s)):
        try:
            import datetime
            return datetime.datetime.fromisoformat(cand).timestamp()
        except (ValueError, OSError):
            continue
    return 0.0


def compose_context(facts: Dict) -> str:
    """The fresh AI/human rollup written to hermes_interaction_context. Built from
    STRUCTURED fields only, scrubbed, capped. NO raw transcript, no analyzer prose."""
    parts: List[str] = ["Status: " + derive_booking_status(facts)]
    yachts = _scrub_writeside(facts.get("booked_yacht") or facts.get("yachts"))
    if yachts:
        parts.append("Yachts: " + yachts)
    dates = _scrub_writeside(facts.get("dates"))
    if dates:
        parts.append("Date asked: " + dates)
    party = _scrub_money(str(facts.get("party_size") or "").strip())
    if party:
        parts.append("Party: " + party)
    lc = _iso_date(facts.get("last_customer_message_at"))
    if lc:
        parts.append("Last contact: " + lc)
    return " · ".join(parts)[:_CTX_MAX].rstrip(" ·")


def compose_note(facts: Dict) -> str:
    """The human-timeline activity note body — a SUMMARY of the relationship state,
    NOT the raw conversation. Each free-text field is scrubbed individually; the
    structured message_count int is preserved verbatim."""
    status = derive_booking_status(facts)
    lines = ["WhatsApp update — status: " + status]
    yachts = _scrub_writeside(facts.get("booked_yacht") or facts.get("yachts"))
    if yachts:
        lines.append("Yacht(s): " + yachts)
    dates = _scrub_writeside(facts.get("dates"))
    if dates:
        lines.append("Dates discussed: " + dates)
    party = _scrub_money(str(facts.get("party_size") or "").strip())
    if party:
        lines.append("Party size: " + party)
    mc = str(facts.get("message_count") or "").strip()
    if mc.isdigit():
        lines.append("Messages exchanged: " + mc)   # structured int — preserved
    read = _scrub_writeside(facts.get("importance_reasoning"))
    if read:
        lines.append("Analyzer read: " + read)
    action = _scrub_writeside(facts.get("suggested_action"))
    if action:
        lines.append("Suggested next step: " + action)
    return ("\n".join(lines)
            + "\n\n(Auto-logged from WhatsApp by Hermes — relationship summary, "
              "not a verbatim transcript. Never quote internal figures to the customer.)")


def build_props(facts: Dict, *, for_create: bool) -> Dict[str, str]:
    """Assemble the descriptive property payload. Name/phone are set ONLY on create
    (we never overwrite an existing contact's name — it may be operator-corrected)."""
    props: Dict[str, str] = {}
    ctx = compose_context(facts)
    if ctx:
        props["hermes_interaction_context"] = ctx
    props["hermes_booking_status"] = derive_booking_status(facts)
    yachts = _scrub_writeside(facts.get("yachts"))
    if yachts:
        props["hermes_yachts_inquired"] = yachts[:255]
    mc = str(facts.get("message_count") or "").strip()
    if mc.isdigit():
        props["hermes_message_count"] = mc
    lc = _iso_date(facts.get("last_customer_message_at"))
    if lc:
        props["hermes_last_contact_date"] = lc
    if for_create:
        first, last = name_parts(facts.get("name"))
        if first:
            props["firstname"] = first
        if last:
            props["lastname"] = last
    return props


def money_guard(props: Dict[str, str]) -> None:
    """HARD belt: refuse to write any forbidden property. RAISES (not assert — asserts
    are stripped under python -O) so a bug can NEVER clobber hermes_summary or write a
    money/lifecycle field."""
    bad = sorted(set(props) & _FORBIDDEN_PROPS)
    if bad:
        raise RuntimeError(
            "hubspot_sync money_guard: forbidden property write blocked: %r" % bad)


def should_create_note(status: str, prev: Optional[Dict], now_ts: float,
                       cooldown_s: float = _NOTE_COOLDOWN_S) -> bool:
    """Timeline-spam guard: post a note on first sync, on a status change, or once
    per cooldown window. now_ts MUST be a wall-clock time (same clock as the stored
    last_note_ts) so the cooldown subtraction is meaningful."""
    if not prev:
        return True
    if (prev.get("last_status") or "") != status:
        return True
    last_note = float(prev.get("last_note_ts") or 0.0)
    return (now_ts - last_note) >= cooldown_s


# ── HTTP (injectable; never raises; never logs the token; backs off on 429) ─────
def _http(method: str, path: str, payload: Optional[Dict],
          _retry: bool = True) -> Tuple[int, Optional[Dict], str]:
    """One HubSpot REST call. Returns (status, json_or_None, err_str). status==0 means
    the call did not complete (timeout/url/json). Never raises; never logs the token."""
    tok = _token()
    if not tok:
        return (0, None, "no token")
    try:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            _API_BASE + path, data=data, method=method,
            headers={"Authorization": "Bearer " + tok,
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as r:
            status = getattr(r, "status", 0)
            raw = r.read().decode("utf-8") or "{}"
        return (status, json.loads(raw), "")
    except urllib.error.HTTPError as e:
        if e.code == 429 and _retry:                # rate limited — honor Retry-After once
            try:
                wait = min(float(e.headers.get("Retry-After")), 10.0)
            except (TypeError, ValueError, AttributeError):
                wait = 2.0
            time.sleep(max(wait, 1.0))
            return _http(method, path, payload, _retry=False)
        try:
            body = e.read().decode("utf-8")[:300]
        except Exception:  # noqa: BLE001
            body = ""
        return (e.code, None, "HTTP %s %s" % (e.code, body))
    except Exception as e:  # noqa: BLE001 — timeout/JSON/url/anything
        return (0, None, type(e).__name__)


def search_contact_id_by_phone(phone_digits: str,
                               http: Callable = _http) -> Tuple[bool, Optional[str]]:
    """Search for a contact by phone (with or without '+'). Returns (ok, id):
      (True, id)   -> contact found
      (True, None) -> search succeeded, DEFINITIVELY no match -> caller may create
      (False, None)-> search FAILED/transient (429/5xx/timeout) -> caller MUST NOT create
    The ok flag is the fix for the duplicate-contact bug: a failed search must never
    be mistaken for 'no match'."""
    if not phone_digits:
        return (False, None)
    plus = "+" + phone_digits
    payload = {
        "filterGroups": [
            {"filters": [{"propertyName": "phone", "operator": "EQ", "value": plus}]},
            {"filters": [{"propertyName": "phone", "operator": "EQ", "value": phone_digits}]},
        ],
        "properties": ["phone"],
        "limit": 1,
    }
    status, data, _err = http("POST", _SEARCH_PATH, payload)
    if not (200 <= status < 300) or data is None:
        return (False, None)
    results = data.get("results") or []
    return (True, results[0].get("id") if results else None)


def _patch_contact(contact_id: str, props: Dict[str, str],
                   http: Callable = _http) -> Tuple[bool, str]:
    """PATCH descriptive props onto an existing contact. On a property/enum/date
    rejection, retry with the core field only so the summary always lands. Returns
    (ok, mode) where mode 'degraded-core-only' signals a partial write."""
    money_guard(props)
    status, _data, err = http("PATCH", _CONTACTS_PATH + "/" + contact_id, {"properties": props})
    if 200 <= status < 300:
        return (True, "patched")
    core = {k: v for k, v in props.items() if k == "hermes_interaction_context"}
    if not core:
        return (False, err)
    money_guard(core)
    status2, _d2, err2 = http("PATCH", _CONTACTS_PATH + "/" + contact_id, {"properties": core})
    if 200 <= status2 < 300:
        return (True, "degraded-core-only")
    return (False, err2 or err)


def _create_contact(phone_digits: str, props: Dict[str, str],
                    http: Callable = _http) -> Tuple[Optional[str], str]:
    """Create a new contact keyed on +E.164 phone. On rejection, retry minimal.
    Returns (id_or_None, mode) where mode 'created-minimal' signals a partial write."""
    full = dict(props)
    full["phone"] = "+" + phone_digits
    money_guard(full)
    status, data, err = http("POST", _CONTACTS_PATH, {"properties": full})
    if 200 <= status < 300 and data:
        return (data.get("id"), "created")
    # Retry minimal ONLY on a 4xx VALIDATION rejection (bad enum/date/etc). A timeout
    # (status 0) or 5xx may have COMMITTED server-side; HubSpot doesn't dedupe on phone,
    # so a blind retry would create a DUPLICATE. Report 'ambiguous-failed' so the caller
    # claims the phone (sibling holds) and the next run reconciles via search.
    if status not in (400, 409, 422):
        return (None, "ambiguous-failed")
    minimal = {"phone": "+" + phone_digits}
    if props.get("hermes_interaction_context"):
        minimal["hermes_interaction_context"] = props["hermes_interaction_context"]
    money_guard(minimal)
    status2, data2, err2 = http("POST", _CONTACTS_PATH, {"properties": minimal})
    if 200 <= status2 < 300 and data2:
        return (data2.get("id"), "created-minimal")
    return (None, "ambiguous-failed" if status2 not in (400, 409, 422) else "validation-failed")


def _create_note(contact_id: str, body: str, ts_epoch: float,
                 http: Callable = _http) -> Tuple[bool, str]:
    """Create a timeline note (SUMMARY, not transcript) associated to the contact."""
    if not body or not contact_id:
        return (False, "empty")
    ms = int((ts_epoch if ts_epoch > 0 else time.time()) * 1000)
    payload = {
        "properties": {"hs_timestamp": ms, "hs_note_body": body[:65000]},
        "associations": [{
            "to": {"id": contact_id},
            "types": [{"associationCategory": "HUBSPOT_DEFINED",
                       "associationTypeId": _NOTE_TO_CONTACT_TYPE_ID}],
        }],
    }
    status, _data, err = http("POST", _NOTES_PATH, payload)
    return (200 <= status < 300, "noted" if 200 <= status < 300 else err)


# ── default exclusion check (injectable for tests) ──────────────────────────────
def _default_exclude(cid: str, resolver: Optional[Callable[[str], str]]) -> bool:
    try:
        import hermes_exclusion_guards as _eg
        return bool(_eg.is_excluded(cid, lid_resolver=resolver))
    except Exception:  # noqa: BLE001 — guard import error -> fail CLOSED (treat as excluded)
        return True


# ── orchestrator: sync ONE contact ──────────────────────────────────────────────
def sync_contact(facts: Dict,
                 resolver: Optional[Callable[[str], str]] = None,
                 *,
                 dry_run: bool = True,
                 want_note: bool = True,
                 prev: Optional[Dict] = None,
                 phone_memo: Optional[Dict[str, str]] = None,
                 http: Callable = _http,
                 search: Callable = search_contact_id_by_phone,
                 exclude: Optional[Callable[[str, Optional[Callable]], bool]] = None,
                 ) -> Dict:
    """Resolve -> match/create -> write interaction_context + props + (optional) note.

    Returns an action record (never raises). Fields:
      action: excluded | no_phone | too_short | matched | created | error
      wrote_props / wrote_note: actual writes (always False in dry_run)
      would_note: dry-run prediction of whether a note would post
      note_failed: a needed note POST failed (caller should retry / not advance status)
      degraded: a structured-prop write was rejected and fell back to core-only
    A transient/failed phone SEARCH yields action='error' and NO create (anti-dup)."""
    cid = str(facts.get("customer_id") or "").strip()
    rec = {"cid": cid, "phone": "", "action": "error", "contact_id": None,
           "wrote_props": False, "wrote_note": False, "would_note": False,
           "note_failed": False, "degraded": False, "status": "", "reason": ""}
    try:
        exclude_fn = exclude or _default_exclude
        if not cid:
            rec["action"] = "no_phone"; rec["reason"] = "blank cid"
            return rec
        if exclude_fn(cid, resolver):
            rec["action"] = "excluded"; rec["reason"] = "exclusion guard"
            return rec
        phone = normalize_phone(_cid_to_phone(cid, resolver))
        rec["phone"] = phone
        if not phone:
            # An @lid resolves to a phone via a LIVE WAHA lookup that fails OPEN to "" on
            # any transient miss/outage. Mark RETRYABLE (resolve_pending) so the cron HOLDS
            # the watermark — NOT terminal — else a WAHA blip permanently skips the contact.
            # A non-@lid with no derivable digits is genuinely terminal.
            if cid.endswith("@lid"):
                rec["action"] = "resolve_pending"
                rec["reason"] = "lid->phone unresolved this run (WAHA miss) — retry"
            else:
                rec["action"] = "no_phone"; rec["reason"] = "no derivable phone"
            return rec
        if len(phone) < 9:
            rec["action"] = "too_short"; rec["reason"] = "phone <9 digits"
            return rec

        status = derive_booking_status(facts)
        rec["status"] = status
        # In-run dedup: if an earlier cid this sweep already resolved to this phone, reuse
        # that contact id. HubSpot search is read-after-write stale, so a fresh create
        # wouldn't be found yet by a search -> a second create would DUPLICATE. Phone is
        # the identity key (two cids -> one person via the @lid/@c.us split, recycled LIDs).
        if phone_memo is not None and phone in phone_memo:
            memo_id = phone_memo[phone]
            if memo_id:                       # real id (or dry-run marker) -> reuse, PATCH
                contact_id = memo_id
                matched = True
            else:                             # sentinel: an earlier same-phone sibling's create
                rec["action"] = "error"       # was ambiguous this run -> HOLD, do NOT re-create
                rec["reason"] = "same-phone sibling create pending this run — retry next run"
                return rec
        else:
            ok, contact_id = search(phone, http)
            if not ok:
                rec["action"] = "error"
                rec["reason"] = "search failed/transient (skipped — retry next run)"
                return rec
            matched = contact_id is not None
        rec["action"] = "matched" if matched else "created"
        rec["contact_id"] = contact_id

        if dry_run:
            rec["reason"] = "dry-run"   # wrote_* stay False — dry-run writes NOTHING
            rec["would_note"] = bool(want_note and should_create_note(status, prev, time.time()))
            if phone_memo is not None:   # claim phone (truthy) so a same-phone dup counts as matched
                phone_memo[phone] = contact_id or "DRYRUN"
            return rec

        # --- real writes ---
        if matched:
            props = build_props(facts, for_create=False)
            ok_w, mode = _patch_contact(contact_id, props, http)
            rec["wrote_props"] = ok_w
            rec["degraded"] = (mode == "degraded-core-only")
            if not ok_w:
                rec["action"] = "error"
                rec["reason"] = "patch failed: " + mode
                return rec
        else:
            props = build_props(facts, for_create=True)
            contact_id, mode = _create_contact(phone, props, http)
            rec["contact_id"] = contact_id
            if not contact_id:
                rec["action"] = "error"; rec["reason"] = "create failed: " + mode
                # An ambiguous (timeout/5xx) create MAY have committed -> claim the phone
                # so a same-phone sibling this sweep HOLDS instead of creating a duplicate.
                if mode == "ambiguous-failed" and phone_memo is not None:
                    phone_memo[phone] = ""   # sentinel: reconcile via search next run
                return rec
            rec["wrote_props"] = True
            rec["degraded"] = (mode == "created-minimal")

        if phone_memo is not None and contact_id:
            phone_memo[phone] = contact_id   # claim phone so later same-phone cids reuse it

        # note (debounced on WALL-CLOCK; hs_timestamp uses the message time)
        if want_note and contact_id and should_create_note(status, prev, time.time()):
            ok_n, why_n = _create_note(
                contact_id, compose_note(facts), _epoch_of(facts.get("last_customer_message_at")), http)
            rec["wrote_note"] = ok_n
            rec["note_failed"] = not ok_n
            if not ok_n:
                rec["reason"] = (rec["reason"] + "; " if rec["reason"] else "") + "note: " + why_n
        return rec
    except Exception as e:  # noqa: BLE001 — never break the sweep
        rec["action"] = "error"
        rec["reason"] = type(e).__name__ + ": " + str(e)[:160]
        return rec


if __name__ == "__main__":
    print("hubspot_sync: enabled=%s token_present=%s" % (enabled(), bool(_token())))
