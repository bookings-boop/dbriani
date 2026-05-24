#!/usr/bin/env python3
"""Unit tests for _md_escape() — Telegram-Markdown-v1 safe escaping.

Hermes/customer text routinely contains '_', '*', '`', '[', ']' as
real characters ('keep_open', 'apple_pay', snake_case identifiers,
code fences). Telegram interprets these as entity-span delimiters;
an unmatched one returns 400 'Can't parse entities' and the operator
sees nothing.

Today's regression: bare 'keep_open' in the [🛑 Close anyway] override
text broke the disregard confirmation message.

Plain-assert style. Run: python3 hermes-bridge/test_md_escape.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from server import _md_escape  # noqa: E402


def test_none_returns_empty_string():
    # Callers should be able to escape unconditionally without
    # None-checking first.
    assert _md_escape(None) == ""


def test_empty_string_returns_empty():
    assert _md_escape("") == ""


def test_underscore_escaped():
    # The original regression — bare 'keep_open' / 'apple_pay' /
    # 'snake_case' broke Markdown parse.
    assert _md_escape("keep_open") == "keep\\_open"
    assert _md_escape("apple_pay") == "apple\\_pay"
    assert _md_escape("snake_case_word") == "snake\\_case\\_word"


def test_asterisk_escaped():
    # Hermes reasoning can contain '*highlighted*' phrases — same
    # entity-span risk.
    assert _md_escape("really *important*") == "really \\*important\\*"


def test_backtick_escaped():
    # Avoids accidental `code` spans from customer text.
    assert _md_escape("type `/help`") == "type \\`/help\\`"


def test_brackets_escaped():
    # Markdown link syntax — unclosed [bracket] otherwise opens a
    # link entity.
    assert _md_escape("[draft] notes") == "\\[draft\\] notes"


def test_backslash_escaped_first():
    # Escape order matters: backslash must be doubled BEFORE other
    # escapes are added, or the literal 'foo\bar' becomes 'foo\bar'
    # (one extra backslash from a subsequent rule).
    assert _md_escape("foo\\bar") == "foo\\\\bar"


def test_round_trip_safe_chars():
    # Plain text (no metachars) round-trips identically.
    assert _md_escape("hello world") == "hello world"
    assert _md_escape("Mark Hassan") == "Mark Hassan"
    assert _md_escape("AED 3000") == "AED 3000"


def test_non_string_coerced():
    # Defensive: callers may pass ints or None-likes.
    assert _md_escape(42) == "42"


def test_combined_chars_keep_open_full_context():
    # The literal text shape that broke today: Markdown payload built
    # around the dynamic 'keep_open' string. After escape, Telegram's
    # Markdown parser sees no unmatched underscore entity.
    raw = "closed despite Hermes saying keep_open"
    escaped = _md_escape(raw)
    # Underscore is escaped; the rest is unchanged.
    assert "keep\\_open" in escaped
    assert "\\_" in escaped


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} md_escape tests passed")
