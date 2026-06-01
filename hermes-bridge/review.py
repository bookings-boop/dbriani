#!/usr/bin/env python3
"""review.py — pipeline review render layer + customer-facts pure
helpers + UAE working-hours gate.

PURE pieces — no DB, no Hermes calls. The DB readers
(read_lead_summary, mark_review_seen) and Hermes wrappers
(refresh_customer_facts_from_waha, hermes_analyze_lead) stay in
server.py for now since their call graphs are wider; they'll move
when routes.py is extracted.

This module's public surface (covered by test_score_lead.py,
test_why_line.py, test_facts_extract_gate.py, test_merge_facts.py):

  score_lead(row, now_dt)               priority ranker
  render_review(scored, totals, mode)   full /review render
  _why_line(row, label_key)             per-card guidance string
  _fmt_dur(secs)                        human-readable duration
  _name_fallback(customer_id)           …1234 display fallback
  _facts_extract_gate(incoming_message) extraction-cost guard
  _merge_facts(cached, extracted)       cache-vs-extracted merge
  build_customer_header(facts)          context-header string
  _is_uae_working_hours(now_epoch)      cron gate
"""
import os
import re
import time


# --- yacht/facts-extraction keywords -------------------------------
# yacht keywords worth gating extraction on — curated from
# system-prompt.md §7.
YACHT_NAMES = (
    "satoshi", "enigma", "aurora", "azimut", "sunseeker", "ferretti",
    "pershing", "benetti", "beneteau", "galeon", "riva", "princess",
    "lamborghini", "sanlorenzo", "maiora", "baglietto", "elan", "elise",
    "diana", "zenith", "bliss", "von dutch", "cabo", "belle", "monaco",
    "cante", "carina", "haigan", "zirve", "luna", "notorious", "asya",
    "zeta", "dolce vita", "tatti", "sapphire", "odysea", "royalty",
    "mila", "athena", "skyfall", "finesse", "sofiya", "eclipse",
    "royal mirage", "yacht",
)

_FACTS_DATE_RE = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|january|february|"
    r"march|april|june|july|august|september|october|november|december|"
    r"mon|tue|wed|thu|fri|sat|sun|monday|tuesday|wednesday|thursday|friday|"
    r"saturday|sunday|today|tomorrow|tonight|weekend|week|month)\b",
    re.IGNORECASE)
_FACTS_NAME_RE = re.compile(
    r"\b(i'?m |i am |my name|this is |call me |name'?s )", re.IGNORECASE)
_FACTS_BOOK_RE = re.compile(
    r"\b(book|booking|reserve|charter|deposit|confirm|pay|payment|guests?|"
    r"pax|people|persons?|birthday|proposal|anniversary|wedding|corporate)\b",
    re.IGNORECASE)


# --- /review render caps (Telegram 4096-char limit safe) ----------
REVIEW_CAP_HOT = int(os.environ.get("REVIEW_CAP_HOT", "10"))
REVIEW_CAP_NEEDS_ATTENTION = int(os.environ.get(
    "REVIEW_CAP_NEEDS_ATTENTION", "10"))
REVIEW_CAP_WARM = int(os.environ.get("REVIEW_CAP_WARM", "8"))
REVIEW_CAP_COLD = int(os.environ.get("REVIEW_CAP_COLD", "5"))

# /review inline auto-heal — for customers with missing critical
# facts (no name AND no yacht), refresh from WAHA history before
# rendering. Bounded so /review latency stays under 10s even with a
# stale cohort. 5 parallel refreshes × ~5s per Hermes extract =
# ~5-7s wall-clock.
# 2, not 5: each inline refresh is a Hermes facts-extraction (CPU-heavy on the
# 2-vCPU box). At 5, repeatedly running /review fired facts-extraction storms
# that saturated the box → docker-exec psql timeouts → /review & /lead failed
# (operator 2026-06-01). Lower cap keeps /review light; the background sweep
# still heals the rest.
REVIEW_INLINE_REFRESH_CAP = int(os.environ.get(
    "REVIEW_INLINE_REFRESH_CAP", "2"))

# UAE working hours (Asia/Dubai = UTC+4, no DST). Used by
# /pipeline-analyze cron to skip overnight runs — keeps the Hermes
# spend in business hours.
UAE_WORK_HOURS_START = int(os.environ.get("UAE_WORK_HOURS_START", "9"))
UAE_WORK_HOURS_END = int(os.environ.get("UAE_WORK_HOURS_END", "21"))


