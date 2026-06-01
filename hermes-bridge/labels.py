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


# Labels a passed-date lead can be auto-closed FROM. DISREGARDED/CONFIRMED/
# PAUSED_* are already terminal / won / held → never re-close them.
_DORMANCY_FROM_OK = frozenset({"NEW", "WARM", "HOT", "NEEDS_ATTENTION", "COLD"})


def _is_dormancy_eligible(label, last_analysis_signal, reengage_attempts,
                          silent_days, min_attempts=2, min_silent_days=7):
    """#5-auto graceful-close gate (2026-06-01). True iff a passed-date lead
    has had >= min_attempts re-engage drafts AND >= min_silent_days of
    customer silence, and is still in an active (non-terminal) label. Pure;
    None-safe. With 0 attempts it is NEVER eligible (the operator's
    'never silent-disregard' rule)."""
    if (last_analysis_signal or "").strip() != REENGAGE_SIGNAL:
        return False
    if label not in _DORMANCY_FROM_OK:
        return False
    if (not isinstance(reengage_attempts, (int, float))
            or reengage_attempts < min_attempts):
        return False
    if (not isinstance(silent_days, (int, float))
            or silent_days < min_silent_days):
        return False
    return True


def _is_feedback_due(trip_date, today, window_days=3):
    """#6-auto-A: True iff a CONFIRMED booking's trip date has JUST passed —
    ended between 1 and window_days days before `today` (the window forgives
    missed cron days; a per-lead Redis dedup guarantees one card per booking).
    Same-day/future trips aren't due. Pure date math; None-safe."""
    if trip_date is None or today is None:
        return False
    delta = (today - trip_date).days
    return 1 <= delta <= window_days


def _completed_card_label(trip_passed, feedback_asked):
    """#6-auto-B: draft-button verb for a CONFIRMED booking card. Upcoming →
    logistics/upsell ('Draft message'); completed + no feedback sent yet →
    'Draft feedback check-in'; completed + feedback already sent → 'Ask for
    review' (operator taps it after a positive reply). Pure."""
    if not trip_passed:
        return "💬 Draft message"
    if feedback_asked:
        return "⭐ Ask for review"
    return "💬 Draft feedback check-in"


def _review_ask_directive(review_url):
    """#6-auto-B: directive for a Google-review request after a positive
    post-trip reply. Warm + low-pressure; includes the review link when set.
    Pure; safe with no/blank url (no broken link)."""
    url = (review_url or "").strip()
    link = (f" Include this exact link at the end: {url}" if url else "")
    return (
        "This customer had a COMPLETED trip and just replied POSITIVELY. Draft "
        "a short, warm message that genuinely THANKS them for the kind words "
        "and gently asks if they'd mind leaving a quick Google review — no "
        "pressure, only because they enjoyed it." + link
        + " One short, sincere message — do not pitch anything else.")


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


# ---------------------------------------------------------------------------
# R4 — no-draft fallback (2026-06-02)
# ---------------------------------------------------------------------------
# handle_draft_followup used to surface "Hermes returned no draft" when the
# drafter (cloud gate + local Hermes) produced no usable message — most often
# `messages` that are dicts MISSING the 'text' key. These two pure helpers let
# it instead funnel that empty state into a fact-anchored placeholder the
# operator can edit, flagged via `fallback_used` + a warning badge.

def _join_draft_parts(messages):
    """Extract text parts from Hermes' `messages` list, tolerant of the two
    shapes it emits: bare strings, or dicts {"role","text"}. A dict missing
    the 'text' key contributes an empty string (the caller filters empties),
    which is the signal the drafter produced no usable message → R4 fallback.
    Non-str/non-dict items are skipped. Pure; mirrors the inline extraction in
    handle_draft_followup so the 'dict-without-text' path is unit-testable."""
    parts = []
    for m in (messages or []):
        if isinstance(m, str):
            parts.append(m.strip())
        elif isinstance(m, dict):
            parts.append((m.get("text") or "").strip())
    return parts


def _fact_anchored_fallback(name="", yacht="", date="", party=""):
    """Last-resort follow-up draft when the drafter returns no usable message
    (R4). Anchors on the facts we already know (yacht / date / party) in
    Maria's warm lowercase default voice rather than a generic 'just checking
    in'. NEVER returns empty — the point is that the operator always has a
    fact-anchored placeholder to edit instead of seeing 'Hermes returned no
    draft'. The caller sets `fallback_used` + a warning badge so this is never
    mistaken for a normal high-quality draft. Pure / deterministic."""
    nm = (name or "").strip()
    first = nm.split(" ")[0] if nm else ""
    yacht = (yacht or "").strip()
    date = (date or "").strip()
    party = str(party or "").strip()
    greet = f"hi {first}" if first else "hi there"
    anchor = ""
    if yacht:
        anchor += f" about the {yacht}"
    if date:
        anchor += f" for {date}"
    if party and party not in ("0", "0.0"):
        anchor += f" for {party} guests"
    if not anchor:
        anchor = " on your enquiry"
    return (f"{greet}, just following up{anchor} — happy to help you lock it "
            f"in or answer any questions whenever suits. let me know!")


# ---------------------------------------------------------------------------
# WAHA-degraded send-block decision (2026-06-02)
# ---------------------------------------------------------------------------

def _waha_send_blocked(ok, status):
    """Should an approved draft's send be BLOCKED because the WhatsApp (WAHA)
    session can't deliver it? True ONLY for a definitive non-sending state —
    STOPPED / SCAN_QR_CODE / FAILED — where the send would silently fail
    (operator thinks it sent, customer gets nothing). WORKING (ok=True) never
    blocks; an ambiguous probe (UNREACHABLE / UNKNOWN / STARTING / anything
    else) does NOT block, so a transient probe blip can never wedge every
    customer send — the actual send attempt + the existing scorecard badge
    warning cover those. Pure/deterministic; the claim-send caller fails OPEN
    (allows the send) on any guard error."""
    if ok:
        return False
    return str(status or "").strip().upper() in (
        "STOPPED", "SCAN_QR_CODE", "FAILED")
