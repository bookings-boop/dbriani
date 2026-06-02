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


# Phrases the LLM analyzer uses when it (often wrongly) believes the booking
# date has elapsed / the event is over.
_PASSED_CLAIM_RE = re.compile(
    r"(has\s+(already\s+)?passed|already\s+passed|is\s+over|event\s+is\s+over|"
    r"is\s+moot|date\s+(has\s+)?passed|already\s+happened|in\s+the\s+past|"
    r"event\s+has\s+(ended|elapsed)|no\s+longer\s+upcoming)",
    re.IGNORECASE,
)


def _passed_date_close_is_wrong(dates_str, reasoning, today=None):
    """4b guard: True when the analyzer's reasoning claims the booking date has
    passed / the event is over BUT the booking date deterministically parses to
    a strictly-FUTURE date — i.e. the 'close' is a hallucination (typically a
    long silence misread as the event being over). Used to VETO an auto-close
    that would wrongly kill a live future booking. Fail-safe: only fires when
    the date parses cleanly to the future; genuinely-past or unparseable/
    relative dates are left alone so real passed-date closes still work.
    Pure/deterministic."""
    if not reasoning or not _PASSED_CLAIM_RE.search(reasoning):
        return False
    d = _parse_booking_date(dates_str)
    if d is None:
        return False
    import datetime as _dt
    today = today or _dt.date.today()
    return d > today


def _booking_date_is_future(dates_str, today=None):
    """True when the booking date deterministically parses to a strictly-FUTURE
    date. Used to override a stale cached close in /review: an upcoming booking
    is an ACTIVE lead, never 'not a customer'. False for past/today/unparseable.
    Pure."""
    d = _parse_booking_date(dates_str)
    if d is None:
        return False
    import datetime as _dt
    today = today or _dt.date.today()
    return d > today


# Bare relative date words a customer typed that get stored verbatim in
# customer_facts.dates — they go stale ("tomorrow" captured days ago still
# renders "tomorrow") and _parse_booking_date can't anchor them.
_RELATIVE_DATE_RE = re.compile(
    r"\b(today|tonight|tomorrow|"
    r"this\s+(week|weekend|evening|afternoon|night|morning)|"
    r"next\s+(week|weekend)|"
    r"(mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)\w*\s+"
    r"(night|evening|afternoon|morning))\b",
    re.IGNORECASE,
)


def _safe_display_date(dates_str):
    """Render a stored booking date for an operator card, but FLAG a bare
    relative word (today/tomorrow/'this weekend'/'Friday night') that was
    captured days ago and never resolved to a calendar date — it renders
    verbatim and goes stale (shows 'tomorrow' forever) and defeats the
    passed-date safeguards. Resolvable absolute dates pass through unchanged;
    'no date' when empty. Pure."""
    s = (dates_str or "").strip()
    if not s:
        return "no date"
    if _parse_booking_date(s) is not None:
        return s
    if _RELATIVE_DATE_RE.search(s):
        return f"{s} ⚠️ reconfirm"
    return s


def _followup_note(out_dur_str, rea, sug):
    """The /review awaiting-reply note for a lead we already followed up. Keeps
    the timing + anti-pushiness cue, but appends the analyzer's lead-specific
    read (reasoning, else suggested action) so the line isn't identical across
    every awaiting-reply lead (bug 5c). Pure."""
    specific = (rea or "").strip() or (sug or "").strip()
    tail = f" — {specific}" if specific else ", don't re-nudge"
    return f"✅ followed up {out_dur_str} ago; awaiting reply{tail}"


def _accumulate_feedback(history, fb, cap=10):
    """Append an operator edit-feedback string to the draft's accumulating list
    so every refine can replay ALL prior corrections (bug 3 — edits used to be
    stateless and forgot earlier feedback). Ignores empty/whitespace; skips a
    consecutive duplicate; caps to the most-recent `cap`. Returns the new list.
    Pure."""
    out = list(history or [])
    fb = (fb or "").strip()
    if fb and (not out or out[-1] != fb):
        out.append(fb)
    return out[-cap:]


# ---------------------------------------------------------------------------
# Lost vs not-a-customer classification (2026-06-02, bug 5e)
# ---------------------------------------------------------------------------
# Split a CLOSED lead's analyzer reasoning into LOST (a real lead that didn't
# convert, with a reason) vs NOT_A_CUSTOMER (vendor/seller/spam/wrong-number),
# so /review stops dumping both under "not a customer". Heuristic over the
# analyzer's free-text reasoning (the structured close_reason isn't stored).

