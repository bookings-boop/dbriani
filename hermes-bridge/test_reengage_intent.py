#!/usr/bin/env python3
"""_is_reengage_enquiry — refines audit #3 (2026-06-07). A previously-LOST/
DISREGARDED customer who sends a GENUINE fresh booking enquiry must REOPEN
(returning customers were being buried); a stray inbound (bare number, 'thanks',
emoji) must NOT reopen a terminal lead.

Run: python3 hermes-bridge/test_reengage_intent.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from labels import _is_operator_close, _is_reengage_enquiry  # noqa: E402


def test_genuine_enquiries_reopen():
    for m in ("Hi Dubriani, I'd like to check availability for Saturday",
              "do you have anything this weekend for 8 guests?",
              "what's the price for the Bliss 55 tomorrow?",
              "still interested — can we book for my birthday?",
              "I'd like to pay the deposit now"):
        assert _is_reengage_enquiry(m) is True, m


def test_stray_inbound_does_not_reopen():
    for m in ("reach me on 0501234567",   # the audit #3 stray bare-number case
              "okay thank you",
              "thanks",
              "ok",
              "👍",
              "0509876543"):
        assert _is_reengage_enquiry(m) is False, m


def test_none_and_empty_safe():
    assert _is_reengage_enquiry(None) is False
    assert _is_reengage_enquiry("") is False


# ---- fix-group 2 (b): over-match — stray soft tokens must NOT reopen --------
def test_overmatch_soft_tokens_do_not_reopen():
    # post-event thank-yous / birthday wishes (party|birthday|anniversary are
    # soft now — only count adjacent to a booking term)
    for m in ("thanks for the party it was great",
              "happy birthday!",
              "great party vibes for my bday",
              "happy anniversary to you both"):
        assert _is_reengage_enquiry(m) is False, m


def test_overmatch_payment_and_vendor_spam_do_not_reopen():
    # 'pay' word-bounded (paypal/payment must NOT trip it); vendor 'rates'/
    # 'interested' pitches are soft with no booking term → no reopen.
    for m in ("send payment via paypal",
              "you can pay with paypal",
              "are you interested in our SEO services? great rates",
              "we offer the best rates, interested?"):
        assert _is_reengage_enquiry(m) is False, m


# ---- fix-group 2 (b): a decline / booked-elsewhere must NEVER reopen ---------
def test_decline_never_reopens():
    # even when the decline carries a booking word ('we already BOOKED elsewhere')
    for m in ("no thanks, we're all set",
              "not interested",
              "we already booked elsewhere",
              "we booked already, thanks",
              "found another company",
              "changed my mind",
              "no longer interested"):
        assert _is_reengage_enquiry(m) is False, m


# ---- fix-group 2 (c): under-match — genuine returning idioms SHOULD reopen ---
def test_undermatch_returning_idioms_reopen():
    for m in ("are you free Saturday?",
              "are you free on Friday",
              "I want to come back next month",
              "same as last time please",
              "can I book again?",
              "free this Sunday?"):
        assert _is_reengage_enquiry(m) is True, m


# ---- fix-group 2 (a): operator-close detection (pure) -----------------------
def test_is_operator_close():
    for cb in ("operator", "operator:disregard_button",
               "operator:disregard_force", "OPERATOR"):
        assert _is_operator_close(cb) is True, cb
    # system / analyzer / cron closes are reopenable; unknown fails OPEN (system)
    for cb in ("system", "system:reengage", "cron-dormancy",
               "auto:analyzer_close", "", None):
        assert _is_operator_close(cb) is False, cb


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} reengage-intent tests passed")
