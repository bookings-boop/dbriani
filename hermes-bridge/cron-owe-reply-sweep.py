#!/usr/bin/env python3
"""cron-owe-reply-sweep.py — trigger the proactive UNANSWERED-customer sweep.

THIN TRIGGER: all intelligence lives in the bridge. This cron only fires
POST /owe-reply-sweep on a schedule; the bridge then finds every lead where WE
owe a reply (the customer messaged after our last outbound), drafts a DIRECT
reply with full chat context, resolves the real phone (so @lid leads are
identifiable), runs the exclusion guard, and self-posts a one-tap ✅ Send card
to the operator's Telegram showing the wait time + situation + draft.
APPROVAL-FIRST: nothing reaches a customer until the operator taps Send.

This REPLACES the 2x/day batch /review push as the way owed leads surface —
runs frequently (*/15) so a freshly-unanswered customer appears within minutes,
and the bridge's owe-claim TTL (OWE_RECARD_TTL, default 4h) re-surfaces a
still-unanswered lead instead of spamming every run.

Modes (OWE_REPLY_MODE in ~/hermes-bridge/.env):
  shadow   (default) -> POST dry_run:true  — preview owed leads, post NOTHING.
  operator           -> POST dry_run:(OWE_REPLY_CONFIRM!=yes) — real cards only
                        when OWE_REPLY_CONFIRM=yes; otherwise still a dry-run.
Quiet hours 22:00-08:00 Asia/Dubai are skipped (override OWE_REPLY_IGNORE_QUIET=1)
so the operator isn't pinged overnight; owed leads surface at 08:00.

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
MODE = (getenv("OWE_REPLY_MODE") or "shadow").strip().lower()
CONFIRM = (getenv("OWE_REPLY_CONFIRM") or "").strip().lower() == "yes"
IGNORE_QUIET = (getenv("OWE_REPLY_IGNORE_QUIET") or "").strip().lower() in (
    "1", "yes", "true")
try:
    LIMIT = int(getenv("OWE_SWEEP_CAP", "8"))  # per-run cap (shared with bridge)
except (TypeError, ValueError):
    LIMIT = 8
TS = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def out(*a):
    print(TS, *a, flush=True)


def quiet_hours():
    if IGNORE_QUIET:
        return False
    dubai = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
        hours=4)
    return dubai.hour >= 22 or dubai.hour < 8


def main():
    if MODE not in ("shadow", "operator"):
        out(f"ABORT: OWE_REPLY_MODE={MODE!r} — use 'shadow' or 'operator'.")
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
        f"http://127.0.0.1:{PORT}/owe-reply-sweep", data=body,
        headers={"Content-Type": "application/json", "X-Bridge-Token": TOKEN},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            r = json.load(resp)
    except Exception as e:  # noqa: BLE001
        out("owe-reply-sweep call returned no response (may still be running "
            "server-side; lock prevents a concurrent run):", repr(e))
        sys.exit(0)
    label = "DRY-RUN" if dry else "LIVE"
    if dry:
        cands = r.get("candidates") or []
        out(f"{label} — {r.get('eligible', len(cands))} owed lead(s) eligible "
            f"(showing {len(cands)}, cap {LIMIT}); posted nothing.")
        for c in cands[:25]:
            who = c.get("name") or c.get("customer_id", "")
            out(f"  {who} ({c.get('label')}, unanswered "
                f"{c.get('owe_hours')}h)")
    else:
        out(f"{label} — posted {r.get('posted', 0)} card(s) of "
            f"{r.get('eligible', 0)} owed; skipped_excluded "
            f"{r.get('skipped_excluded', 0)}; skipped_error "
            f"{r.get('skipped_error', 0)}. Operator taps ✅ Send to deliver.")
    if not r.get("ok", False) and not r.get("skipped"):
        out("WARN: endpoint returned ok=false:", str(r)[:200])


if __name__ == "__main__":
    main()
