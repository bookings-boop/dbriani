#!/usr/bin/env python3
"""cron-hubspot-sync.py — incremental WhatsApp -> HubSpot sync (summary + activity + linking).

Runs every ~15 min from cron. For each conversation with NEW activity since the last
high-water mark, it phone-links (or creates) the HubSpot contact and writes the fresh
relationship summary -> hermes_interaction_context, descriptive props, and a timeline
NOTE (a summary, NOT the raw transcript). Raw conversation_messages NEVER leave Postgres.

MODES:
  (no args)            dry-run of the INCREMENTAL window (reads only; safe preview)
  --execute            actually write (LIVE). No-op unless env HUBSPOT_SYNC_ENABLED=1.
  --backfill           process ALL contacts (keyset-paginated, no watermark); pair with
                       --dry-run for the one-time preview, or --execute for the backfill.
  --dry-run            force read-only (works even when the flag is OFF) — for previews.
  --limit N            incremental: batch cap. backfill: TOTAL cap (else drains fully).

SAFETY:
  - A WRITE happens only when HUBSPOT_SYNC_ENABLED=1 AND --execute AND not --dry-run.
    The crontab uses `--execute`, so the cron is a pure no-op until the flag is flipped.
  - Single-runner: an flock lockfile prevents overlapping runs (cron tick overrunning,
    or a manual backfill racing the incremental cron) from creating duplicate contacts.
  - Watermark advances ONLY across the contiguous prefix of SETTLED rows, and never
    past a row whose write errored — so a transient HubSpot outage retries, never drops.
"""
import fcntl
import json
import os
import subprocess
import sys
import time

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


# Cron runs with a bare environ; load .env into os.environ so imported modules that read
# os.environ DIRECTLY work: hubspot_sync._token() (HUBSPOT_TOKEN), waha (WAHA_API_KEY/
# WAHA_BASE), and the flags. setdefault never overrides a real process-env value.
for _k, _v in ENV.items():
    os.environ.setdefault(_k, _v)


PG = getenv("BRIDGE_PG_CONTAINER", "n8n-postgres-1")
PG_USER = getenv("BRIDGE_PG_USER", "hermes_rw")
PG_DB = getenv("BRIDGE_PG_DB", "n8n")
STATE_PATH = os.path.join(HOME, "hermes-bridge", ".hubspot-sync-state.json")
LOCK_PATH = os.path.join(HOME, "hermes-bridge", ".hubspot-sync.lock")
CREATED_LOG = os.path.join(HOME, "hermes-bridge", ".hubspot-created.log")  # JSONL audit of every CREATE (reversal trail)
PER_CONTACT_SLEEP_S = float(getenv("HUBSPOT_SYNC_SLEEP_S", "0.3") or "0.3")  # <=~3/s < HubSpot 4/s search cap
DEFAULT_LIMIT = int(getenv("HUBSPOT_SYNC_LIMIT", "500") or "500")
BACKFILL_PAGE = int(getenv("HUBSPOT_SYNC_BACKFILL_PAGE", "200") or "200")
CONTACTS_TTL_S = float(getenv("HUBSPOT_SYNC_STATE_TTL_S", str(60 * 86400)) or str(60 * 86400))
MAX_RETRIES = int(getenv("HUBSPOT_SYNC_MAX_RETRIES", "5") or "5")  # write-error -> quarantine after N

sys.path.insert(0, os.path.join(HOME, "hermes-bridge"))
import hubspot_sync  # noqa: E402


def _sync_enabled():
    """Write gate. Reads process env AND ~/hermes-bridge/.env (via getenv) — the cron runs
    with a bare environ and would NOT inherit a .env-only flag otherwise. Default OFF means
    the `--execute` crontab line is a true no-op until the flag is set."""
    return (getenv("HUBSPOT_SYNC_ENABLED", "0") or "0").strip().lower() in (
        "1", "true", "yes", "on")

# Ordered fact fields — MUST match the SELECT column order in _select().
_FIELDS = ["customer_id", "name", "label", "yachts", "booked_yacht", "dates",
           "party_size", "message_count", "importance_reasoning",
           "suggested_action", "last_customer_message_at", "updated_at"]
_FS = chr(31)


def _psql(sql, timeout=40):
    try:
        r = subprocess.run(
            ["docker", "exec", PG, "psql", "-U", PG_USER, "-d", PG_DB, "-tA", "-c", sql],
            capture_output=True, text=True, timeout=timeout)
    except Exception as e:  # noqa: BLE001
        return None, type(e).__name__ + ": " + str(e)[:160]
    if r.returncode != 0:
        return None, (r.stderr or "").strip()[:200]
    return r.stdout, None