def _is_uae_working_hours(now_epoch=None):
    """True if current wall-clock falls inside [START,END) Asia/Dubai.
    UAE has no daylight savings so a flat +4 offset works year-round."""
    if now_epoch is None:
        now_epoch = time.time()
    # gmtime returns UTC; +4h offset for Dubai
    utc_hour = time.gmtime(now_epoch).tm_hour
    dubai_hour = (utc_hour + 4) % 24
    return UAE_WORK_HOURS_START <= dubai_hour < UAE_WORK_HOURS_END


# --- customer-facts pure helpers ----------------------------------

def _facts_extract_gate(incoming_message):
    """Heuristic: should this (non-first) message trigger a fresh extraction?
    True if it plausibly carries a new fact — a digit, a known yacht keyword,
    a date/time word, a self-introduction, or a booking keyword."""
    m = (incoming_message or "").lower()
    if not m:
        return False
    if any(ch.isdigit() for ch in m):
        return True
    if any(y in m for y in YACHT_NAMES):
        return True
    return bool(_FACTS_DATE_RE.search(m) or _FACTS_NAME_RE.search(m)
                or _FACTS_BOOK_RE.search(m))


def build_customer_header(facts):
    """facts: {name,dates,yachts,party_size,message_count} -> header string.
    Each optional line shows only when its fact is non-empty; the name line,
    the message-count line and the divider are always present."""
    f = facts or {}
    name = str(f.get("name") or "").strip()
    lines = ["\U0001F464 " + (name or "New contact")]
    if str(f.get("dates") or "").strip():
        lines.append("\U0001F4C5 Interested in: " + str(f["dates"]).strip())
    if str(f.get("yachts") or "").strip():
        lines.append("\U0001F6E5️ Looking at: " + str(f["yachts"]).strip())
    if str(f.get("party_size") or "").strip():
        lines.append("\U0001F465 Party size: " + str(f["party_size"]).strip())
    try:
        mc = int(f.get("message_count") or 0)
    except (TypeError, ValueError):
        mc = 0
    lines.append("\U0001F522 Message #" + str(mc) + " in conversation")
    lines.append("─" * 30)
    return "\n".join(lines)

def _merge_facts(cached, extracted):
    """Merge rule: a non-empty extracted field overwrites; an empty extracted
    field keeps the cached value — a failed/partial extraction never erases a
    known fact."""
    cached = cached or {}
    out = {}
    for k in ("name", "dates", "yachts", "party_size"):
        ev = str((extracted or {}).get(k, "") or "").strip()
        out[k] = ev or str(cached.get(k) or "")
    return out


# --- /review ranking + render -------------------------------------

# Yacht hourly rate (AED/hr) from catalog §7. Used to rank leads by the
# VALUE of the yachts they're interested in (operator 2026-05-29: "based
# on hourly yacht rate the customers are interested in, it should be
# sorted" — William/Pershing 82 @5,500 should outrank mid-rate leads).
# Keys are lowercase substrings matched against customer_facts.yachts.
YACHT_RATE = {
    "élan 44": 799, "elan 44": 799, "elise 50": 900, "diana 50": 1100,
    "novia 55": 1300, "zenith 64": 1300, "bliss 55": 1400,
    "von dutch 40": 1400, "azimut 62": 1500, "cabo 77": 2200,
    "azimut 50": 2700, "azimut 79": 2700, "belle 75": 2800,
    "sunseeker 88": 2800, "pershing 5x": 2900, "monaco 60": 3000,
    "satoshi": 3000, "ferretti 670": 3500, "eclipse 90": 4000,
    "cante 97": 4400, "carina 75": 4400, "azimut 70": 4500,
    "azimut 77": 4500, "haigan": 4500, "zirve 72": 4500,
    "azimut 88": 4750, "luna 101": 5000, "galeon 780": 5000,
    "notorious": 5000, "asya 110": 5300, "ferretti 780": 5500,
    "pershing 82": 5500, "benetti 120": 5500, "royal mirage": 6000,
    "zeta 100": 7000, "dolce vita": 7500, "riva 82": 9000,
    "tatti 110": 9000, "sapphire 150": 9000, "baglietto 110": 9000,
    "princess x95": 9900, "lamborghini 63": 10000, "odysea 130": 10000,
    "aurora 130": 14000, "sunseeker 131": 15000, "royalty 136": 15000,
    "saffuriya": 15000, "thunder": 15000, "mila 141": 18000,
    "athena 170": 18000, "skyfall 177": 20000, "finesse": 20000,
    "sofiya": 20000,
}


