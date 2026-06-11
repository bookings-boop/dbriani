#!/usr/bin/env python3
"""quote_accepted → HOT (Eva 2026-06-10: she accepted an AED 87,731 quote
with "Ok" and the ladder re-emitted multi_yacht_engaged conf=0.20 → stayed
WARM; nothing represented "customer accepted our quoted price").

Rule: inbound is a SHORT standalone affirmative (whole message) AND any of
our last 3 outbound bubbles within 48h carried a currency amount (an open
quote) → ("HOT", "quote_accepted"). Last-3 not last-1: Eva's 87,731 quote
was phone-sent and reached the durable store LATE — at eval time the newest
stored outbound had no money, the 2nd-newest ("15,000 AED/hr") did.

Run: python3 hermes-bridge/test_quote_accepted.py  (or pytest)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402
import labels  # noqa: E402

EVA = {"customer_id": "199145352634437@lid", "message_count": 10,
       "yachts": "Elise 50, Bliss 55, Zenith 64, Cabo 77, Thunder Superyacht",
       "dates": "Fri Jun 19", "label": "WARM"}


def _patch(mod, **attrs):
    old = {}
    for k, v in attrs.items():
        old[k] = getattr(mod, k)
        setattr(mod, k, v)
    return old


def _restore(mod, old):
    for k, v in old.items():
        setattr(mod, k, v)


# ---------------------------------------------------------------- regexes
def test_affirmative_re_positives():
    for m in ("Ok", "ok", "okay!", "Yes", "yep", "sure", "Deal",
              "done ✅", "Confirmed", "perfect", "sounds good",
              "let's do it", "go ahead", "book it", "👍"):
        assert labels.AFFIRMATIVE_RE.match(m.strip()), m


def test_affirmative_re_negatives():
    for m in ("ok but what time?", "I will think about it", "is it ok?",
              "ok so the thing is we might come later with more people",
              "yesterday was great", "not ok", "books are great",
              "ok 5000 aed", "surely you can discount"):
        assert not labels.AFFIRMATIVE_RE.match(m.strip()), m


def test_outbound_quote_re():
    for t in ("after discount we can do 87,731 AED for 6 hours",
              "AED 1,000 extra", "special rate — 15,000 AED/hr",
              "total is 4200 aed", "2,500 dirhams per hour"):
        assert labels.OUTBOUND_QUOTE_RE.search(t), t
    for t in ("for June 19th, how many hours were you thinking?",
              "see you at 2PM", "berth DA19", "we have 6 yachts"):
        assert not labels.OUTBOUND_QUOTE_RE.search(t), t


# ----------------------------------------------------------------- ladder
def test_eva_replay_ok_after_quote_promotes_hot():
    old = _patch(server, _recent_outbound_quote=lambda cid, **k: True,
                 _has_recent_payment_intent=lambda cid: False)
    try:
        label, sig, _ev = server.compute_label("Ok", dict(EVA))
    finally:
        _restore(server, old)
    assert (label, sig) == ("HOT", "quote_accepted"), (label, sig)


def test_ok_without_recent_quote_falls_through():
    old = _patch(server, _recent_outbound_quote=lambda cid, **k: False,
                 _has_recent_payment_intent=lambda cid: False)
    try:
        label, sig, _ev = server.compute_label("Ok", dict(EVA))
    finally:
        _restore(server, old)
    assert sig == "multi_yacht_engaged", sig  # today's (dampened) behavior


def test_non_affirmative_never_fires_even_with_quote():
    old = _patch(server, _recent_outbound_quote=lambda cid, **k: True,
                 _has_recent_payment_intent=lambda cid: False)
    try:
        for msg in ("I will think about it", "ok but what time?"):
            _l, sig, _e = server.compute_label(msg, dict(EVA))
            assert sig != "quote_accepted", (msg, sig)
    finally:
        _restore(server, old)


def test_money_inbound_keeps_money_mentioned_priority():
    old = _patch(server, _recent_outbound_quote=lambda cid, **k: True,
                 _has_recent_payment_intent=lambda cid: False)
    try:
        _l, sig, _e = server.compute_label("5000 aed works for us",
                                           dict(EVA))
    finally:
        _restore(server, old)
    assert sig == "money_mentioned", sig


def test_payment_confirmed_outranks_quote_accepted():
    # NOTE: "done ✅" does NOT match PAYMENT_CONFIRMED_RE today — the
    # trailing \b after the emoji never finds a word boundary (latent
    # pre-existing bug, queued separately). "all done" genuinely matches,
    # proving the payment-confirmed branch keeps ladder priority over
    # quote_accepted for payment-language.
    old = _patch(server,
                 _recent_outbound_quote=lambda cid, **k: True,
                 _has_recent_payment_intent=lambda cid: False,
                 _has_recent_payment_link_sent=lambda cid, hours=48: True)
    try:
        label, sig, _e = server.compute_label("all done", dict(EVA))
    finally:
        _restore(server, old)
    assert (label, sig) == ("CONFIRMED", "payment_confirmed_chat"), (label, sig)


# ----------------------------------------------------------------- helper
def test_recent_outbound_quote_reads_last3_and_fails_closed():
    calls = []

    def fake_psql(sql, timeout=12):
        calls.append(sql)
        return ("for June 19th, how many hours?\n"
                "the Thunder is showing a special rate — 15,000 AED/hr\n"
                "a few of our yachts have jacuzzis", None)

    old = _patch(server, _psql=fake_psql)
    try:
        assert server._recent_outbound_quote("199@lid") is True
        s = calls[0]
        assert "direction = 'out'" in s and "LIMIT 3" in s \
            and "conversation_messages" in s, s
    finally:
        _restore(server, old)
    # fail-CLOSED on DB error
    old = _patch(server, _psql=lambda sql, timeout=12: (None, "boom"))
    try:
        assert server._recent_outbound_quote("199@lid") is False
    finally:
        _restore(server, old)


def test_humanize_knows_quote_accepted():
    assert "quote" in server._humanize_signal(
        "auto:quote_accepted", "", "system").lower()


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn(); print("PASS", fn.__name__)
        except AssertionError as e:
            failed += 1; print("FAIL", fn.__name__, "-", e or "assert")
        except Exception as e:  # noqa: BLE001
            failed += 1; print("ERROR", fn.__name__, "-", repr(e))
    print(f"{len(fns)-failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
