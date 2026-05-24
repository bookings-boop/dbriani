#!/usr/bin/env python3
"""db.py — Postgres + Redis primitives via docker exec.

We don't use a real DB driver (psycopg2 / redis-py) because the
bridge runs as a sudo-free user-systemd unit and the n8n stack
already exposes both containers locally — shelling into them is
deterministic, requires no native deps, and zero credential
plumbing (psql connects via trust auth on the postgres socket
inside the container; redis-cli connects on localhost).

Trade-off: each call pays a docker-exec cost (~50-150ms). For the
bridge's volume (a few hundred calls/hour) this is acceptable. If
that ever changes, swap to real drivers — only this module needs
touching.
"""
import os
import subprocess

from util import log  # noqa: F401 — re-exported for compat


# --- container + credential config -----------------------------------
PG_CONTAINER = os.environ.get("BRIDGE_PG_CONTAINER", "n8n-postgres-1")
PG_USER = os.environ.get("BRIDGE_PG_USER", "hermes_rw")
PG_DB = os.environ.get("BRIDGE_PG_DB", "n8n")
REDIS_CONTAINER = os.environ.get("BRIDGE_REDIS_CONTAINER", "n8n-redis-1")


def _psql(sql, timeout=12):
    """Run one SQL statement via docker exec; return (stdout, err_or_None).

    Caller is responsible for SQL escaping — use _lit() for any
    user/customer-derived string interpolation. We don't parameterize
    because psql -c takes a single SQL string, not a parameterized
    statement."""
    r = subprocess.run(
        ["docker", "exec", PG_CONTAINER, "psql", "-U", PG_USER, "-d", PG_DB,
         "-tA", "-c", sql],
        capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        return None, r.stderr.strip()[:200]
    return r.stdout, None


def _redis(args, timeout=8):
    """Run one redis-cli command via docker exec; return
    (stdout, err_or_None). Args list maps directly to redis-cli argv
    so callers can pass any redis command + its args."""
    r = subprocess.run(
        ["docker", "exec", REDIS_CONTAINER, "redis-cli"] + list(args),
        capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        return None, r.stderr.strip()[:200]
    return r.stdout, None


def _lit(v):
    """SQL string literal (or NULL) — escapes single quotes by
    doubling them (Postgres standard). None and empty string both
    become NULL so callers can pass through optional fields without
    a None-check first."""
    if v is None or v == "":
        return "NULL"
    return "'" + str(v).replace("'", "''") + "'"
