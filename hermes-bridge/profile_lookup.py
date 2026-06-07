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
import re
import unicodedata
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


# ---- gating-patch helpers (2026-06-07): tighten inject gate (Option B) +
# strip price figures. Keep the prompt carrying PREFERENCES, never prices. ----
_PLACEHOLDER_NAMES = {"unknown", "none", "null", "n/a"}


def _is_real_name(name) -> bool:
    """A usable customer name — not blank and not an 'Unknown' placeholder."""
    if not isinstance(name, str):
        return False
    s = name.strip()
    return bool(s) and s.lower() not in _PLACEHOLDER_NAMES


# concrete-preference detectors (affirmative, negation-aware): a profile injects
# only if it carries >=1 real preference the operator could not get from the
# inbound — a BOOKED yacht, a dietary restriction, a crew/beverage pref, or an
# occasion. Contact-only / all-"not mentioned" prefs match nothing => no inject.
_DIET_RE = re.compile(
    r"(?i)\b(halal|vegan|vegetarian|pescatarian|kosher|allerg\w*|gluten|"
    r"no\s+pork|no\s+beef|no\s+alcohol|nut\s+allergy|shellfish|lactose|"
    r"dairy[-\s]free|sugar[-\s]free|jain\s+food)\b")
# food-safety INCIDENTS phrased as events (not clinical allergy terms) — a recorded
# reaction/poisoning is a must-keep fact for a charter caterer. Tight enough to
# skip casual/positive mentions ("we ate prawns and loved them").
_FOODSAFE_RE = re.compile(
    r"(?i)("
    r"food\s*poisoning"
    r"|allergic\s+reaction"
    r"|bad\s+reaction\s+to|reacted\s+(?:badly\s+)?to\b"
    r"|(?:got|felt|fell|getting|feeling)\s+(?:sick|ill|unwell)\b"
    r"|made\s+(?:him|her|them|us|\w+)\s+(?:sick|ill|unwell)\b"
    r"|can(?:'?t|not)\s+eat\b"
    r"|high[-\s]risk\s+food"
    r"|\bavoid\b[^.\n]{0,20}\b(?:allerg\w*|reaction|nuts?|peanuts?|shellfish|prawns?|seafood|dairy|gluten|pork|fish)\b"
    r"|\b(?:allerg\w*|sensitiv\w*|intoleran\w*)\b[^.\n]{0,25}\bavoid\b"
    r")")
_BEV_RE = re.compile(
    r"(?i)\b(prosecco|ros[eé]|champagne|whisk(?:y|ey)|vodka|mo[eë]t|veuve|"
    r"dom\s?p[eé]rignon|hennessy|grey\s?goose|c[oô]tes\s?de\s?provence|aperol|"
    r"tequila|gin|don\s?julio|macallan|hendrick'?s?|baileys|whispering\s?angel|"
    r"red\s+wine|white\s+wine|sparkling|bottles?\s+of)\b")
_OCC_RE = re.compile(
    r"(?i)\b(birthday|anniversary|proposal|bachelor(?:ette)?|wedding|honeymoon|"
    r"engagement|graduation|corporate\s+event)\b")
_CREW_RE = re.compile(
    r"(?i)(\b(?:same|requested|prefers?|wants?|loved|praised)\s+\w{0,12}\s?(?:crew|chef)\b"
    r"|\b(?:crew|chef)\b[^.\n]{0,40}\b(?:professional|excellent|praised|perfect|"
    r"highly|impeccable|amazing|recommend\w*)\b)")
_YNAME = (r"(?:sunseeker|princess|azimut|ferretti|benetti|pershing|sanlorenzo|"
          r"mangusta|riva|numarine|gulf\s?craft|majesty|sea\s?ray|cabo|bliss|"
          r"cante|belle|asya|satoshi|novia|eyva|aquila|oryx|von\s?dutch|"
          r"lamborghini|aventura|dubriani|atlas|royalty|haigan|thunder|elise|"
          r"amore|galeon|gallant|sx\s?\d{2,3})")
