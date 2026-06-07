"""intake.py — silent-intake-drop detector (pure; unit-tested in
test_intake_gap.py).

A lead can reach WAHA but never land in customer_facts when the WAHA->n8n
webhook misses the message (outage / container-IP-drift window) OR the n8n
draft pipeline drops it (e.g. the Claude node 400s on a credit-balance error
and its error branch skips the customer_facts upsert — RCA 2026-06-07).
intake_gaps() cross-references a WAHA chat overview against the set of ingested
customer_ids and returns UNANSWERED inbound 1:1 chats we have no record of, so
the cron (cron-intake-gap.py) can AUTO-INGEST them via the existing recovery
path and post a one-tap recovery card. No DB / no network here — all I/O lives
in the cron wrapper.

NEVER-MISS hardening (2026-06-07):
  * `canon` — a caller-supplied identity resolver applied to BOTH sides of the
    membership test so an @lid chat that was ingested under its @c.us identity
    (or vice-versa) is NOT a false gap. Pure: the cron injects a resolver built
    from WAHA's lid->phone map; the default normalises phone digits only.
  * `max_age_days=None` — NO age cap. A never-ingested lead must never age out
    of detection (the old 7-day cap let a buried drop become permanently
    invisible). The cron uses None for the absent-from-customer_facts case.
"""


def _chat_id(c):
    cid = c.get("id")
    if isinstance(cid, dict):
        cid = cid.get("_serialized") or cid.get("id")
    return str(cid or "")


def _norm_ts(v):
    """WAHA timestamps come as seconds OR milliseconds — normalise to seconds."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return 0.0
    return v / 1000.0 if v > 1e12 else v


def _norm_digits(digits):
    """Mirror hermes_exclusion_guards.normalize_phone digit rules: drop a
    leading international '00', and lift UAE national formats to a 971 country
    code (0xxxxxxxxx -> 971xxxxxxxxx; 5xxxxxxxx -> 9715xxxxxxxx)."""
    if not digits:
        return ""
    if digits.startswith("00"):
        digits = digits[2:]
    if len(digits) == 10 and digits.startswith("0"):
        digits = "971" + digits[1:]
    elif len(digits) == 9 and digits.startswith("5"):
        digits = "971" + digits
    return digits


def canon_phone(cid, lid_map=None):
    """Pure identity canonicaliser for the absent-check.

    Reduces a WAHA/customer_facts cid to a single canonical key so the two
    WhatsApp identity domains line up:
      * '<digits>@c.us' -> normalised phone digits.
      * '<lid>@lid'     -> resolved phone digits via `lid_map` (a
        {'<lid>@lid': '<digits>'} dict, e.g. from WAHA's bulk lids endpoint);
        when the lid is UNRESOLVED it falls through to the lowercased raw id so
        a possibly-dropped @lid lead stays DISTINCT and is still flagged
        (fail-toward-flagging — we'd rather over-alert than miss a lead).
      * anything else    -> its own digits, else the lowercased raw id.
    None-safe; never raises."""
    s = str(cid or "").strip()
    if not s:
        return ""
    low = s.lower()
    if low.endswith("@lid"):
        mapped = (lid_map or {}).get(s) or (lid_map or {}).get(low) or ""
        d = _norm_digits("".join(ch for ch in str(mapped) if ch.isdigit()))
        return d or low
    if low.endswith("@c.us"):
        d = _norm_digits("".join(ch for ch in low.split("@", 1)[0]
                                 if ch.isdigit()))
        return d or low
    d = _norm_digits("".join(ch for ch in low if ch.isdigit()))
    return d or low


def intake_gaps(chats, cf_ids, now_ts, max_age_days=7, canon=None):
    """Return unanswered inbound 1:1 chats absent from customer_facts.

    Each result: {cid, name, body, age_days}. The raw WAHA cid is preserved in
    the result (so the cron can /refresh it) while a CANONICALISED key is used
    for the membership test. Excludes already-ingested cids (canonical match),
    fromMe-last chats (we replied), and groups/status/broadcast.

    max_age_days:
      * a number  -> skip chats older than that many days (legacy alert window).
      * None       -> NO age cap; a never-ingested lead never ages out (the
        cron's bulletproof default). age_days is still computed for the card.

    canon: cid -> canonical str, applied to BOTH cf_ids and each chat cid.
      Defaults to phone-digit normalisation (canon_phone w/o a lid map). The
      cron injects a lid-aware resolver to fold @lid<->@c.us identities.
      Sorted most-recent first."""
    if canon is None:
        canon = canon_phone
    out = []
    cf = set()
    for x in (cf_ids or ()):
        if not x:
            continue
        try:
            cf.add(canon(x))
        except Exception:
            cf.add(str(x))
    for c in (chats or []):
        cid = _chat_id(c)
        if not cid:
            continue
        low = cid.lower()
        if "@g.us" in low or "status@" in low or "broadcast" in low:
            continue
        try:
            key = canon(cid)
        except Exception:
            key = cid
        if key in cf:
            continue
        lm = c.get("lastMessage") or {}
        from_me = lm.get("fromMe")
        if from_me is None:
            from_me = (lm.get("_data") or {}).get("fromMe")
        if from_me:
            continue
        ts = _norm_ts(c.get("conversationTimestamp") or lm.get("timestamp") or 0)
        age = (now_ts - ts) / 86400.0 if ts else 1e9
        if max_age_days is not None and age > max_age_days:
            continue
        name = (lm.get("notifyName")
                or (lm.get("_data") or {}).get("notifyName") or "")
        body = " ".join((lm.get("body") or "").split())
        out.append({"cid": cid, "name": name, "body": body[:80],
                    "age_days": round(age, 1)})
    out.sort(key=lambda d: d["age_days"])
    return out
