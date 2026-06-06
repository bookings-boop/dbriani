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


def test_style_directive_has_multi_option_layout_rule():
    # Rule 1 (2026-06-04): listing 2+ options must be a scannable layout — each
    # option in its own message bubble (or own line), never a crammed paragraph;
    # short replies stay conversational.
    d = server.STYLE_DIRECTIVE.lower()
    assert "own message bubble" in d
    assert "own line" in d
    assert "2+" in d or "more than one option" in d
    assert "comma-separate" in d or "never inline" in d  # no crammed paragraph
    assert "short reply" in d or "do not force" in d      # don't over-structure


def test_tone_directive_reaches_drafter():
    orig = server._psql
    server._psql = lambda *a, **k: ("", None)  # offline: no DB rules/notes
    try:
        f = server.behavioral_context("971500000000@c.us")["formatted"]
    finally:
        server._psql = orig
    assert server.TONE_DIRECTIVE in f, \
        "TONE_DIRECTIVE missing from behavioral_context().formatted"
    t = server.TONE_DIRECTIVE.lower()
    assert "concierge" in t and ("five-star" in t or "5-star" in t)
    assert "maria" in t          # must preserve her existing voice


def test_quality_query_includes_style_and_tone():
    # Scorer alignment (2026-06-06): the SCORER must judge against the SAME
    # STYLE/TONE directives the drafter got — otherwise a well-formatted draft is
    # penalised against a bar the scorer never saw and clusters at 6/10.
    # build_quality_query must embed them even with NO system_prompt + no DB.
    orig = server._psql
    server._psql = lambda *a, **k: ("", None)  # offline: no DB rules/lead row
    try:
        q = server.build_quality_query({
            "system_prompt": "TEST BASE PROMPT",   # avoid the file fallback
            "incoming_message": "hi, any availability Saturday?",
            "customer_name": "Test",
            "history": "",
            "current_draft": "Sure — here are two options...",
            "customer_id": "",   # empty → skip the DB lead-analysis block
        })
    finally:
        server._psql = orig
    assert server.STYLE_DIRECTIVE in q, "scorer query missing STYLE_DIRECTIVE"
    assert server.TONE_DIRECTIVE in q, "scorer query missing TONE_DIRECTIVE"


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} style-directive tests passed")
