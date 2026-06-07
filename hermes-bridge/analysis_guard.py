"""Deterministic guardrail over the (LLM) Hermes lead-analysis.

The analyzer's output (label / importance_score / reasoning) is trusted blindly
today and drives labels, sort, and the /review display — so its errors propagate
everywhere. This PURE layer encodes the analyzer's known failure modes and either
CORRECTS the result or FLAGS it as unreliable, so a wrong analysis can't quietly
mislabel a customer, mis-sort the queue, or hide a real booking. 2026-06-07.

Pure (no I/O) → unit-tested in test_analysis_guard.py and safe to call from both
the analyze handler (store time) and the /review render (display time).
"""
import re

from labels import _close_bucket  # vendor-vs-lost reasoning classifier (reused)

# --- scam / fraud (Mike: crypto scam was scored LOST) -----------------------
_SCAM_RE = re.compile(
    r"\b(scam|fraud|phish\w*|crypto\w*|bitcoin|\bbtc\b|usdt|forex|"
    r"investment\s+(scheme|opportunity|fund)|airdrop|\bnft\b|"
    r"romance\s+scam|advance[\s-]+fee|impersonat\w*|money\s+launder\w*|"
    r"wallet\s+(address|recovery)|trading\s+(signal|bot|group))\b",
    re.IGNORECASE)


def is_scam(text):
    """True if the analyzer's reasoning describes a scam/fraud (crypto, romance,
    advance-fee, phishing). Pure."""
    return bool(_SCAM_RE.search(text or ""))


def reclassify_close_label(reasoning):
    """Authoritative terminal label for a 'close' verdict, in priority order:
    SCAM (crypto/fraud) > DISREGARDED (vendor/spam/wrong-number/completed) >
    LOST (a real customer who didn't convert — price/competitor/timing/ghost,
    AND the unclear default, so a real customer is NEVER branded 'not a
    customer'). Supersedes labels._close_label_for by adding the SCAM route.
    Pure."""
    r = (reasoning or "").strip()
    if is_scam(r):
        return "SCAM"
    bkt, _ = _close_bucket(r)
    return "DISREGARDED" if bkt in ("NOT_A_CUSTOMER", "COMPLETED") else "LOST"


# --- degraded input: the analyzer ran on missing/empty history --------------
_EMPTY_HISTORY_RE = re.compile(
    r"\b(first\s+contact|no\s+(prior\s+)?(messages?|conversation|history|reply|"
    r"replies)|zero\s+conversation|garbled\s+reply|never\s+(replied|responded|"
    r"messaged)|one\s+(garbled\s+)?(message|reply))\b", re.IGNORECASE)


def is_analysis_unreliable(msg_count, history_len, reasoning):
    """The analysis can't be trusted because it ran on missing history. True
    when the analyzer claims 'first contact / no messages' yet the lead has real
    message history, OR the fetched history was ~empty while the lead is
    substantial. (Émilie: 157 msgs but 'first contact'; the analyzer scored a
    paid CONFIRMED booking 0/100 'date passed'.) Pure."""
    try:
        mc = int(msg_count or 0)
    except (TypeError, ValueError):
        mc = 0
    try:
        hl = int(history_len or 0)
    except (TypeError, ValueError):
        hl = 0
    if mc >= 4 and bool(_EMPTY_HISTORY_RE.search(reasoning or "")):
        return True
    if mc >= 6 and hl < 40:
        return True
    return False


# --- stale relative date (Émilie: 'tomorrow 4-7PM' frozen 10 days ago) -------
_REL_DATE_RE = re.compile(
    r"\b(today|tonight|tomorrow|tmrw|this\s+(week|weekend|evening|morning|"
    r"afternoon|night)|next\s+(week|weekend)|"
    r"(mon|tues?|wed(nes)?|thurs?|fri|satur?|sun)(day)?)\b", re.IGNORECASE)
_ABS_DATE_RE = re.compile(
    r"\b(20\d\d"                                              # a year
    r"|(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2}"  # Jun 11
    r"|\d{1,2}\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)"           # 11 Jun
    r"|\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\b",                     # 11/06/2026
    re.IGNORECASE)


def is_stale_relative_date(dates_str, age_days):
    """A stored RELATIVE date ('tomorrow' / 'this weekend' / 'friday') frozen
    days ago is meaningless — the analyzer reads it as a past date and says
    'opportunity gone'. True when the dates field is relative (and not also an
    absolute date) AND it was set >= ~1 day ago. Pure."""
    d = dates_str or ""
    if not _REL_DATE_RE.search(d) or _ABS_DATE_RE.search(d):
        return False
    try:
        return float(age_days or 0) >= 1.0
    except (TypeError, ValueError):
        return False
