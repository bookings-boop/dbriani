#!/usr/bin/env python3
"""canonicalize_cid cycle/convergence safety (2026-06-04, QC A6).

A merged_into loop (a->b->a) previously resolved to a half-way intermediate
with no signal. Now a visited-set detects the cycle, logs a WARNING, and returns
the INPUT cid (treated as canonical) — observable, never a silent wrong target.

Run: python3 hermes-bridge/test_canonicalize_cycle.py
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402


def _fake_psql(chain):
    def fake(sql, **k):
        m = re.search(r"customer_id = '([^']*)'", sql)
        cid = m.group(1) if m else ""
        return (chain.get(cid, ""), None)  # '' = merged_into NULL / no row
    return fake


def _canon(chain, cid):
    orig = server._psql
    server._psql = _fake_psql(chain)
    try:
        return server.canonicalize_cid(cid)
    finally:
        server._psql = orig


def test_cycle_returns_input_not_intermediate():
    out = _canon({"a@c.us": "b@c.us", "b@c.us": "a@c.us"}, "a@c.us")
    assert out == "a@c.us"


def test_one_hop_resolves_to_canonical():
    assert _canon({"x@lid": "y@c.us"}, "x@lid") == "y@c.us"


def test_no_row_returns_input():
    assert _canon({}, "z@c.us") == "z@c.us"


def test_multi_hop_chain_resolves():
    assert _canon({"a": "b", "b": "c"}, "a") == "c"


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} canonicalize-cycle tests passed")