def _yacht_max_rate(yachts_str):
    """Max catalog hourly rate (AED/hr) among the yachts the customer is
    interested in; 0 if none recognised. Drives strict rate-first
    ordering within each /review section."""
    y = (yachts_str or "").lower()
    if not y:
        return 0
    best = 0
    for name, rate in YACHT_RATE.items():
        if name in y and rate > best:
            best = rate
    return best


def _yacht_rate_bonus(yachts_str):
    """Additive score bonus scaled by the customer's max yacht rate
    (rate/3, capped at 2400 to stay under the WAITING/CONFIRMED tier
    bases). Keeps the flat cross-section score roughly value-weighted;
    strict within-section ordering is handled by _yacht_max_rate."""
    return min(_yacht_max_rate(yachts_str) // 3, 2400)


def _booking_urgency_bonus(dates_str):
    """Return urgency bonus by booking-date proximity. Operator's #1
    sorting concern: bookings happening NOW must surface above bookings
    months out, regardless of label.

      today / tomorrow → +600  (drop-everything)
      within 3 days    → +400
      within 7 days    → +200
      else             → 0

    Past dates return 0 — _label_eval already demotes them to COLD via
    the date_passed signal."""
    d = _parse_booking_date(dates_str)
    if not d:
        return 0
    import datetime as _dt
    today = _dt.date.today()
    days_until = (d - today).days
    if days_until < 0:
        return 0  # past date — already handled by label demotion
    if days_until <= 1:
        return 600
    if days_until <= 3:
        return 400
    if days_until <= 7:
        return 200
    return 0


# Re-export from labels (already imported via server's re-export chain
# but be explicit here so score_lead is self-contained).
from labels import _parse_booking_date  # noqa: E402


def score_lead(row, now_dt):
    """Compute priority score per docs/pipeline-review-plan.md §2e step 3.
    Pure function; deterministic. row is the dict shape from _read_lead_summary.
    Negative scores → PAUSED/snoozed tail.

    Sorting layers (highest priority first):
      1. CONFIRMED terminal short-circuit (5000)
      2. label base (NEEDS_ATTENTION 1000, HOT 800, etc.) — except
         WAITING_FOR_PAYMENT which is 4000 base + bonuses
      3. urgency_by_booking_date: today/tomorrow +600, ≤3d +400, ≤7d +200
      4. yacht_rate_bonus: scaled by max hourly rate (rate/3, cap 2400)
      5. urgency_by_silence (we_owe_reply, HOT silent >2h, same-day, etc.)
      6. importance_score from Hermes (0-90 within tier)
    """
    score = 0
    label = row.get("label") or "NEW"
    # DISREGARDED is terminal-closed — score deeply negative so they fall
    # into the pause_tail (and render_review filters them out entirely
    # before pause_tail builds, so they're never displayed at all).
    if label == "DISREGARDED":
        return -100000
    # CONFIRMED is a terminal/success state — short-circuit before any urgency
    # or damping math can take the score negative and dump them into the
    # paused tail. They render in their own ✅ section.
    if label == "CONFIRMED":
        return 5000
    # WAITING_FOR_PAYMENT base — high but no longer short-circuit, so
    # urgency + yacht-size bonuses can stack. A WAITING_FOR_PAYMENT
    # booking tomorrow (+600) for a large yacht (+200) reaches 4800 —
    # second only to CONFIRMED. Operator: "+44 7547 600306
    # WAITING_FOR_PAYMENT for booking tomorrow should be top".
    if label == "WAITING_FOR_PAYMENT":
        score += 4000
    elif label == "NEEDS_ATTENTION":
        score += 1000
    elif label == "HOT":
        score += 800
    elif label == "WARM":
        score += 500
    elif label == "NEW":
        score += 300
    elif label == "COLD":
        # "almost-bought" cold = had booking intent but no payment
        if row.get("last_booking_intent_at"):
            score += 100
        kind = row.get("last_rejection_kind")
        lra = row.get("last_rejection_at_seconds")  # seconds since rejection
        if kind == "rejected_price" and lra is not None and lra > 30 * 86400:
            score += 50
        if kind == "rejected_timing":
            score += 50  # always surfaceable if cold + rejected_timing
    if label.startswith("PAUSED_"):
        score -= 10000
    if row.get("label_locked_active"):
        score -= 500

    # Booking-date urgency — applies to ALL active labels including
    # WAITING_FOR_PAYMENT. A tomorrow-booking outranks a months-out
    # booking regardless of label-tier base.
    score += _booking_urgency_bonus(row.get("dates") or "")

    # Yacht-rate bonus — scaled by the hourly rate of the yachts the
    # customer wants, so higher-value charters sort to the top within a
    # label tier (operator 2026-05-29).
    score += _yacht_rate_bonus(row.get("yachts") or "")

    # Hermes importance — additive bonus within the label tier. Cap at +90
    # so a HOT (base 800) with importance=100 reaches 890 — still well below
    # NEEDS_ATTENTION (1000), preserving label-based sectioning while letting
    # Hermes re-sort within each section by recency-of-value.
    imp = row.get("importance_score")
    if isinstance(imp, int):
        score += min(90, max(0, imp))

    # urgency boosts
    cmsg = row.get("last_customer_message_at_seconds")  # silent secs
    orep = row.get("last_operator_reply_at_seconds")
    we_owe = (cmsg is not None and orep is not None and cmsg < orep and cmsg < 99999999)
    # "we owe a reply" really means: customer msg is more recent than our reply
    # AND it's been >30min since they spoke.
    if (cmsg is not None and cmsg < 99999999  # not epoch-fallback
            and (orep is None or orep > cmsg)
            and cmsg > 30 * 60):
        score += 300
    if label == "HOT" and cmsg is not None and cmsg > 2 * 3600:
        score += 200
    dates = (row.get("dates") or "").lower()
    sameday = ("today" in dates or "tonight" in dates)
    if sameday and (orep is None or (cmsg is not None and (orep > cmsg))):
        score += 400
    plink = row.get("last_payment_link_at_seconds")
    ppromised = row.get("last_payment_promised_at_seconds")
    if (plink is not None and ppromised is None and plink > 24 * 3600):
        score += 150

    # damping — keep nudge damping (don't keep re-pushing the same nudge
    # for 24h after operator already drafted one), but DROP review-seen
    # damping (that suppressed the only WARM lead just because the operator
    # looked at the report a moment ago — the report should be consistent
    # across consecutive /review calls).
    nudge = row.get("last_nudge_drafted_at_seconds")
    if nudge is not None and nudge < 24 * 3600:
        score -= 100

    return score

def _fmt_dur(secs):
    """Human-readable '9h', '24m', '3d' etc. None → '—'."""
    if secs is None:
        return "—"
    if secs < 90:
        return f"{int(secs)}s"
    if secs < 90 * 60:
        return f"{int(secs / 60)}m"
    if secs < 36 * 3600:
        return f"{int(secs / 3600)}h"
    return f"{int(secs / 86400)}d"



# Cyrillic → Latin transliteration so the operator can read non-English
# customer names (operator 2026-05-31: a Russian-named lead "Богдан" was
# unrecognisable). Russian/Ukrainian coverage; unknown chars pass through.
_CYR2LAT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "ґ": "g", "д": "d", "е": "e",
    "ё": "yo", "є": "ye", "ж": "zh", "з": "z", "и": "i", "і": "i", "ї": "yi",
    "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p",
    "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "shch", "ъ": "", "ы": "y", "ь": "", "э": "e",
    "ю": "yu", "я": "ya",
}