def _now_pg():
    out, err = _psql("SELECT to_char(now(),'YYYY-MM-DD\"T\"HH24:MI:SS.USOF')")
    return None if (err or not out) else out.strip().splitlines()[0].strip()


def _sx(col):
    """SQL: coalesce + neutralize the chr(31)/chr(30) separators AND CR/LF/TAB to spaces,
    so no embedded control char can split or misalign a -tA row (this codebase is known to
    use \\x1f internally — recycled-LID RCA)."""
    return ("translate(coalesce(%s,''), chr(31)||chr(30)||chr(13)||chr(10)||chr(9), '     ')"
            % col)


def _select(where, order, limit):
    cols = (" || chr(31) || ").join([
        "cf.customer_id", _sx("cf.name"), _sx("cf.label"), _sx("cf.yachts"),
        _sx("cf.booked_yacht"), _sx("cf.dates"), _sx("cf.party_size"),
        "coalesce(cf.message_count::text,'')",
        _sx("cf.importance_reasoning"), _sx("cf.suggested_action"),
        "coalesce(to_char(cs.last_customer_message_at,'YYYY-MM-DD\"T\"HH24:MI:SS.USOF'),'')",
        "coalesce(to_char(cs.updated_at,'YYYY-MM-DD\"T\"HH24:MI:SS.USOF'),'')",
    ])
    sql = ("SELECT %s FROM customer_facts cf "
           "LEFT JOIN conversation_state cs ON cs.customer_id = cf.customer_id "
           "WHERE %s ORDER BY %s LIMIT %d" % (cols, where, order, int(limit)))
    out, err = _psql(sql)
    if err:
        return None, err
    rows = []
    for ln in (out or "").splitlines():
        if not ln.strip():
            continue
        parts = ln.split(_FS)
        if len(parts) != len(_FIELDS):
            print("hubspot-sync: WARN malformed row (%d/%d fields) skipped"
                  % (len(parts), len(_FIELDS)))
            continue
        rows.append(dict(zip(_FIELDS, parts)))
    return rows, None


def _lit(s):
    return "'" + str(s).replace("'", "''") + "'"


def _fetch_incremental(wm_eff, limit):
    # Composite keyset (updated_at, customer_id) — exact and tie-proof. A bare-timestamp
    # watermark WEDGES if >=limit rows share one updated_at (a bulk updated_at=now() touch,
    # e.g. server.mark_review_seen); the customer_id tiebreak guarantees forward progress.
    where = ("cf.merged_into IS NULL AND (cs.updated_at, cf.customer_id) > "
             "(%s::timestamptz, %s)" % (_lit(wm_eff["ts"]), _lit(wm_eff["cid"])))
    return _select(where, "cs.updated_at ASC, cf.customer_id ASC", limit)


def _fetch_backfill_page(cursor, page):
    where = "cf.merged_into IS NULL AND cf.customer_id > %s" % _lit(cursor)
    return _select(where, "cf.customer_id ASC", page)


def _load_state():
    try:
        with open(STATE_PATH) as f:
            s = json.load(f)
    except FileNotFoundError:
        return {"watermark": None, "contacts": {}}
    except ValueError:
        print("hubspot-sync: FATAL state file is corrupt JSON (%s) — refusing to run "
              "(silent watermark reset would skip a window). Inspect/restore it." % STATE_PATH)
        sys.exit(2)
    if not isinstance(s, dict):
        print("hubspot-sync: FATAL state file is not a JSON object — refusing to run.")
        sys.exit(2)
    s.setdefault("watermark", None)
    s.setdefault("contacts", {})
    if not isinstance(s["contacts"], dict):
        s["contacts"] = {}
    return s


def _save_state(state):
    try:
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, STATE_PATH)
    except Exception as e:  # noqa: BLE001
        print("hubspot-sync: WARN state save failed: %s" % e)


def _log_created(cid, phone, hid):
    """Append every newly-CREATED HubSpot contact to a JSONL audit so the backfill's
    creates are a precise reversal trail (a delete script can read this)."""
    try:
        with open(CREATED_LOG, "a") as f:
            f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                "cid": cid, "phone": phone, "id": hid}) + "\n")
    except Exception:  # noqa: BLE001
        pass


def _prune_contacts(contacts):
    cutoff = time.time() - CONTACTS_TTL_S
    return {k: v for k, v in contacts.items()
            if float((v or {}).get("last_note_ts") or 0) >= cutoff} if contacts else {}


def _is_settled(rec):
    """A row is settled (safe to advance the watermark past) when it is terminal
    (nothing to sync) OR its props wrote AND any needed note posted."""
    a = rec.get("action")
    if a in ("excluded", "no_phone", "too_short"):
        return True
    if a in ("matched", "created"):
        return bool(rec.get("wrote_props")) and not rec.get("note_failed")
    return False  # error / search-failed -> retry


