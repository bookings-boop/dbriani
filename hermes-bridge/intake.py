"""intake.py — silent-intake-drop detector (pure; unit-tested in
test_intake_gap.py).

A lead can reach WAHA but never land in customer_facts when the WAHA->n8n
webhook misses the message (outage / container-IP-drift window). intake_gaps()
cross-references a WAHA chat overview against the set of ingested customer_ids
and returns recent UNANSWERED inbound 1:1 chats we have no record of, so the
cron (cron-intake-gap.py) can alert the operator to /refresh them. No DB / no
network here — all I/O lives in the cron wrapper."""


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


def intake_gaps(chats, cf_ids, now_ts, max_age_days=7):
    """Return recent unanswered inbound 1:1 chats absent from customer_facts.

    Each result: {cid, name, body, age_days}. Excludes already-ingested cids,
    fromMe-last chats (we replied), groups/status/broadcast, and anything older
    than max_age_days. Sorted most-recent first."""
    out = []
    cf = set(cf_ids or ())
    for c in (chats or []):
        cid = _chat_id(c)
        if not cid:
            continue
        low = cid.lower()
        if "@g.us" in low or "status@" in low or "broadcast" in low:
            continue
        if cid in cf:
            continue
        lm = c.get("lastMessage") or {}
        from_me = lm.get("fromMe")
        if from_me is None:
            from_me = (lm.get("_data") or {}).get("fromMe")
        if from_me:
            continue
        ts = _norm_ts(c.get("conversationTimestamp") or lm.get("timestamp") or 0)
        age = (now_ts - ts) / 86400.0 if ts else 1e9
        if age > max_age_days:
            continue
        name = (lm.get("notifyName")
                or (lm.get("_data") or {}).get("notifyName") or "")
        body = " ".join((lm.get("body") or "").split())
        out.append({"cid": cid, "name": name, "body": body[:80],
                    "age_days": round(age, 1)})
    out.sort(key=lambda d: d["age_days"])
    return out
