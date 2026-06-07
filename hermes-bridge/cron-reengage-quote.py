#!/usr/bin/env python3
"""cron-reengage-quote.py — trigger the proactive ghost-recovery follow-up sweep.

THIN TRIGGER (2026-06-07 rearchitecture): all intelligence lives in the bridge's
native engine. This cron only fires POST /followup-sweep on a schedule; the
bridge then scans quoted-but-silent leads, drafts each with FULL chat context +
the verified Voss ghost-recovery phrasing, runs the exclusion guard, persists the
draft, and self-posts a one-tap ✅ Send card to the operator's Telegram.
APPROVAL-FIRST: nothing reaches a customer until the operator taps Send.

Modes (REENGAGE_MODE in ~/hermes-bridge/.env):
  shadow   (default) -> POST dry_run:true  — preview candidates, post NOTHING.
  operator           -> POST dry_run:(REENGAGE_CONFIRM!=yes) — real cards only
                        when REENGAGE_CONFIRM=yes; otherwise still a dry-run.
Quiet hours 22:00-08:00 Asia/Dubai are skipped (override REENGAGE_IGNORE_QUIET=1).
REENGAGE_DAILY_CAP -> per-run `limit` (the bridge's scan already caps the pool).

Stdlib only.
"""
import datetime
import json
import os
import sys
import urllib.request

HOME = os.path.expanduser("~")
ENV = {}
try:
    for ln in open(os.path.join(HOME, "hermes-bridge", ".env")):
        ln = ln.strip()
        if "=" in ln and not ln.startswith("#"):
            k, v = ln.split("=", 1)
            ENV[k] = v
except FileNotFoundError:
    pass


def getenv(k, d=None):
    v = os.environ.get(k)
    return v if (v is not None and v != "") else ENV.get(k, d)


PORT = getenv("BRIDGE_PORT", "8788")
TOKEN = getenv("BRIDGE_TOKEN", "")
MODE = (getenv("REENGAGE_MODE") or "shadow").strip().lower()
CONFIRM = (getenv("REENGAGE_CONFIRM") or "").strip().lower() == "yes"
IGNORE_QUIET = (getenv("REENGAGE_IGNORE_QUIET") or "").strip().lower() in ("1", "yes", "true")
try:
    # per-RUN cap (the bridge's scan + 48h cooldown bound the real cadence)
    LIMIT = int(getenv("REENGAGE_DAILY_CAP", "5"))
except (TypeError, ValueError):
    LIMIT = 5
TS = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def out(*a):
    print(TS, *a, flush=True)


def quiet_hours():
    if IGNORE_QUIET:
        return False
    dubai = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=4)
    return dubai.hour >= 22 or dubai.hour < 8


def main():
    if MODE not in ("shadow", "operator"):
        out(f"ABORT: REENGAGE_MODE={MODE!r} — use 'shadow' or 'operator'.")
        sys.exit(1)
    if not TOKEN:
        out("ABORT: BRIDGE_TOKEN not set in ~/hermes-bridge/.env")
        sys.exit(1)
    if quiet_hours():
        out("quiet hours (Dubai) — skipping; picks up after 08:00")
        return
    dry = not (MODE == "operator" and CONFIRM)
    body = json.dumps({"dry_run": dry, "limit": LIMIT}).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}/followup-sweep", data=body,
        headers={"Content-Type": "application/json", "X-Bridge-Token": TOKEN},
        method="POST")
    try:
        # generous timeout: a real run does up to `limit` sequential Hermes
        # drafts; matches the server-side sweep lock window.
        with urllib.request.urlopen(req, timeout=600) as resp:
            r = json.load(resp)
    except Exception as e:  # noqa: BLE001
        out("followup-sweep call returned no response (may still be running "
            "server-side; lock prevents a concurrent run):", repr(e))
        sys.exit(0)
    label = "DRY-RUN" if dry else "LIVE"
    if dry:
        cands = r.get("candidates") or []
        out(f"{label} — {len(cands)} candidate(s) eligible "
            f"(limit {LIMIT}); posted nothing.")
        for c in cands[:25]:
            who = c.get("name") or c.get("customer_id", "")
            out(f"  [{c.get('window')}] {who} ({c.get('label')}, "
                f"silent {c.get('silence_hours')}h) :: {c.get('phrase_preview', '')}")
    else:
        out(f"{label} — posted {r.get('posted', 0)} card(s); "
            f"skipped_excluded {r.get('skipped_excluded', 0)}; "
            f"skipped_error {r.get('skipped_error', 0)}. "
            "Operator taps ✅ Send in Telegram to deliver.")
    if not r.get("ok", False) and not r.get("skipped"):
        out("WARN: endpoint returned ok=false:", str(r)[:200])


if __name__ == "__main__":
    main()