_COMPLETED_RE = re.compile(
    r"\b((booking|charter|trip)\s+(fully\s+)?(executed|completed|complete|"
    r"fulfilled|delivered|done)|(fully\s+)?executed|successfully\s+"
    r"(completed|delivered|chartered)|already\s+(sailed|chartered))\b",
    re.IGNORECASE)
_NAC_RE = re.compile(
    r"\b(vendor|supplier|seller|selling\s+to\s+us|spam|wrong\s+number|"
    r"b2b\s+pitch|partnership|pitch(ing)?|marketing|promot(e|ion|ing)|"
    r"advertis|agency|broker|recruit|job\s+(enquiry|inquiry|application))\b",
    re.IGNORECASE)
_LOST_PRICE_RE = re.compile(
    r"\b(price|pricing|expensive|too\s+much|budget|afford|"
    r"cheaper|out\s+of\s+budget)\b", re.IGNORECASE)
_LOST_COMPETITOR_RE = re.compile(
    r"\b(booked\s+(elsewhere|with\s+another)|another\s+(operator|company|"
    r"provider)|went\s+with|found\s+a\s+boat|competitor|already\s+booked)\b",
    re.IGNORECASE)
_LOST_TIMING_RE = re.compile(
    r"\b(cancel(led|s|ling)?|changed?\s+plans|postpon\w*|"
    r"no\s+longer\s+(needed|looking|interested)|next\s+time|not\s+this\s+time)\b",
    re.IGNORECASE)
_LOST_GHOST_RE = re.compile(
    r"\b(ghost(ed|ing)?|no\s+(reply|response)|unresponsive|"
    r"stopped\s+(replying|responding)|went\s+silent)\b", re.IGNORECASE)


def _close_bucket(reasoning):
    """Classify a closed lead's analyzer reasoning. Returns (bucket, label):
    bucket = 'NOT_A_CUSTOMER' (vendor/seller/spam/wrong-number), 'LOST' (a real
    lead that didn't convert — price/competitor/timing/ghosted), or '' when
    unclear. label = a short display string. Pure/heuristic — vendor/spam is
    checked first so a vendor mentioning price isn't mislabeled 'lost'."""
    r = (reasoning or "").strip()
    if not r:
        return "", ""
    if _COMPLETED_RE.search(r):
        return "COMPLETED", "completed booking"
    if _NAC_RE.search(r):
        return "NOT_A_CUSTOMER", "vendor / not a customer"
    if _LOST_PRICE_RE.search(r):
        return "LOST", "lost — price"
    if _LOST_COMPETITOR_RE.search(r):
        return "LOST", "lost — booked elsewhere"
    if _LOST_TIMING_RE.search(r):
        return "LOST", "lost — cancelled / timing"
    if _LOST_GHOST_RE.search(r):
        return "LOST", "lost — ghosted"
    return "", ""


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


# ---------------------------------------------------------------------------
# Payment match-buttons (2026-06-02)
# ---------------------------------------------------------------------------
#
# A truly-unmatched Nomod charge (no link / no phone / no amount+time hit)
# currently makes the operator read the Nomod dashboard and type
# `/label <name> CONFIRMED` by hand. These two PURE helpers turn that into a
# tap: build_paymatch_keyboard renders the current WAITING_FOR_PAYMENT
# customers as candidate buttons; parse_paymatch_callback decodes the tap.
# The operator still identifies the payer (name/phone is on the card) — the
# button only saves the manual /label. callback_data must stay <= 64 BYTES
# (Telegram limit); a candidate whose cid won't fit is DROPPED rather than
# truncated, because a corrupted cid would promote the wrong customer.

PAYMATCH_PREFIX = "paymatch"
# Telegram caps callback_data at 64 bytes.
_PAYMATCH_CB_MAX = 64