def _translit_name(name):
    """Latin transliteration of a Cyrillic name ('Богдан' -> 'Bogdan') so the
    operator can read it. '' if the name has no Cyrillic or is unchanged."""
    s = name or ""
    if not any("Ѐ" <= ch <= "ӿ" for ch in s):
        return ""
    out = []
    for ch in s:
        lat = _CYR2LAT.get(ch.lower())
        if lat is None:
            out.append(ch)
            continue
        if ch.isupper() and lat:
            lat = lat[0].upper() + lat[1:]
        out.append(lat)
    res = "".join(out).strip()
    return res if (res and res.lower() != s.lower()) else ""


def render_review(scored, totals, mode="ondemand"):
    """Return a dict with both the single-message rendering (kept for backward
    compat) AND a per-lead-cards rendering so the workflow can post one message
    per lead — each lead's 3-button inline keyboard then sits with that lead's
    text, instead of stacking 6× at the bottom of one big report.

    Returns:
      {
        telegram_text: <full single-message render — backward-compat header>,
        inline_keyboards: <all rows stacked, backward-compat>,
        per_lead_messages: [
          {text: '...', inline_keyboard: [[{text:'Draft nudge',...}, ...]]},
          ...
        ],
        header_text: <just the summary header, no lead lines>,
        mark_seen_ids: [...],
      }
    """
    sections = {
        "AWAITING_REPLY":  {"items": [], "cap": 30,
                            "header": "📨 AWAITING YOUR REPLY — customer messaged, no reply yet",
                            "emoji": "📨"},
        "WAITING_FOR_PAYMENT": {"items": [], "cap": 20,
                            "header": "⏳ WAITING FOR PAYMENT — link sent, awaiting payment",
                            "emoji": "⏳"},
        "HOT":             {"items": [], "cap": REVIEW_CAP_HOT,
                            "header": "🔥 HOT — ready to close",
                            "emoji": "🔥"},
        "NEEDS_ATTENTION": {"items": [], "cap": REVIEW_CAP_NEEDS_ATTENTION,
                            "header": "⚠️ NEEDS ATTENTION",
                            "emoji": "⚠️"},
        "WARM":            {"items": [], "cap": REVIEW_CAP_WARM,
                            "header": "♨️ WARM — worth nudging",
                            "emoji": "♨️"},
        "NEW":             {"items": [], "cap": 5,
                            "header": "🌱 NEW — early conversations",
                            "emoji": "🌱"},
        "COLD":            {"items": [], "cap": REVIEW_CAP_COLD,
                            "header": "❄️ COLD — re-engage candidates",
                            "emoji": "❄️"},
        "CONFIRMED":       {"items": [], "cap": 20,
                            "header": "✅ CONFIRMED — booked / paid",
                            "emoji": "✅"},
    }
    pause_tail = []
    seen_ids = []
    for score, row in scored:
        label = row.get("label") or "NEW"
        # DISREGARDED is terminal-closed — hide completely. Not in any
        # section, not in pause_tail, not in totals. Operator can /label
        # to reopen if they change their mind.
        if label == "DISREGARDED":
            continue
        # PAUSED_* always tail. CONFIRMED is success — keep on the report,
        # but in its own section without nudge buttons (handled below).
        if label.startswith("PAUSED_") or score < 0:
            pause_tail.append(row)
            continue
        if label not in sections:
            continue
        # AWAITING REPLY — the customer messaged after our last outbound (or we
        # never replied): we OWE a reply. Pull these into a top, uncapped
        # section so an unanswered customer (especially a question) is NEVER
        # buried in a capped tier's overflow (operator 2026-05-31: HOT lead
        # 'Богдан' with an unanswered question was hidden by the HOT cap).
        _cs = row.get("last_customer_message_at_seconds")
        _rs = row.get("last_operator_reply_at_seconds")
        _ns = row.get("last_nudge_drafted_at_seconds")
        _outs = [s for s in (_rs, _ns) if isinstance(s, (int, float))]
        _out = min(_outs) if _outs else None
        _owe = isinstance(_cs, (int, float)) and (_out is None or _cs < _out)
        # Only a GENUINE active prospect belongs in AWAITING_REPLY. Exclude any
        # lead the analyzer scored exactly 0 = non-customer / supplier / spam /
        # a finished-and-done trip (operator 2026-06-01: a fruit SUPPLIER 'Alma'
        # and a completed trip 'Émilie', both importance 0, sat on top of the
        # pipeline as "awaiting reply"). A NULL/unscored new lead is still allowed.
        _imp = row.get("importance_score")
        try:
            _imp_zero = (_imp is not None and int(_imp) == 0)
        except (TypeError, ValueError):
            _imp_zero = False
        # CONFIRMED = a won/finished booking, not a lead "awaiting reply"
        # (operator 2026-06-01: Luke, trip finished, was showing as awaiting
        # reply). It stays in the CONFIRMED section instead.
        if _owe and not _imp_zero and label != "CONFIRMED":
            sections["AWAITING_REPLY"]["items"].append((score, row))
        else:
            sections[label]["items"].append((score, row))
        seen_ids.append(row["customer_id"])

    # Sort NEW section by last_customer_message_at DESC NULLS LAST — most
    # recently active customer first. Score-based tie-breaking was letting
    # genuinely new contacts fall into the overflow behind older NEW leads.
    # Other sections keep their score-based order (their boosts already
    # encode recency via the "we owe a reply > 30min" rule).
    def _recency_key(item):
        secs = item[1].get("last_customer_message_at_seconds")
        return (secs is None, secs if secs is not None else 0)
    sections["NEW"]["items"].sort(key=_recency_key)
    # AWAITING_REPLY: active leads (an unanswered live lead = revenue at risk)
    # rank ABOVE CONFIRMED post-booking messages (often just a "thanks"); within
    # each group, longest-waiting first (SLA fairness).
    # operator 2026-06-01 (HOT lead Amaka was #4 while waiting): within the
    # awaiting-reply queue, rank by HEAT first (HOT on top), then longest-
    # waiting — an unanswered HOT lead is the most expensive to ignore.
    _AWAIT_LABEL_PRIO = {"WAITING_FOR_PAYMENT": 0, "HOT": 0, "NEEDS_ATTENTION": 1,
                         "WARM": 2, "NEW": 3, "COLD": 4}
    sections["AWAITING_REPLY"]["items"].sort(
        key=lambda it: (
            (it[1].get("label") or "") == "CONFIRMED",                 # active first
            _AWAIT_LABEL_PRIO.get(it[1].get("label") or "", 3),        # HOT → COLD
            -(it[1].get("last_customer_message_at_seconds") or 0),     # longest-waiting
        ))

    # STRICT rate-first ordering within each value-relevant section
    # (operator 2026-05-29): order by the highest hourly rate of the
    # yachts the customer wants, descending; the additive score is the
    # tiebreaker (urgency/importance). This guarantees e.g. an
    # AK Royalty 136 @18,000/hr ranks above a Tatti 110 @9,000/hr even
    # when the additive rate bonus is capped. NEW keeps recency order
    # (yacht rate is usually unknown that early).
    def _rate_key(item):
        return (_yacht_max_rate(item[1].get("yachts") or ""), item[0])
    for _lk in ("WAITING_FOR_PAYMENT", "HOT", "NEEDS_ATTENTION",
                "WARM", "COLD", "CONFIRMED"):
        sections[_lk]["items"].sort(key=_rate_key, reverse=True)

    # ---- Single-message render (backward compat) ----------------------------
    when = ("Scheduled review" if mode == "scheduled"
            else "On-demand review")
    header_lines = [f"📋 *Pipeline Review* — {when}",
                    (f"{totals.get('total', 0)} active · "
                     f"{totals.get('HOT', 0)} hot · "
                     f"{totals.get('NEEDS_ATTENTION', 0)} need attention · "
                     f"{totals.get('COLD', 0)} cold"),
                    "_Prioritised view — top leads per tier shown; "
                    "`/review <tier>` (e.g. `/review hot`) for the full list._"]
    lines = list(header_lines) + [""]
    keyboards = []
    per_lead_messages = []

    for label_key in ("AWAITING_REPLY", "WAITING_FOR_PAYMENT", "HOT",
                      "NEEDS_ATTENTION", "WARM", "NEW", "COLD", "CONFIRMED"):
        sect = sections[label_key]
        items = sect["items"]
        if not items:
            continue
        cap = sect["cap"]
        shown = items[:cap]
        overflow = len(items) - len(shown)
        if overflow > 0:
            lines.append(
                f"*{sect['header']}* — showing top {len(shown)} of {len(items)}")
        else:
            lines.append(f"*{sect['header']}* ({len(items)})")
        for i, (score, row) in enumerate(shown, 1):
            why = _why_line(row, label_key)
            # Hermes importance — append score + suggestion when present.
            # Dedupe: if _why_line already used suggested_action (CONFIRMED/
            # WAITING_FOR_PAYMENT path), don't repeat it on the 🧠 line —
            # show the reasoning instead, or just the score alone.
            imp = row.get("importance_score")
            imp_bits = ""
            if isinstance(imp, int):
                imp_bits = f"\n🧠 Hermes: *{imp}/100*"
                if label_key == "CONFIRMED":
                    # Booked & paid — any cached suggested_action/reasoning
                    # predates the payment (e.g. 'send payment link to
                    # lock the deposit'), so it's stale and misleading
                    # (Antonio, 2026-05-28). Suppress it; point the
                    # operator at post-sale actions instead.
                    imp_bits += (" — _booked & paid · confirm logistics "
                                 "or upsell (extra hour / add-ons)_")
                else:
                    # STALENESS GUARD (2026-05-29): the cached
                    # suggested_action/reasoning comes from the last
                    # hermes_analyze_lead run. If the customer has
                    # messaged OR we've nudged SINCE that analysis, the
                    # recommendation is out of date — it caused /review to
                    # recommend nudges to customers who'd already gone
                    # silent on prior nudges, or who just said "plans
                    # changed" (Aysar). _seconds = seconds-ago, so a
                    # LARGER value is older: the analysis is stale when it
                    # ran longer ago than the latest activity. Suppress
                    # the suggestion + tell the operator to open the chat;
                    # the draft flow re-analyses with fresh history.
                    _imp_s = row.get("importance_analyzed_at_seconds")
                    _cust_s = row.get("last_customer_message_at_seconds")
                    _nudge_s = row.get("last_nudge_drafted_at_seconds")
                    _reply_s = row.get("last_operator_reply_at_seconds")
                    # Our most-recent outbound (reply OR nudge), in
                    # seconds-ago (smaller = more recent).
                    _out_cands = [s for s in (_reply_s, _nudge_s)
                                  if isinstance(s, (int, float))]
                    _out_s = min(_out_cands) if _out_cands else None
                    # We messaged AFTER the customer → awaiting their reply.
                    _we_last = (_out_s is not None
                                and (not isinstance(_cust_s, (int, float))
                                     or _out_s < _cust_s))
                    # Analysis stale if ANY activity (customer msg or our
                    # outbound) happened after it ran.
                    _stale = isinstance(_imp_s, (int, float)) and (
                        (isinstance(_cust_s, (int, float))
                         and _imp_s > _cust_s + 60)
                        or (_out_s is not None and _imp_s > _out_s + 60))
                    sug = (row.get("suggested_action") or "").strip()
                    rea = (row.get("importance_reasoning") or "").strip()
                    why_used_sug = (sug and why == sug)
                    if _we_last and _out_s is not None:
                        # We ALREADY followed up and they haven't replied —
                        # never recommend another nudge (Shanebabu was
                        # told 'send nudge' 8 min after we messaged him,
                        # 2026-05-29). Show when + that we're waiting.
                        imp_bits += (
                            f" — _✅ followed up {_fmt_dur(_out_s)} ago — "
                            f"awaiting reply, don't re-nudge_")
                    elif _stale:
                        # Customer was active since the analysis ran; the
                        # cached recommendation is out of date.
                        imp_bits += (" — _🔄 customer active since last "
                                     "analysis — open chat / draft to "
                                     "re-check_")
                    elif sug and not why_used_sug:
                        imp_bits += f" — _{sug}_"
                    elif rea and why_used_sug:
                        # Why-line already carries the suggestion; show the
                        # 'why this score' reasoning on the 🧠 line instead.
                        imp_bits += f" — _{rea}_"
                    elif rea:
                        imp_bits += f" — _{rea}_"
            try:
                from waha import country_flag_for_cid
                _flag = country_flag_for_cid(row.get("customer_id"))
            except Exception:
                _flag = ""
            _nm = row.get("name") or _name_fallback(row.get("customer_id"))
            # Non-English name → append a Latin transliteration; show the phone
            # so the operator can find a lead by NUMBER too (operator 2026-05-31).
            _tr = _translit_name(_nm)
            _nm_disp = _nm + (f" ({_tr})" if _tr else "")
            try:
                from waha import display_phone_for_cid
                _ph = display_phone_for_cid(row.get("customer_id"))
            except Exception:
                _ph = ""
            _idline = f"*{_nm_disp}*" + (f"  {_ph}" if _ph else "")
            lead_body = (
                f"{(_flag + ' ') if _flag else ''}{_idline} — "
                f"{(row.get('yachts') or 'no yacht set')} · "
                f"{(row.get('dates') or 'no date')} · "
                f"msg #{row.get('message_count')}\n"
                f"⏱ silent {_fmt_dur(row.get('last_customer_message_at_seconds'))}"
                f"  ·  {why}"
                f"{imp_bits}"
            )
            lines.append(f"{i}. " + lead_body.replace("\n", "\n   "))
            sid = row["customer_id"]
            # CONFIRMED keeps Draft nudge (post-confirm messaging: boarding
            # details, thank-yous, upsells, re-engagement) but drops Snooze
            # (no auto-nudges to suppress on a confirmed booking) and
            # Disregard (already-won deals don't need closing).
            if label_key == "CONFIRMED":
                kb = [[
                    {"text": "💬 Draft message", "callback_data": f"nudge:{sid}"},
                    {"text": "ℹ️ Info",          "callback_data": f"inf:{sid}"},
                ]]
            else:
                # 2 rows of 2 — keeps the keyboard scannable. Disregard is
                # the destructive action, parked alone on row 2 next to Info
                # so the operator doesn't fat-finger it next to Draft nudge.
                # AWAITING_REPLY leads owe a direct answer, so the primary verb
                # is "Draft reply", not "nudge" (same callback/draft path).
                _draft_txt = ("✍️ Draft reply"
                              if label_key == "AWAITING_REPLY"
                              else "💬 Draft nudge")
                kb = [
                    [
                        {"text": _draft_txt,
                         "callback_data": f"nudge:{sid}"},
                        {"text": "💤 Snooze 4h",
                         "callback_data": f"snz:{sid}:4h"},
                    ],
                    [
                        {"text": "ℹ️ Info",
                         "callback_data": f"inf:{sid}"},
                        {"text": "🛑 Disregard",
                         "callback_data": f"disregard:{sid}"},
                    ],
                ]
            keyboards.append(kb[0])
            # Per-lead card: section emoji prefix + lead body, plus its own kb.
            per_lead_messages.append({
                "text": f"{sect['emoji']} *{label_key}*\n{lead_body}",
                "inline_keyboard": kb,
                "customer_id": sid,
            })
        if overflow > 0:
            lines.append(
                f"   _+{overflow} more — `/review {label_key.lower()}` to see all_"
            )
        lines.append("")

    if pause_tail:
        lines.append(f"⏸ Paused/snoozed: {len(pause_tail)} — `/info` to see.")

    telegram_text = "\n".join(lines).strip()
    header_text = "\n".join(header_lines).strip()
    return {
        "telegram_text": telegram_text,
        "inline_keyboards": keyboards,
        "header_text": header_text,
        "per_lead_messages": per_lead_messages,
        "mark_seen_ids": seen_ids,
    }



