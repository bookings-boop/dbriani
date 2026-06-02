#!/usr/bin/env python3
"""High-salience WhatsApp STYLE directive (2026-06-02).

The operator has repeatedly corrected the same draft-formatting mistakes (hype
opener, wrong price format, missing yacht URL, wall-of-text, re-asking known
facts). Those corrections live in behavior_rules but get diluted among 40+ rules
in a 70KB prompt and under-followed. STYLE_DIRECTIVE pins the worst offenders at
high salience in behavioral_context().formatted (same lever as NO_INVENT).

Run: python3 hermes-bridge/test_style_directive.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402


def test_style_directive_reaches_drafter():
    orig = server._psql
    server._psql = lambda *a, **k: ("", None)  # offline: no DB rules/notes
    try:
        f = server.behavioral_context("971500000000@c.us")["formatted"]
    finally:
        server._psql = orig
    assert server.STYLE_DIRECTIVE in f, \
        "STYLE_DIRECTIVE missing from behavioral_context().formatted"


def test_style_directive_covers_known_violations():
    d = server.STYLE_DIRECTIVE.lower()
    assert "aed" in d                       # price-format rule
    assert "dubriani.com/yacht" in d        # yacht-URL rule
    assert "hype" in d                      # no hype opener
    assert "scannable" in d or "short" in d  # not a wall of text
    assert "already" in d                   # never re-ask known facts


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} style-directive tests passed")