def _process(f, contacts, resolver, dry, tally, phone_memo=None):
    cid = f.get("customer_id", "")
    prev = contacts.get(cid)
    rec = hubspot_sync.sync_contact(f, resolver, dry_run=dry, want_note=True, prev=prev,
                                    phone_memo=phone_memo)
    act = rec.get("action", "error")
    tally[act] = tally.get(act, 0) + 1
    if rec.get("wrote_props"):
        tally["wrote_props"] += 1
    if rec.get("degraded"):
        tally["degraded"] += 1
    if dry and rec.get("would_note"):
        tally["would_note"] += 1
    elif not dry and rec.get("wrote_note"):
        tally["wrote_note"] += 1
    if act == "error":
        tally["error_examples"].append("%s: %s" % (cid, rec.get("reason", "")[:120]))
    if not dry and act in ("matched", "created"):
        prevc = contacts.get(cid) or {}
        posted = rec.get("wrote_note")
        # M5: if a status-change note FAILED, keep prev status so it retries next run
        last_status = (prevc.get("last_status", "") if rec.get("note_failed")
                       else rec.get("status", ""))
        contacts[cid] = {
            "last_status": last_status,
            "last_note_ts": (time.time() if posted else prevc.get("last_note_ts", 0)),
            "hubspot_id": rec.get("contact_id"),
        }
        if rec.get("action") == "created" and rec.get("contact_id"):
            _log_created(cid, rec.get("phone", ""), rec["contact_id"])   # reversal trail
    time.sleep(PER_CONTACT_SLEEP_S)
    return rec


def _new_tally():
    return {"matched": 0, "created": 0, "excluded": 0, "no_phone": 0, "too_short": 0,
            "resolve_pending": 0, "error": 0, "wrote_props": 0, "wrote_note": 0,
            "would_note": 0, "degraded": 0, "error_examples": []}


def _print_tally(stamp, mode, n, flag, tally, dry):
    print("hubspot-sync %s [%s]: scope=%d flag=%s | matched=%d created=%d excluded=%d "
          "no_phone=%d too_short=%d resolve_pending=%d error=%d | props=%d degraded=%d "
          "notes_%s=%d"
          % (stamp, mode, n, "on" if flag else "off", tally["matched"], tally["created"],
             tally["excluded"], tally["no_phone"], tally["too_short"],
             tally["resolve_pending"], tally["error"], tally["wrote_props"],
             tally["degraded"], "would" if dry else "written",
             tally["would_note"] if dry else tally["wrote_note"]))
    for ex in tally["error_examples"][:10]:
        print("  ERROR " + ex)


def _resolver():
    try:
        from waha import phone_for_cid
        return phone_for_cid
    except Exception as e:  # noqa: BLE001
        print("hubspot-sync: WARN waha.phone_for_cid unavailable (%s) — @lid won't resolve"
              % type(e).__name__)
        return None


def _run_backfill(dry, total_cap):
    stamp = time.strftime("%Y-%m-%d %H:%M")
    state = _load_state()
    contacts = state.get("contacts", {})
    resolver = _resolver()
    if resolver is None and not dry:
        print("hubspot-sync %s [BACKFILL]: ABORT — WAHA resolver unavailable; @lid "
              "contacts would not resolve. Fix WAHA, then re-run." % stamp)
        return 1
    tally = _new_tally()
    phone_memo = {}   # one memo for the whole backfill -> same-phone cids never duplicate
    cursor, processed, pages = "", 0, 0
    while True:
        page = BACKFILL_PAGE
        if total_cap:
            page = min(page, total_cap - processed)
            if page <= 0:
                print("hubspot-sync: backfill TRUNCATED at --limit %d (more remain)" % total_cap)
                break
        rows, err = _fetch_backfill_page(cursor, page)
        if err is not None:
            print("hubspot-sync %s [BACKFILL]: FACTS read failed: %s" % (stamp, err))
            break
        if not rows:
            break
        for f in rows:
            _process(f, contacts, resolver, dry, tally, phone_memo)
        cursor = rows[-1]["customer_id"]
        processed += len(rows)
        pages += 1
        print("hubspot-sync %s [BACKFILL p%d]: %d processed so far (cursor=%s)"
              % (stamp, pages, processed, cursor))
        if len(rows) < page:
            break
    _print_tally(stamp, "BACKFILL/" + ("DRY-RUN" if dry else "EXECUTE"),
                 processed, _sync_enabled(), tally, dry)
    if not dry:
        state["contacts"] = _prune_contacts(contacts)
        _save_state(state)
    return 0