_BOOK = r"(?:booked|chartered|rented|confirmed)"
# a curated yacht NAME within ~40 chars of a BOOKING word (either order), so
# "yachts of interest" enquiries and "unnamed luxury yacht" do NOT qualify.
_YACHT_BOOKED_RE = re.compile(
    r"(?i)(" + _BOOK + r"[^.\n]{0,40}\b" + _YNAME + r"\b"
    r"|\b" + _YNAME + r"\b[^.\n]{0,40}" + _BOOK + r")")


def _has_concrete_pref(prefs) -> bool:
    """True iff the prefs text carries >=1 concrete preference (booked yacht,
    dietary, crew, beverage, or occasion). Empty/contact-only prefs -> False."""
    if not isinstance(prefs, str) or not prefs.strip():
        return False
    return bool(_DIET_RE.search(prefs) or _FOODSAFE_RE.search(prefs)
                or _BEV_RE.search(prefs) or _OCC_RE.search(prefs)
                or _CREW_RE.search(prefs) or _YACHT_BOOKED_RE.search(prefs))


# strip price amounts ADJACENT to an AED/dirham token — never bare numbers, yacht
# model numbers (Sunseeker 88, SX88, Princess 74, Anna 50) or dates.
_AED_AMOUNT_RE = re.compile(
    r"(?i)(?<![a-z])(?:aed|dhs?|dirhams?|درهم)\s*\.?\s*\d[\d,]*(?:\.\d+)?"
    r"|\d[\d,]*(?:\.\d+)?\s*(?:aed|dhs?|dirhams?|درهم)(?![a-z])")


def _strip_aed(text) -> str:
    """Remove 'X,XXX AED' / 'AED X,XXX' price figures from prefs text."""
    if not isinstance(text, str) or not text:
        return text or ""
    return _AED_AMOUNT_RE.sub("[price removed]", text)


# ---- conflicting body-name suppression: the bot greets off the resolved
# (header) name; when a prefs-body "Full name/Name:" line states a GENUINELY
# different person, hide just that one line so the rendered block doesn't
# contradict itself. Variants/transliterations (Joergen/Jörgen, Michael/Mihails)
# are NOT conflicts and are left untouched. ----
_NAME_PARTICLES = {"de", "van", "von", "la", "le", "el", "al", "bin",
                   "der", "den", "da", "di", "del", "mac", "mc", "abu"}
_NAME_GENERIC = {"company", "under", "booking", "name", "client", "customer",
                 "guest", "corporate", "group", "family", "the", "llc",
                 "unknown", "stated", "provided", "given"}


def _name_norm(s) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))  # ö -> o
    return s.lower().strip()


def _name_tokens(s):
    return [t for t in re.findall(r"[a-z]+", _name_norm(s))
            if len(t) >= 3 and t not in _NAME_PARTICLES]