def build_paymatch_keyboard(charge_id, candidates, max_buttons=6):
    """Build the inline_keyboard rows (list of one-button rows) offering each
    candidate customer as a tap-to-confirm match for an unmatched charge.

    candidates: list of {customer_id, name?, phone?, amount?}. Each becomes a
    button 'name · phone · AED amount' with callback_data
    'paymatch:<charge8>:<cid>'. A final '🚫 Not listed' row
    ('paymatch:<charge8>:none') always lets the operator decline. Candidates
    whose callback_data would exceed the 64-byte limit are skipped (safe: a
    truncated cid would mis-promote). Capped at max_buttons candidates."""
    c8 = (charge_id or "")[:8]
    rows = []
    for cand in (candidates or [])[:max_buttons]:
        cid = str(cand.get("customer_id") or "").strip()
        if not cid:
            continue
        cb = f"{PAYMATCH_PREFIX}:{c8}:{cid}"
        if len(cb.encode("utf-8")) > _PAYMATCH_CB_MAX:
            continue  # cannot encode this cid safely — force dashboard path
        name = str(cand.get("name") or "").strip()
        phone = str(cand.get("phone") or "").strip()
        amount = cand.get("amount", cand.get("link_amount"))
        if name:
            text = name[:32] + (f" · {phone}" if phone and phone != name else "")
        else:
            text = phone or cid
        if amount not in (None, "", 0):
            text += f" · AED {amount}"
        rows.append([{"text": text, "callback_data": cb}])
    rows.append([{"text": "🚫 Not listed — check dashboard",
                  "callback_data": f"{PAYMATCH_PREFIX}:{c8}:none"}])
    return rows


def parse_paymatch_callback(data):
    """Decode a 'paymatch:<charge8>:<cid>' callback. Returns
    {'charge': <charge8>, 'cid': <cid or None>} or None if not a paymatch
    callback. cid 'none' (the dismissal button) decodes to cid=None. Strict:
    anything that isn't a well-formed paymatch callback returns None so it can
    never be mistaken for a match instruction."""
    if not data or not isinstance(data, str):
        return None
    parts = data.split(":", 2)
    if len(parts) != 3 or parts[0] != PAYMATCH_PREFIX:
        return None
    charge, cid = parts[1].strip(), parts[2].strip()
    if not charge or not cid:
        return None
    return {"charge": charge, "cid": None if cid == "none" else cid}


# ---------------------------------------------------------------------------
# Outgoing message-bubble guard (2026-06-02 "false" incident)
# ---------------------------------------------------------------------------
#
# A draft's `messages` is a list of WhatsApp bubbles that the send path
# iterates and String()-coerces. On 2026-06-02 a (list, bool) tuple from an
# un-unpacked sanitize_draft_messages() leaked a Python False into that list,
# which became a literal "false" bubble sent to a customer. This canonical
# guard guarantees every bubble is a real, non-empty string — dropping
# booleans, None, numbers, nested lists, blanks, and the literal poison words
# — so a serialization bug / LLM glitch / future regression can never again
# mint a junk bubble. Pure; used at the persistence boundary (_draft_update).

_POISON_BUBBLES = frozenset({"false", "true", "none", "null"})


def _clean_message_bubbles(messages):
    """Return `messages` as a list of clean, non-empty strings. Drops anything
    that isn't a real string bubble (False/None/numbers/nested lists/blank/
    whitespace and the literal 'false'/'true'/'none'/'null'). Non-list/tuple
    input yields [] (not valid bubbles). Pure/deterministic."""
    if not isinstance(messages, (list, tuple)):
        return []
    out = []
    for m in messages:
        if not isinstance(m, str):
            continue
        s = m.strip()
        if not s or s.lower() in _POISON_BUBBLES:
            continue
        out.append(s)
    return out


# ---------------------------------------------------------------------------
# Send status guard (2026-06-02 double-send)
# ---------------------------------------------------------------------------
#
# claim-send historically never read draft.status, so its only idempotency was
# a per-draft_id 60s NX lock. Tapping ✅ Send on an already-sent card, or on a
# stale/superseded SIBLING card for the same customer (an unmerged @lid vs
# @c.us split — a DIFFERENT draft_id), re-sent the customer. This predicate
# decides when a send must be refused because the draft is already terminal.

_TERMINAL_SEND_STATUSES = frozenset({"sent", "superseded", "disregarded"})


def _quality_floor_ok(score, threshold=8):
    """Autonomous-send QUALITY FLOOR. True ONLY when score is a real integer
    >= threshold. FAIL-CLOSED: None, non-int, bool, float, or below-threshold
    all return False — so a scoring error (score 0 / None) or a low score can
    NEVER auto-send; it routes to approval. Pure/deterministic."""
    return (isinstance(score, int) and not isinstance(score, bool)
            and score >= threshold)