def _name_fallback(customer_id):
    """Build a recognisable label from customer_id when the customer's name
    is missing. Shows the last 4 digits prefixed with '…'. No country-code
    guessing — @lid IDs aren't real E.164 numbers, and even @c.us IDs have
    variable-length CCs that would mislabel."""
    cid = (customer_id or "").strip()
    digits = "".join(c for c in cid.split("@")[0] if c.isdigit())
    if len(digits) < 4:
        return "(unknown)"
    return "…" + digits[-4:]

def _why_line(row, label_key):
    """Heuristic one-liner. Prefers Hermes' suggested_action when present
    for CONFIRMED + WAITING_FOR_PAYMENT (where the generic line — e.g.
    'share boarding details or upsell' — is often wrong because the
    operator has already shared boarding / payment link), and falls
    back to deterministic rules everywhere else."""
    # For CONFIRMED/WAITING customers, suggested_action from Hermes is
    # more accurate than the generic guidance (it knows what's already
    # been said in the chat). Skip generic notes if we have it.
    if label_key in ("CONFIRMED", "WAITING_FOR_PAYMENT"):
        sug = (row.get("suggested_action") or "").strip()
        if sug:
            return sug
    notes = []
    if row.get("last_payment_link_at_seconds") is not None:
        plink_h = int(row["last_payment_link_at_seconds"] / 3600)
        if row.get("last_payment_promised_at_seconds") is None and plink_h > 24:
            notes.append(f"payment link sent {plink_h}h ago, no commitment")
    if row.get("last_booking_intent_at") and label_key == "COLD":
        notes.append("almost-bought (hit booking intent before going cold)")
    if (row.get("last_rejection_kind") == "rejected_price"
            and label_key == "COLD"):
        notes.append("lost on price — try different angle")
    if (row.get("last_rejection_kind") == "rejected_timing"
            and label_key == "COLD"):
        notes.append("lost on timing — date may be relevant now")
    if not notes:
        if label_key == "HOT":
            notes.append("hot signals — push toward booking")
        elif label_key == "NEEDS_ATTENTION":
            notes.append("we owe a reply")
        elif label_key == "WARM":
            notes.append("engaged — value-add nudge could move it")
        elif label_key == "COLD":
            notes.append("worth a soft re-engagement message")
        elif label_key == "WAITING_FOR_PAYMENT":
            notes.append("payment link sent — check if paid or nudge")
        elif label_key == "CONFIRMED":
            notes.append("booked / paid — share boarding details or upsell")
        elif label_key == "NEW":
            notes.append("new conversation")
        else:
            notes.append(label_key.lower().replace("_", " "))
    return " · ".join(notes)
