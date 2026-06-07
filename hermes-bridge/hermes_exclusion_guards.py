#!/usr/bin/env python3
"""Exclusion guards — Layer 3 (safety-only) of the customer-data pipeline.

A pure BLOCK-LIST that suppresses PROACTIVE / marketing outreach (nudges,
follow-ups, re-engagement) to people who must never be cold-pitched:

  - internal_staff          Dubriani team / supervisors
  - external_crew_vendors   captains, crew, suppliers
  - external_agents         B2B partners / agents (incl. no-history pending)

This is a block-list, NOT a customer lookup — it can only *suppress* an
outreach draft, never select or mix up a customer. It does NOT block inbound
handling or normal approval-mode replies: a staff member who messages us still
gets a normal reply. Only proactive outreach TO them is blocked.

Phone identity here comes in three shapes; the guard handles all:
  - '+<digits>' / '<digits>' / spaced / '00<cc>...'  -> normalize, match
  - '<digits>@c.us'  -> the digits ARE the phone     -> normalize, match
  - '<hash>@lid'     -> a privacy HASH, NOT a phone   -> match only after a
                        resolver (waha.phone_for_cid) maps it to a real phone

normalize_phone() is THE single normalizer, used on BOTH the stored list and
the incoming identity. It mirrors server._normalize_phone so the guard agrees
with the rest of the bridge.

FAIL-CLOSED: any internal error, or data that failed to load, makes
is_excluded() return True (block outreach). Erring toward NOT sending is the
safe direction for a suppression list.

Data: exclusion_phones.json sits next to this module and is loaded by absolute
path (Path(__file__).parent) — never a cwd-relative or /tmp path.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional

_DATA_PATH = Path(__file__).resolve().parent / "exclusion_phones.json"

# Categories that constitute a HARD block. Anything else in the data file is
# ignored (defence against an over-broad data file sweeping in real customers).
_HARD_CATEGORIES = (
    "internal_staff",
    "external_crew_vendors",
    "external_agents",
)


def normalize_phone(s: Optional[str]) -> str:
    """Normalize a phone string to bare digits, mirroring
    server._normalize_phone: keep digits only, drop a leading international
    '00' prefix, and lift UAE national formats to a 971 country code
    (0xxxxxxxxx -> 971xxxxxxxxx; 5xxxxxxxx -> 9715xxxxxxxx). Returns '' when
    there are no digits. Used on BOTH sides of the membership test."""
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


class ExclusionGuard:
    """Holds the normalized block-set and answers is_excluded()."""

    def __init__(self, phones_by_category: Dict[str, List[str]],
                 ok: bool = True):
        self._ok = ok
        self.normset: set = set()
        self.counts: Dict[str, int] = {}
        # normalized phone -> category (first category wins for reporting)
        self._cat: Dict[str, str] = {}
        for category, phones in (phones_by_category or {}).items():
            if category not in _HARD_CATEGORIES:
                continue
            seen = 0
            for raw in (phones or []):
                norm = normalize_phone(raw)
                if not norm:
                    continue
                if norm not in self.normset:
                    self.normset.add(norm)
                    self._cat.setdefault(norm, category)
                    seen += 1
            self.counts[category] = seen
        self.total = len(self.normset)

    @classmethod
    def failed(cls) -> "ExclusionGuard":
        """A guard whose data did NOT load. is_excluded() blocks everything
        (fail-closed) so a missing/broken data file can never silently let
        cold-pitches reach staff."""
        return cls({}, ok=False)

    def _identity_to_phone(self, identity: str,
                           lid_resolver: Optional[Callable[[str], str]]) -> str:
        """Derive a phone string from any identity shape. '' when no phone is
        derivable (e.g. an unresolvable @lid hash — which is NOT an error)."""
        s = (identity or "").strip()
        if not s:
            return ""
        if s.endswith("@lid"):
            # An @lid is a privacy HASH, not a phone. Only the resolver
            # (waha.phone_for_cid) can map it to a real number. May raise —
            # the caller's try/except turns that into a fail-closed block.
            if lid_resolver is not None:
                return lid_resolver(s) or ""
            return ""
        if s.endswith("@c.us"):
            return s.split("@", 1)[0]
        return s

    def is_excluded(self, identity: str,
                    lid_resolver: Optional[Callable[[str], str]] = None) -> bool:
        """True if proactive outreach to `identity` must be BLOCKED.

        identity may be a phone string or a WhatsApp customer_id
        ('<digits>@c.us' / '<hash>@lid'). lid_resolver maps an @lid to a
        '+<digits>' phone (pass waha.phone_for_cid in production).

        FAIL-CLOSED: returns True on any internal error or unloaded data.
        Returns False (allow) only when we positively determine the phone is
        not on the block-list, or when we genuinely cannot derive a phone
        (an unresolvable @lid — most of which are ordinary customers)."""
        try:
            if not self._ok:
                return True  # data not loaded -> block (safe direction)
            phone = self._identity_to_phone(identity, lid_resolver)
            if not phone:
                return False  # can't confirm; allow (not an error)
            return normalize_phone(phone) in self.normset
        except Exception:  # noqa: BLE001 — any failure must block, not send
            return True

    def category_of(self, identity: str,
                    lid_resolver: Optional[Callable[[str], str]] = None
                    ) -> Optional[str]:
        """The block category for an identity, or None if not excluded.
        Returns '__load_failed__' when the data did not load."""
        try:
            if not self._ok:
                return "__load_failed__"
            phone = self._identity_to_phone(identity, lid_resolver)
            if not phone:
                return None
            return self._cat.get(normalize_phone(phone))
        except Exception:  # noqa: BLE001
            return "__load_failed__"


def _load(path: Path = _DATA_PATH) -> ExclusionGuard:
    """Build the singleton guard from exclusion_phones.json. On ANY failure
    return a failed() guard (fail-closed)."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        cats = data.get("categories") if isinstance(data, dict) else None
        if cats is None and isinstance(data, dict):
            # tolerate a flat {category: [...]} shape too
            cats = {k: v for k, v in data.items()
                    if isinstance(v, list)}
        guard = ExclusionGuard(cats or {})
        if guard.total == 0:
            print("⚠ exclusion guards: data file loaded but 0 phones — "
                  "FAILING CLOSED (blocking proactive outreach)",
                  file=sys.stderr)
            return ExclusionGuard.failed()
        return guard
    except FileNotFoundError:
        print(f"⚠ exclusion guards: {path} not found — FAILING CLOSED "
              "(blocking proactive outreach)", file=sys.stderr)
        return ExclusionGuard.failed()
    except Exception as e:  # noqa: BLE001
        print(f"⚠ exclusion guards: load error {e!r} — FAILING CLOSED",
              file=sys.stderr)
        return ExclusionGuard.failed()


# Module singleton, built once at import.
_GUARD: ExclusionGuard = _load()


def is_excluded(identity: str,
                lid_resolver: Optional[Callable[[str], str]] = None) -> bool:
    """Module-level convenience over the singleton (fail-closed)."""
    return _GUARD.is_excluded(identity, lid_resolver)


def category_of(identity: str,
                lid_resolver: Optional[Callable[[str], str]] = None
                ) -> Optional[str]:
    return _GUARD.category_of(identity, lid_resolver)


def loaded_count() -> int:
    return _GUARD.total


def load_ok() -> bool:
    return _GUARD._ok


def category_counts() -> Dict[str, int]:
    return dict(_GUARD.counts)


if __name__ == "__main__":
    print("Exclusion guards:",
          "LOADED" if load_ok() else "FAILED-CLOSED",
          f"| {loaded_count()} phones | {category_counts()}")