def _is_handoff_message(text):
    """True ONLY for the conservative handoff/holding line, which the operator
    allows to auto-send even below the score floor. Requires ALL THREE
    distinctive phrases (normalized) so a fragment or a real answer can never
    match — it can never broaden into a floor bypass. Pure."""
    if not text:
        return False
    t = re.sub(r"\s+", " ", re.sub(r"[^a-z ]", " ", str(text).lower())).strip()
    return ("not the best person to answer" in t
            and "colleagues contact you" in t
            and "correct details" in t)


def _sibling_send_blocked(csent_value, current_did):
    """Customer-level sibling-card send guard decision (2026-06-02 false-positive
    fix). True ONLY when a send to this customer's phone was already claimed by a
    DIFFERENT draft_id (a genuine sibling/duplicate card from an @lid/@c.us
    identity split) within the window. The SAME card re-tapped
    (csent_value == current_did) is NOT blocked here — it falls through to the
    per-draft claim + status guards, which give the accurate 'already sending' /
    'already sent' message instead of a misleading 'duplicate cards' one. Pure;
    None/blank/redis-newline safe (no value or same id -> not blocked)."""
    cv = (csent_value or "").strip()
    cd = (current_did or "").strip()
    return bool(cv and cd and cv != cd)


def _send_blocked_by_status(status):
    """True when an approved send must be REFUSED because the draft is already
    in a terminal state — already sent (double-send), superseded by a newer
    draft, or disregarded. pending / awaiting_* drafts are sendable. Pure; the
    claim-send caller fails OPEN (allows the send) when status can't be read, so
    a transient lookup error never blocks a legitimate first send — only an
    unambiguous terminal status blocks."""
    return str(status or "").strip().lower() in _TERMINAL_SEND_STATUSES


# ---------------------------------------------------------------------------
# Not-convertible digest dedup (2026-06-02, bug 4a)
# ---------------------------------------------------------------------------

def _dedup_leads(items):
    """Collapse a not-convertible card list to ONE entry per customer.
    items = iterable of (cid, name, reason). Dedup by cid first, then by
    normalized non-empty name — the name pass catches UNMERGED @lid/@c.us
    identity splits that share a display name (e.g. 'Noha' under two ids),
    which canonicalize_cid can't yet unify (both rows still merged_into NULL).
    Preserves order; keeps the first occurrence. Pure/deterministic."""
    seen_cid, seen_name, out = set(), set(), []
    for cid, name, reason in items:
        c = (cid or "").strip()
        n = " ".join((name or "").strip().lower().split())
        if (c and c in seen_cid) or (n and n in seen_name):
            continue
        if c:
            seen_cid.add(c)
        if n:
            seen_name.add(n)
        out.append((cid, name, reason))
    return out


# ---------------------------------------------------------------------------
# Capacity-fit constraint for the drafter (2026-06-02 capacity bug)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Identity merge-safety guard (2026-06-02 — Qurbani/Royalty 136 false merge)
# ---------------------------------------------------------------------------
#
# The reconcile (handle_reconcile_identities) merged an @lid row into its
# WAHA-resolved @c.us row using ONLY WhatsApp's live LID->phone lookup, with no
# check that the two rows describe the same human. WhatsApp recycles/re-points
# LIDs, so an old @lid row (Antonio / Bliss 55, 155 msgs) whose LID now resolves
# to a DIFFERENT person's number (Qurbani / Royalty 136) was collapsed into one
# card — burying the high-value lead. This guard requires same-person evidence:
# a merge is refused when the two rows carry DISTINCT REAL names.

_PLACEHOLDER_NAMES = frozenset({
    "", "unknown", "unknown customer", "customer", "guest", "there",
    "client", "lead", "n/a", "na", "none",
})


def _is_real_name(name):
    """True when `name` is a genuine customer name — not blank, a placeholder
    ('unknown'/'customer'/'guest'…), or a phone number rendered as a name.
    Pure; case/whitespace-insensitive."""
    n = " ".join((name or "").strip().lower().split())
    if not n or n in _PLACEHOLDER_NAMES:
        return False
    if n.startswith("+") or n.replace(" ", "").lstrip("+").isdigit():
        return False
    return True