def _run_incremental(dry, limit):
    stamp = time.strftime("%Y-%m-%d %H:%M")
    state = _load_state()
    contacts = state.get("contacts", {})
    errors = state.get("errors") if isinstance(state.get("errors"), dict) else {}
    quarantine = state.get("quarantine") if isinstance(state.get("quarantine"), dict) else {}
    t0 = _now_pg()
    if not t0:
        print("hubspot-sync %s [INCREMENTAL]: postgres unreachable (now() failed)" % stamp)
        return 1
    resolver = _resolver()
    if resolver is None and not dry:
        # No live WAHA resolver -> every @lid would resolve to '' and (without this guard)
        # mass-settle. ABORT without advancing the watermark so nothing is skipped.
        print("hubspot-sync %s [INCREMENTAL]: ABORT — WAHA resolver unavailable; "
              "watermark NOT advanced (no rows skipped)" % stamp)
        return 1
    stored = state.get("watermark")
    # Composite keyset watermark {ts, cid}. First run (none/legacy): baseline to NOW so
    # history is covered by --backfill, not replayed by the incremental sweep.
    if isinstance(stored, dict) and stored.get("ts"):
        wm_eff = {"ts": stored["ts"], "cid": stored.get("cid", "")}
    else:
        wm_eff = {"ts": t0, "cid": ""}
    rows, err = _fetch_incremental(wm_eff, limit)
    if err is not None:
        print("hubspot-sync %s [INCREMENTAL]: FACTS read failed: %s" % (stamp, err))
        return 1
    tally = _new_tally()
    phone_memo = {}
    hw, broke = None, False
    for f in rows:
        cid = f.get("customer_id", "")
        rec = _process(f, contacts, resolver, dry, tally, phone_memo)
        if dry:
            continue
        if _is_settled(rec):
            errors.pop(cid, None)
            eff_settled = True
        elif rec.get("action") == "error":   # genuine WRITE failure -> count toward quarantine
            errors[cid] = int(errors.get(cid, 0)) + 1
            if errors[cid] >= MAX_RETRIES:
                quarantine[cid] = {"reason": rec.get("reason", "")[:160], "attempts": errors[cid]}
                errors.pop(cid, None)
                print("  QUARANTINE %s after %d attempts: %s"
                      % (cid, MAX_RETRIES, rec.get("reason", "")[:120]))
                eff_settled = True           # advance past so one poison row can't starve the tail
            else:
                eff_settled = False
        else:                                # resolve_pending (transient WAHA miss) -> HOLD,
            eff_settled = False              # never quarantine; recovers when WAHA resolves it
        if not broke and eff_settled:
            hw = {"ts": f.get("updated_at") or wm_eff["ts"], "cid": cid}   # last settled key
        elif not eff_settled:
            broke = True
    _print_tally(stamp, "INCREMENTAL/" + ("DRY-RUN" if dry else "EXECUTE"),
                 len(rows), _sync_enabled(), tally, dry)
    if not dry:
        new_wm = hw if hw is not None else wm_eff   # advance to last settled key, else hold
        state["watermark"] = new_wm
        state["contacts"] = _prune_contacts(contacts)
        state["errors"] = errors
        state["quarantine"] = quarantine
        _save_state(state)
        if broke:
            print("hubspot-sync: watermark HELD at %s (unsettled rows retry next run)"
                  % new_wm.get("ts"))
        if tally["resolve_pending"]:
            print("hubspot-sync: %d @lid contact(s) unresolved this run (WAHA may be degraded)"
                  " — held for retry" % tally["resolve_pending"])
        if quarantine:
            print("hubspot-sync: %d contact(s) quarantined for manual follow-up: %s"
                  % (len(quarantine), ", ".join(list(quarantine)[:10])))
    return 0


def main(argv):
    backfill = "--backfill" in argv
    dry = ("--dry-run" in argv) or ("--execute" not in argv)
    explicit_limit = None
    if "--limit" in argv:
        try:
            explicit_limit = int(argv[argv.index("--limit") + 1])
        except (IndexError, ValueError):
            pass

    if not _sync_enabled() and not dry:
        print("hubspot-sync %s: HUBSPOT_SYNC_ENABLED off — no-op (execute requested)"
              % time.strftime("%Y-%m-%d %H:%M"))
        return 0

    lock = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("hubspot-sync %s: another run holds the lock — skipping this tick"
              % time.strftime("%Y-%m-%d %H:%M"))
        return 0

    try:
        if backfill:
            return _run_backfill(dry, explicit_limit)
        return _run_incremental(dry, explicit_limit or DEFAULT_LIMIT)
    finally:
        try:
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
