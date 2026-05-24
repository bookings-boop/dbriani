#!/usr/bin/env python3
"""util.py — leaf-level shared helpers. No bridge imports; nothing
imports from us upward except db.py. Anything that's used everywhere
and has no domain meaning lives here."""
import os
import time


def log(*a):
    """Single-line timestamped log. Uses ISO-8601 second precision so
    journalctl --since works cleanly. flush=True because systemd
    captures stdout via PIPE — without flushing, log lines bunch up at
    process exit and post-mortems lose ordering."""
    print(time.strftime("%Y-%m-%dT%H:%M:%S"), *a, flush=True)


def _envflag(key, default):
    """Truthy env-var helper. 'true', '1', 'yes' (case-insensitive,
    whitespace-stripped) -> True. Anything else -> False. The default
    is itself parsed by the same rule so callers pass strings, not
    bools."""
    return os.environ.get(key, default).strip().lower() in ("true", "1", "yes")
