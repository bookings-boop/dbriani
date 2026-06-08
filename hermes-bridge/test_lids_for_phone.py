#!/usr/bin/env python3
"""Unit tests for waha.lids_for_phone() — phone -> @lid resolution via WAHA's
lid->phone map (the keystone for finding @lid leads by phone; Phase-2 #1).

The map is {'<lid>@lid': '<phone-digits>'}. Given a normalized phone, return the
matching @lid cid(s): exact, or a country-code-tolerant suffix match (diff <=4).
Pure; None-safe; returns a list (0/1 normally, >1 = ambiguous for the caller).

Run: python3 hermes-bridge/test_lids_for_phone.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from waha import lids_for_phone as L  # noqa: E402

M = {"104711420166164@lid": "971506798997",
     "31890199318638@lid": "971588211789",
     "999@lid": "971111111111"}


def test_exact_match():
    assert L(M, "971506798997") == ["104711420166164@lid"]


def test_country_code_tolerant_either_direction():
    assert L(M, "506798997") == ["104711420166164@lid"]          # query missing 971
    assert L({"A@lid": "506798997"}, "971506798997") == ["A@lid"]  # map missing 971


def test_no_false_match():
    assert L(M, "971222333444") == []


def test_ambiguous_returns_all():
    r = L({"A@lid": "971500000000", "B@lid": "971500000000"}, "971500000000")
    assert set(r) == {"A@lid", "B@lid"}


def test_short_query_ignored():
    assert L(M, "12345") == []          # < 7 digits


def test_none_empty_safe():
    assert L(None, "971506798997") == []
    assert L({}, "971506798997") == []
    assert L(M, "") == []
    assert L(M, None) == []


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} lids_for_phone tests passed")