def _lev(a, b) -> int:
    if a == b:
        return 0
    if abs(len(a) - len(b)) > 2:
        return 9
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[-1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _jaro_winkler(a, b) -> float:
    if a == b:
        return 1.0
    la, lb = len(a), len(b)
    if not la or not lb:
        return 0.0
    win = max(max(la, lb) // 2 - 1, 0)
    fa = [False] * la
    fb = [False] * lb
    m = 0
    for i in range(la):
        for j in range(max(0, i - win), min(i + win + 1, lb)):
            if not fb[j] and a[i] == b[j]:
                fa[i] = fb[j] = True
                m += 1
                break
    if not m:
        return 0.0
    sa = [a[i] for i in range(la) if fa[i]]
    sb = [b[j] for j in range(lb) if fb[j]]
    t = sum(1 for x, y in zip(sa, sb) if x != y) / 2
    jaro = (m / la + m / lb + (m - t) / m) / 3
    pref = 0
    for x, y in zip(a, b):
        if x == y and pref < 4:
            pref += 1
        else:
            break
    return jaro + pref * 0.1 * (1 - jaro)


def _names_conflict(header, body) -> bool:
    """True only when header and body name are genuinely different people —
    not a substring, shared token, spelling variant, or transliteration."""
    h, b = _name_norm(header), _name_norm(body)
    if not h or not b or h in b or b in h:
        return False
    ht, bt = _name_tokens(header), _name_tokens(body)
    if not ht or not bt or set(ht) & set(bt):
        return False
    for x in ht:
        for y in bt:
            if _lev(x, y) <= 2 or _jaro_winkler(x, y) >= 0.80:
                return False
    return True


_NAMEFIELD_RE = re.compile(
    r"(?i)^\s*[-*|\s]*\*{0,2}(?:full name|customer name|name)\*{0,2}\s*[:|]\s*(.+)$")


def _drop_conflicting_name(prefs, header) -> str:
    """Remove a body 'Full name/Name:' line ONLY when its name genuinely
    conflicts with the header name. Every other line — and every non-conflicting
    profile — is returned byte-for-byte unchanged."""
    if not isinstance(prefs, str) or not prefs or not _is_real_name(header):
        return prefs if isinstance(prefs, str) else ""
    out = []
    for line in prefs.split("\n"):
        m = _NAMEFIELD_RE.match(line)
        if m:
            val = re.split(r"\|", m.group(1))[0]
            val = re.sub(r"\[[^\]]*\]|\([^)]*\)", "", val)
            val = re.sub(r"\b(?:FACT|INFERENCE|surname unknown|last name unknown)\b",
                         "", val, flags=re.I).strip(" *:|-\t")
            toks = _name_tokens(val)
            looks_like_name = (val and not any(ch.isdigit() for ch in val)
                               and toks and any(t not in _NAME_GENERIC for t in toks))
            if looks_like_name and _names_conflict(header, val):
                continue  # suppress the conflicting body name line
        out.append(line)
    return "\n".join(out)


def profile_block(cid: str,
                  phone_resolver: Optional[Callable[[str], str]] = None) -> str:
    """Render the PROFILE context block for the draft prompt, or "".

    "" when: the feature is disabled, no profile is found, the customer is not a
    known/returning client, or any error occurs (fail-safe). Injects facts and
    preferences only — never internal revenue figures."""
    try:
        if not enabled():
            return ""
        # Layer-3 exclusion guard (shared block-list): never enrich a draft for a
        # staff/crew/agent number that must not be treated as a customer — same
        # block-list the nudge path honours. Fail-closed: is_excluded() returns
        # True on any guard error, and any import/lookup error falls through to
        # the outer handler -> "" (no injection). New leads stay unchanged.
        import hermes_exclusion_guards as _eg
        if _eg.is_excluded(cid, lid_resolver=phone_resolver):
            return ""
        p = lookup(cid, phone_resolver)
        if not p:
            return ""
        if not p.get("returning"):
            return ""  # only enrich known/returning clients; new leads unchanged
        name = p.get("name")
        if not _is_real_name(name):
            return ""  # gate: skip blank / "Unknown" placeholder names
        prefs = (p.get("profile_prefs") or "").strip()
        if not _has_concrete_pref(prefs):
            return ""  # gate: skip hollow / contact-only / all-"not mentioned"
        prefs = _strip_aed(prefs)  # strip price figures before rendering
        prefs = _drop_conflicting_name(prefs, name)  # hide a contradicting body name
        country = p.get("country") or "?"
        nb = p.get("n_booking_blocks") or 0
        lines = [
            "--- CUSTOMER PROFILE (internal context — RETURNING / known client) ---",
            f"Name: {name}  |  Country: {country}  |  Past bookings on record: {nb}",
            "Acknowledge the relationship; don't re-ask basics already known.",
            "Known preferences & history (from prior conversations):",
            prefs,
        ]
        lines.append("(Context only. Never quote internal figures, and never "
                     "state a fact you cannot see in THIS conversation unless it "
                     "is listed above.)")
        return "\n".join(lines)
    except Exception:  # noqa: BLE001 — must never break the draft path
        return ""


if __name__ == "__main__":
    print(f"profile_lookup: index loaded={load_ok()} count={loaded_count()} "
          f"enabled={enabled()}")
