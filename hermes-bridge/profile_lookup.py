#!/usr/bin/env python3
"""Profile-lookup (Layer 1) — inject returning-customer context into drafts.

Loads the deduped, gateway-anchored customer profile index (built off the
extraction files) and renders a compact, operator-facing PROFILE block for the
draft prompt. Keyed by the SAME `server._normalize_phone` standard used by the
exclusion guard and the revenue index, so a customer_id ('<digits>@c.us' or
'<hash>@lid') resolves to the same key everywhere.

SAFETY:
  - Gated by env PROFILE_LOOKUP_ENABLED (default OFF). When off, profile_block()
    returns "" and the draft prompt is byte-for-byte unchanged.
  - FAIL-SAFE: every public call is wrapped so it can NEVER raise into the draft
    path. Missing index / missing profile / any error -> "" (draft proceeds).
  - Injects FACTS/preferences only, never internal revenue figures.

Data: customer_profile_index.json sits next to this module (Path(__file__).parent),
never a cwd-relative path. ~4 MB, loaded once at import.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Callable, Dict, Optional

_DATA_PATH = Path(__file__).resolve().parent / "customer_profile_index.json"


def normalize_phone(s: Optional[str]) -> str:
    """Bare-digit normalize, mirroring server._normalize_phone (drop leading
    00; UAE-local 0xxxxxxxxx -> 971..., 5xxxxxxxx -> 9715...)."""
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


def _load() -> Dict:
    try:
        with open(_DATA_PATH, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001 — missing/broken index must not crash import
        return {}


_INDEX: Optional[Dict] = None  # lazy: only load the ~4 MB index when first used


def _index() -> Dict:
    """Cached lazy load — keeps `import profile_lookup` cheap when the feature
    is OFF (the index is only read once the first profile_block is requested)."""
    global _INDEX
    if _INDEX is None:
        _INDEX = _load()
    return _INDEX


def enabled() -> bool:
    return os.environ.get("PROFILE_LOOKUP_ENABLED", "").strip().lower() in (
        "1", "true", "yes", "on")


def load_ok() -> bool:
    return bool(_index())


def loaded_count() -> int:
    return len(_index())


def _cid_to_phone(cid: str, resolver: Optional[Callable[[str], str]]) -> str:
    s = (cid or "").strip()
    if not s:
        return ""
    if s.endswith("@lid"):
        return (resolver(s) or "") if resolver else ""   # @lid is a hash, resolve via WAHA
    if s.endswith("@c.us"):
        return s.split("@", 1)[0]
    return s


def lookup(cid: str, phone_resolver: Optional[Callable[[str], str]] = None) -> Optional[Dict]:
    """Return the profile dict for a customer_id, or None. Never raises."""
    try:
        phone = _cid_to_phone(cid, phone_resolver)
        key = normalize_phone(phone)
        if not key:
            return None
        return _index().get(key)
    except Exception:  # noqa: BLE001
        return None


def profile_block(cid: str,
                  phone_resolver: Optional[Callable[[str], str]] = None) -> str:
    """Render the PROFILE context block for the draft prompt, or "".

    "" when: the feature is disabled, no profile is found, the customer is not a
    known/returning client, or any error occurs (fail-safe). Injects facts and
    preferences only — never internal revenue figures."""
    try:
        if not enabled():
            return ""
        p = lookup(cid, phone_resolver)
        if not p:
            return ""
        if not p.get("returning"):
            return ""  # only enrich known/returning clients; new leads unchanged
        name = p.get("name") or "this customer"
        country = p.get("country") or "?"
        nb = p.get("n_booking_blocks") or 0
        prefs = (p.get("profile_prefs") or "").strip()
        if not name and not prefs:
            return ""
        lines = [
            "--- CUSTOMER PROFILE (internal context — RETURNING / known client) ---",
            f"Name: {name}  |  Country: {country}  |  Past bookings on record: {nb}",
            "Acknowledge the relationship; don't re-ask basics already known.",
        ]
        if prefs:
            lines.append("Known preferences & history (from prior conversations):")
            lines.append(prefs)
        lines.append("(Context only. Never quote internal figures, and never "
                     "state a fact you cannot see in THIS conversation unless it "
                     "is listed above.)")
        return "\n".join(lines)
    except Exception:  # noqa: BLE001 — must never break the draft path
        return ""


if __name__ == "__main__":
    print(f"profile_lookup: index loaded={load_ok()} count={loaded_count()} "
          f"enabled={enabled()}")
