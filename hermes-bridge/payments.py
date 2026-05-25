#!/usr/bin/env python3
"""payments.py — Nomod payment-link integration.

Wraps the small subset of Nomod's REST API we use:
  - POST /v1/links     — create a payment link
  - GET  /v1/charges   — poll for completed payments

Nomod doesn't issue webhooks, so the /poll-payments cron sweeps
/charges every 2 min and dedups via Redis. This module is the
transport layer + a payer-vs-customer mismatch heuristic; the
sweep loop + matching logic lives upstream in server.py."""
import json
import os
import urllib.error
import urllib.request

from util import _envflag


# --- credentials + feature flag --------------------------------------
NOMOD_API_KEY = os.environ.get("NOMOD_API_KEY", "")
# Webhook signing secret (svix HMAC-SHA256). Format whsec_<base64>.
# Operator pastes the value from Nomod dashboard after creating the
# webhook. Empty string means webhook endpoint will return 500
# ("secret not configured") on every signed request — bridge advertises
# the endpoint but refuses to process events.
NOMOD_WEBHOOK_SECRET = os.environ.get("NOMOD_WEBHOOK_SECRET", "")
NOMOD_API_BASE = os.environ.get(
    "NOMOD_API_BASE", "https://api.nomod.com/v1").rstrip("/")
PAYMENTS_ENABLED = _envflag("PAYMENTS_ENABLED", "true")

# Nomod's Cloudflare WAF 403s the default Python-urllib UA. Custom UA
# matches what we registered with their support; do NOT change it
# casually.
_NOMOD_UA = "DubrianiHermesBridge/1.0"


# --- payer matching --------------------------------------------------

def _normalize_phone_digits(s):
    """Strip everything except digits. Used to compare
    charge.customer_info phone numbers against customer_facts
    customer_ids (which embed digits before the @c.us or as the suffix
    of @lid pushNames)."""
    return "".join(ch for ch in (s or "") if ch.isdigit())


def _payer_mismatch(charge_payer, customer_facts_row, waha_chats=None):
    """True if the charge.customer_info doesn't look like the same
    person we sent the link to. Compares payer phone digit suffix
    against:
      1. customer_id digits (catches @c.us-shape ids — phone IS the id)
      2. customer_facts.name digits (catches phone-string pushName
         fallback)
      3. WAHA chat-list pushName digits (catches @lid hashed ids —
         only WAHA knows the real phone)
    Returns False (no mismatch) when we don't have a phone for the
    customer to compare against — we can't verify, so don't false-flag.
    Operator wants this surfaced when payer ≠ customer so CRM can
    link the two; false positives are worse than false negatives
    here."""
    if not customer_facts_row:
        return False
    payer_phone = _normalize_phone_digits(
        (charge_payer or {}).get("phone_number"))
    if not payer_phone:
        return False
    tail = payer_phone[-9:] if len(payer_phone) >= 9 else payer_phone
    if not tail:
        return False
    # Layer A: @c.us-shape customer_id contains the phone literally.
    cust_id_digits = _normalize_phone_digits(
        (customer_facts_row.get("customer_id") or "").split("@")[0])
    if cust_id_digits and len(cust_id_digits) >= 7 \
            and cust_id_digits.endswith(tail):
        return False
    # Layer B: customer_facts.name is a phone-string fallback (e.g.
    # WAHA pushName before backfill — '+971 55 563 3317').
    cust_name_digits = _normalize_phone_digits(
        customer_facts_row.get("name") or "")
    if cust_name_digits and len(cust_name_digits) >= 7 \
            and cust_name_digits.endswith(tail):
        return False
    # Layer C: WAHA chat pushName — the only source for @lid hashed
    # customer_ids that have a human-name in customer_facts.
    cid = customer_facts_row.get("customer_id") or ""
    if waha_chats and cid:
        for ch in waha_chats:
            cid_str = ch.get("_serialized") or (
                ch.get("id") or {}).get("_serialized") or ""
            if cid_str == cid:
                pn_digits = _normalize_phone_digits(ch.get("name") or "")
                if pn_digits and len(pn_digits) >= 7 \
                        and pn_digits.endswith(tail):
                    return False
                # WAHA had the customer but their pushName isn't a
                # phone we can match against — can't determine
                # mismatch reliably.
                if not pn_digits:
                    return False
                break  # WAHA chat found but phone didn't match
    # No comparable phone found anywhere — can't verify, default to
    # no false-flag.
    if not (cust_id_digits or cust_name_digits or waha_chats):
        return False
    # We had data to compare AND nothing matched — true mismatch.
    return True


# --- Nomod REST -----------------------------------------------------

def nomod_list_recent_charges(page_size=50):
    """GET /v1/charges — Nomod's payment feed. Returns
    (list_of_charges_or_None, err_string_or_None). Always non-raising.
    Used by /poll-payments cron to detect customer payments."""
    if not NOMOD_API_KEY:
        return None, "NOMOD_API_KEY not configured on the bridge"
    req = urllib.request.Request(
        NOMOD_API_BASE + f"/charges?page_size={int(page_size)}",
        headers={"X-API-KEY": NOMOD_API_KEY, "User-Agent": _NOMOD_UA})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            resp = json.loads(r.read().decode("utf-8"))
        results = resp.get("results")
        if not isinstance(results, list):
            return None, "Nomod /charges response had no results array"
        return results, None
    except urllib.error.HTTPError as e:
        return None, f"Nomod HTTP {e.code}"
    except Exception as e:
        return None, f"Nomod fetch failed: {e!r}"


def nomod_create_link(amount, summary, customer_name):
    """Create a Nomod payment link. Returns (link_url, link_id, err).

    Currency is hard-coded AED — never taken from the LLM. No
    expiry_date (Nomod default). The booking summary is the single
    line item + the note; customer_name goes in the link title."""
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
                 "User-Agent": _NOMOD_UA})
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