def _name_edit_distance(a, b):
    """Levenshtein edit distance between two short strings (names). Pure.
    Iterative two-row DP — names are short so cost is negligible."""
    a = a or ""
    b = b or ""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(
                prev[j] + 1,                # deletion
                cur[j - 1] + 1,             # insertion
                prev[j - 1] + (ca != cb),   # substitution
            ))
        prev = cur
    return prev[-1]


def _names_likely_same_person(name_a, name_b):
    """Pure. True when two REAL names plausibly describe the SAME human, so the
    reconcile may merge their @lid/@c.us rows. Deliberately CONSERVATIVE — only
    the dominant legit splits qualify; shared-first-name-only and nicknames do
    NOT (we err toward NOT merging: a false merge buries a high-value lead, while
    a refused merge only leaves a reversible duplicate card):
      - exact match (case/space-insensitive); or
      - one name's token-set is a subset of the other's (pushName 'Antonio' vs
        extracted 'Antonio Rossi' — the #1 case); or
      - a small whole-string edit distance (transliteration/typo: Mohammad/
        Mohammed, Sara/Sarah, Jon/John).
    Antonio vs Qurbani (recycled-LID false merge) stays NOT-same -> blocked."""
    na = " ".join((name_a or "").strip().lower().split())
    nb = " ".join((name_b or "").strip().lower().split())
    if not na or not nb:
        return False
    if na == nb:
        return True
    ta, tb = set(na.split()), set(nb.split())
    if ta <= tb or tb <= ta:
        return True
    thresh = 1 if min(len(na), len(nb)) <= 6 else 2
    return _name_edit_distance(na, nb) <= thresh


def _merge_blocked(name_a, name_b):
    """Identity merge-safety guard. True when two customer rows must NOT be
    auto-merged because they carry DISTINCT real names that do NOT look like the
    same person — strong evidence they are different humans (the Qurbani->Antonio
    recycled-LID false merge). Returns False (ALLOW) when either name is missing/
    placeholder/phone (a bare @lid legitimately merges into its named @c.us row)
    OR the two names plausibly describe the same person (exact / first-name-vs-
    full-name / transliteration-typo — see _names_likely_same_person). Pure; the
    reconcile caller fails OPEN only for non-name reasons.

    (2026-06-02 PM loosen, stress-finding #3: the prior `na != nb` blocked the
    MOST COMMON legit split — first-name / typo — causing duplicate draft cards;
    the safety net now blocks only genuinely-different names.)"""
    if not _is_real_name(name_a) or not _is_real_name(name_b):
        return False
    return not _names_likely_same_person(name_a, name_b)


def _do_not_merge_pinned(cid_a, cid_b, rows):
    """Pure, name-INDEPENDENT durable un-merge pin (stress-finding #4). Returns
    True when customer_label_history holds a 'do_not_merge' row that links this
    exact pair — written the first time the reconcile refuses a name-conflicting
    merge, so a later name-refresh that blanks/aligns a name can never silently
    re-merge them. `rows` = iterable of (customer_id, signal, evidence)."""
    a = (cid_a or "").strip()
    b = (cid_b or "").strip()
    if not a or not b:
        return False
    for row in rows:
        cid = (row[0] or "").strip()
        signal = (row[1] or "").strip() if len(row) > 1 else ""
        evidence = (row[2] or "") if len(row) > 2 else ""
        if signal != "do_not_merge":
            continue
        if cid == a and b in evidence:
            return True
        if cid == b and a in evidence:
            return True
    return False


def _manual_override_protects(signal, age_days, window_days=14):
    """Pure. True when a lead carries a RECENT manual operator override
    (signal 'manual:*') that the analyzer's auto-demote-to-COLD must NOT
    silently undo (stress #11 — the manual HOT reverts on never-booked Dean
    Pearson rows had no label lock, so the cron could re-COLD them). Past
    window_days the automation may re-evaluate. Fail-SAFE: an unknown/negative
    age (just-written / clock skew) protects."""
    s = (signal or "").strip().lower()
    if not s.startswith("manual:"):
        return False
    if age_days is None:
        return True
    try:
        age = float(age_days)
    except (TypeError, ValueError):
        return True
    if age < 0:
        return True
    return age <= window_days


