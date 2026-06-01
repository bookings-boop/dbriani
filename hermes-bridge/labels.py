#!/usr/bin/env python3
"""labels.py — label vocabulary, signal regexes, and confidence-dampening
constants.

The PURE pieces of the pipeline-review labeling system live here:
  - LABELS frozenset (what state a customer can be in)
  - _LABEL_RANK ordering (sticky-upward guard)
  - _HARD_DEMOTE_SIGNALS (signals strong enough to demote regardless
    of confidence dampening)
  - The chat-signal regexes (MONEY_RE / LETS_DO_IT_RE / etc.) used by
    the auto-classifier
  - Confidence dampening tuning constants
  - _parse_booking_date for past-date detection

DB-touching helpers (get_correction_count, _has_recent_payment_*,
apply_label_transition) stay in server.py until a follow-up phase —
their wide call graph makes moving them higher-blast-radius."""
import re


# ---------------------------------------------------------------------------
# Label vocabulary
# ---------------------------------------------------------------------------

LABELS = frozenset({
    "NEW", "WARM", "HOT", "NEEDS_ATTENTION", "COLD",
    "WAITING_FOR_PAYMENT", "CONFIRMED",
    "PAUSED_SPAM", "PAUSED_B2B", "PAUSED_PERSONAL",
    # DISREGARDED — operator (or Hermes via [🛑 Disregard]) closed the
    # lead as unconvertible. Hidden from /review entirely; not in
    # pause_tail. Reopened only via explicit `/label <name> WARM`.
    "DISREGARDED",
})

# Label-priority ranking — higher = higher operator priority. Used by
# the sticky-upward guard in _label_eval to prevent weak signals from
# demoting a strong state on a single message. PAUSED_* labels are
# intentionally OMITTED — they're score-floored (-10000) by score_lead,
# not promotion-ranked.
_LABEL_RANK = {
    "NEW": 0, "COLD": 1, "WARM": 2, "HOT": 3,
    "NEEDS_ATTENTION": 4, "WAITING_FOR_PAYMENT": 5, "CONFIRMED": 6,
    # DISREGARDED ranks ABOVE CONFIRMED so the auto-classifier's
    # sticky-upward guard treats it as terminal — a stray HOT/WARM
    # signal won't bounce a disregarded lead back into /review.
    # Operator must explicitly /label them to reopen.
    "DISREGARDED": 7,
}

# Signals strong enough to demote regardless of confidence dampening.
# Past-date / cold-decay / payment-confirmed / lock are deterministic;
# the rest are LLM/regex heuristics that shouldn't pull a HOT down
# without strong agreement. Operator's manual /label always overrides
# via a different code path (apply_label_transition reason='manual:*').
_HARD_DEMOTE_SIGNALS = frozenset({
    "date_passed", "cold_decay", "confirmed_terminal", "locked",
})

# Analyzer signal for a lead whose booking date has already passed. A follow-up
# draft to such a lead should be a GENTLE re-engage check-in ("hope it worked
# out — welcome back for a future date"), NOT a sales nudge (#5, 2026-06-01).
# cold_decay is dormant and confirmed_terminal is a completed booking (→ #6
# feedback) — only date_passed re-engages.
REENGAGE_SIGNAL = "date_passed"


def _is_reengage_followup(last_analysis_signal):
    """True when a follow-up draft should be a passed-date re-engage check-in
    rather than a nudge/reply. Pure; tolerant of None/whitespace."""
    return (last_analysis_signal or "").strip() == REENGAGE_SIGNAL


# Tier demotion when confidence < CONFIDENCE_DEMOTE_THRESHOLD.
_TIER_BELOW = {"HOT": "WARM", "WARM": "NEW", "NEW": "NEW", "COLD": "COLD"}


# ---------------------------------------------------------------------------
# Chat-signal regexes (auto-classifier)
# ---------------------------------------------------------------------------

MONEY_RE = re.compile(
    r"\b(AED|aed|price|budget|cost|how\s*much|cheap|expensive)\b"
    r"|\$\d|\b\d{4,}\b",
    re.IGNORECASE,
)

LETS_DO_IT_RE = re.compile(
    # Verified commit/finalize phrases. Detects when a customer is past
    # negotiation and ready to pay. Each alternation is anchored on a
    # commit verb so it doesn't false-positive on generic chat.
    r"(let'?s\s+(do\s+it|book|lock|go\s+ahead|proceed)|"
    r"i'?ll\s+take\s+it|i\s+want\s+to\s+(book|lock|take)|"
    r"i'?m\s+(in|ready)|ready\s+to\s+(book|pay|lock)|"
    r"sounds\s+(good|great)[,\s]+book|"
    r"(please\s+)?book\s+(it|the|this|that|now|a|my|us|me|asap)"
    r"(?!\s*(call|phone|meeting|appointment|table|slot\s+to\s+call))|"
    r"lock\s+it\s+in|"
    r"send\s+(me\s+)?the?\s+(payment\s+)?link|"
    r"how\s+(do|can|should)\s+i\s+pay|"
    r"go\s+ahead\s+(with|and\s+book)|"
    r"yes\s+(book|let'?s|please)|"
    r"ok\s+(let'?s|book|go\s+ahead)|"
    r"alright\s+(let'?s|book|go\s+ahead))",
    re.IGNORECASE,
)

