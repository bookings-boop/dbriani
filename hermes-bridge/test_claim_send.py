#!/usr/bin/env python3
"""Regression test for the handle_queue `_redis` shadow (2026-06-01 incident).

Commit 983861b added `from db import _redis` INSIDE handle_queue (the new
'awaiting' action). A function-local import makes `_redis` a local for the
WHOLE function, so the 'claim-send' action (which references the module-level
_redis before that line) raised:

    UnboundLocalError: cannot access local variable '_redis'

Every send-claim crashed → send-race protection dead, malformed bridge
responses (n8n shipped a literal `false` to customers), and draft supersede
broke (stale "ready to send" cards survived next to already-sent ones).

The fix: drop the redundant local import (module-level `_redis` is imported at
routes.py top). This test asserts `_redis` is NOT a function-local of
handle_queue, which is redis-free, instant, and catches any re-introduction.

Run: python3 hermes-bridge/test_claim_send.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import routes  # noqa: E402


def test_redis_not_shadowed_in_handle_queue():
    locals_ = routes.handle_queue.__code__.co_varnames
    assert "_redis" not in locals_, (
        "_redis is a function-local in handle_queue (a `from db import _redis` "
        "shadow) — the claim-send path will UnboundLocalError on every send")


def test_redis_is_a_module_global():
    # positive: _redis resolves to the module-level import (a free/global name)
    assert "_redis" in routes.handle_queue.__code__.co_names


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} claim-send shadow tests passed")