# ---------------------------------------------------------------------------
# Draft-log (migration 007) — pure column whitelist + coercion for the
# fail-safe UPSERT in server._draft_log_write. ADDITIVE observability only;
# this never changes what is sent, it only records it.
# ---------------------------------------------------------------------------
_DRAFT_LOG_COLS = {
    "customer_id": str, "customer_name": str, "trigger_kind": str,
    "incoming_message": str, "draft_text": str, "mode": str,
    "score": int, "score_flags": str, "score_summary": str,
    "outcome": str, "final_text": str, "is_shadow": bool,
}


def _draft_log_columns(fields):
    """Pure. Whitelist + type-coerce fields for a draft_log UPSERT. Drops
    unknown columns and None values (so a partial lifecycle UPSERT never NULLs a
    prior value), coerces score->int (a bool is NOT a score), is_shadow->bool,
    the rest->str. An un-coercible value drops that one column (fail-safe)."""
    out = {}
    if not isinstance(fields, dict):
        return out
    for col, typ in _DRAFT_LOG_COLS.items():
        if col not in fields:
            continue
        v = fields[col]
        if v is None:
            continue
        try:
            if typ is int:
                if isinstance(v, bool):
                    continue          # True must never become score 1
                out[col] = int(v)
            elif typ is bool:
                out[col] = bool(v)
            else:
                out[col] = str(v)
        except (TypeError, ValueError):
            continue                  # un-coercible -> skip the column
    return out


# ---------------------------------------------------------------------------
# Auto-demote-to-COLD guard + event-passed detector (2026-06-02 — Tal Sudai)
# ---------------------------------------------------------------------------
#
# Tal Sudai (a CASH booking) sat at NEW because a cash deal never reaches
# CONFIRMED (which requires a payment_link_sent). His dates field was the literal
# word 'today' — never anchored to a calendar date — so the analyzer read the
# silence as "the event passed" and auto-demoted NEW->COLD. The existing demote
# only protected WAITING_FOR_PAYMENT/CONFIRMED; NEW was demotable, and there was
# no check that the "passed" claim was actually VERIFIABLE.

def _demote_to_cold_blocked(dates_str, reasoning, ever_booked=False,
                            today=None):
    """Guard the analyzer's auto-demote-to-COLD. Returns True (BLOCK the demote)
    when:
      (a) the customer has EVER been booked/paid (`ever_booked`) — a won/paid
          lead must never be auto-killed to COLD; or
      (b) the close is driven by a 'date passed / event over' claim BUT the
          booking date does NOT deterministically parse to a date on/before
          today — i.e. the 'passed' is unverifiable (relative/unparseable like
          'today'/'tomorrow') or actually FUTURE. We refuse to kill a lead on a
          date we cannot confirm has passed (the Tal Sudai incident).
    A close for a REAL reason (price/competitor/ghost/spam/vendor) on ANY date
    is NOT blocked — only passed-date-driven closes are gated on verifiability.
    Pure/deterministic; None-safe."""
    if ever_booked:
        return True
    if reasoning and _PASSED_CLAIM_RE.search(reasoning):
        import datetime as _dt
        today = today or _dt.date.today()
        d = _parse_booking_date(dates_str)
        if d is None or d > today:
            return True
    return False


def event_passed(dates_str, today=None):
    """True ONLY when the booking date deterministically parses to a date
    strictly BEFORE today (the event already happened). False for today /
    future / unparseable dates — we claim 'passed' only when we can prove it.
    The single post-event detector for /review render (replaces ad-hoc copies).
    Pure; None-safe."""
    d = _parse_booking_date(dates_str)
    if d is None:
        return False
    import datetime as _dt
    today = today or _dt.date.today()
    return d < today


def _party_size_fit_line(party_size):
    """Drafter capacity constraint: only recommend yachts that fit the stated
    party. Returns a one-line instruction, or '' when the party size is unknown
    / non-numeric / <= 0 (safe to concatenate). Rides _lead_state_block ->
    behavioral_context().formatted -> the live drafter, no n8n change. Pure."""
    try:
        n = int(str(party_size).strip())
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    return (f"Party size: {n} guests — ONLY recommend yacht(s) that seat at "
            f"least {n}. NEVER suggest a yacht whose max capacity is below "
            f"{n}; if the party exceeds every single yacht, recommend the "
            f"largest fitting option(s) or note combining two boats.")