# Past-date detection — used to demote HOT customers whose booking date
# has passed. Matches month + day-of-month patterns in the dates field.
PAST_DATE_MONTH_RE = re.compile(
    r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|"
    r"nov(?:ember)?|dec(?:ember)?)\s+(\d{1,2})(?:\s*,?\s*(\d{4}))?",
    re.IGNORECASE,
)

# Payment-confirmation language from the customer. ONLY trips CONFIRMED
# when paired with a recent payment_link_sent in autonomous_sends (last
# 48h) — see _has_recent_payment_link_sent + compute_label. Anchored on
# explicit commit verbs so a generic 'done' / 'paid' in casual chat
# (e.g. 'i paid for parking') doesn't false-positive without context.
PAYMENT_CONFIRMED_RE = re.compile(
    r"\b("
    r"i'?ve?\s+(paid|sent|transferred|done\s+(it|the\s+payment))|"
    r"(payment|paid)\s+(done|sent|made|complete|successful)|"
    r"(transferred|sent)\s+(it|the\s+(payment|amount|aed|money))|"
    r"made\s+the?\s+payment|just\s+paid|now\s+paid|"
    r"transaction\s+(successful|complete|done)|"
    r"all\s+(paid|done|settled)|"
    r"settled\s+(it|the\s+(payment|invoice))|"
    r"payment\s+✅|paid\s+✅|done\s+✅"
    r")\b",
    re.IGNORECASE,
)

SAME_DAY_RE = re.compile(
    r"\b(today|tonight|right\s*now|now|asap|immediately|"
    r"this\s+(afternoon|evening|night))\b",
    re.IGNORECASE,
)

PRICING_INQUIRED_RE = re.compile(
    r"\b(price|cost|how\s*much|rate|rates|charge|fee)\b",
    re.IGNORECASE,
)

YACHT_KEYWORD_RE = re.compile(
    r"\b(yacht|boat|satoshi|pershing|sunseeker|thunder|catamaran|"
    r"cruise|charter)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Confidence dampening — see docs/pipeline-review-plan.md §2a step 4
# ---------------------------------------------------------------------------

CORRECTION_WINDOW_DAYS = 90
CORRECTION_DAMPENING_DIVISOR = 5.0
CONFIDENCE_FLOOR = 0.2
CONFIDENCE_DEMOTE_THRESHOLD = 0.4


# ---------------------------------------------------------------------------
# Date parsing (PAST_DATE_MONTH_RE consumer)
# ---------------------------------------------------------------------------

_MONTH_NUM = {
    "jan": 1, "january": 1, "feb": 2, "february": 2,
    "mar": 3, "march": 3, "apr": 4, "april": 4, "may": 5,
    "jun": 6, "june": 6, "jul": 7, "july": 7, "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9, "oct": 10, "october": 10,
    "nov": 11, "november": 11, "dec": 12, "december": 12,
}


def _parse_booking_date(dates_str):
    """Best-effort parse of customer_facts.dates into a date object.
    Returns datetime.date or None. Handles 'Sat May 23', 'Mon Jun 1',
    'Thu Jan 28 2027', etc. Year defaults to nearest future year (so
    'May 23' in late May means this year; in December means next year).
    """
    import datetime as _dt
    if not dates_str:
        return None
    m = PAST_DATE_MONTH_RE.search(dates_str)
    if not m:
        return None
    month = _MONTH_NUM.get(m.group(1).lower()[:3])
    if not month:
        return None
    try:
        day = int(m.group(2))
    except (TypeError, ValueError):
        return None
    year_grp = m.group(3)
    today = _dt.date.today()
    if year_grp:
        try:
            year = int(year_grp)
        except ValueError:
            year = today.year
    else:
        # No year given — pick the nearest sensible year. If month+day
        # is already past in current year by >60 days, assume next
        # year. Else current year (covers cases like 'May 23' on May 24
        # where the date IS in the past).
        try:
            cand = _dt.date(today.year, month, day)
        except ValueError:
            return None
        days_past = (today - cand).days
        if days_past > 60:
            year = today.year + 1
        else:
            year = today.year
    try:
        return _dt.date(year, month, day)
    except ValueError:
        return None
