#!/usr/bin/env python3
"""hubspot_sync (write-side) — unit tests for the WhatsApp -> HubSpot sync.

Contract under test:
  - Identity/linking is phone-only; @lid resolved via the injected resolver (live WAHA).
  - Composed summary/note are SUMMARIES, never raw transcripts, and are money-scrubbed.
  - money_guard() makes it IMPOSSIBLE to write hermes_summary / money / lifecycle props.
  - dry_run performs the read-only phone search but makes ZERO writes.
  - Note timeline is debounced (first / status-change / cooldown).
  - Read-side (hubspot_lookup) only surfaces hermes_interaction_context when the
    HUBSPOT_CTX_READ_ENABLED sub-gate is ON (default OFF == legacy behaviour).

Run (no pytest on the box): python3 hermes-bridge/test_hubspot_sync.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import hubspot_lookup  # noqa: E402
import hubspot_sync as hs  # noqa: E402


# ── derive_booking_status ────────────────────────────────────────────
def test_status_booked_via_booked_yacht():
    assert hs.derive_booking_status({"booked_yacht": "Satoshi", "label": "WARM"}) == "booked"


def test_status_booked_via_confirmed_label():
    assert hs.derive_booking_status({"label": "CONFIRMED"}) == "booked"


def test_status_lost_disregarded():
    assert hs.derive_booking_status({"label": "LOST"}) == "lost"
    assert hs.derive_booking_status({"label": "DISREGARDED"}) == "disregarded"


def test_status_quoted_requires_yachts_and_dates():
    assert hs.derive_booking_status({"yachts": "Luna", "dates": "Jun 20"}) == "quoted"
    assert hs.derive_booking_status({"yachts": "Luna"}) == "enquiry"  # no dates -> enquiry
    assert hs.derive_booking_status({}) == "enquiry"


# ── name_parts ───────────────────────────────────────────────────────
def test_name_parts_basic_and_titlecase():
    assert hs.name_parts("john smith") == ("John", "Smith")
    assert hs.name_parts("Madonna") == ("Madonna", "")


def test_name_parts_drops_digit_and_email_tokens():
    assert hs.name_parts("Ali 971501234567") == ("Ali", "")
    assert hs.name_parts("bob bob@x.com jones") == ("Bob", "Jones")


def test_name_parts_placeholder_is_empty():
    for bad in ("", "unknown", "Customer", "guest", "n/a"):
        assert hs.name_parts(bad) == ("", ""), bad


# ── compose_context / compose_note ───────────────────────────────────
def test_compose_context_shape_and_scrub():
    ctx = hs.compose_context({"label": "WARM", "yachts": "Satoshi",
                              "dates": "Jun 20", "party_size": "6 guests",
                              "last_customer_message_at": "2026-06-14T10:00:00+00:00"})
    assert ctx.startswith("Status: quoted")
    assert "Yachts: Satoshi" in ctx
    assert "Party: 6 guests" in ctx
    assert "Last contact: 2026-06-14" in ctx
    assert len(ctx) <= hs._CTX_MAX


def test_compose_context_scrubs_money():
    ctx = hs.compose_context({"label": "WARM", "yachts": "Satoshi AED 5,000 deal"})
    assert "5,000" not in ctx and "5000" not in ctx


def test_compose_note_is_summary_not_transcript_and_scrubbed():
    note = hs.compose_note({"label": "WARM", "yachts": "Luna",
                            "party_size": "8", "message_count": "12",
                            "importance_reasoning": "Asked for AED 7,500 budget, hot lead",
                            "suggested_action": "Send paylink"})
    assert "WhatsApp update — status:" in note
    assert "Analyzer read:" in note
    assert "not a verbatim transcript" in note
    assert "7,500" not in note and "7500" not in note   # money scrubbed


# ── build_props + money_guard ─────────────────────────────────────────
def test_build_props_sets_name_only_on_create():
    create = hs.build_props({"name": "Jane Doe", "label": "WARM", "yachts": "X"},
                            for_create=True)
    assert create.get("firstname") == "Jane" and create.get("lastname") == "Doe"
    patch = hs.build_props({"name": "Jane Doe", "label": "WARM", "yachts": "X"},
                           for_create=False)
    assert "firstname" not in patch and "lastname" not in patch


def test_build_props_writes_interaction_context_never_summary():
    p = hs.build_props({"label": "WARM", "yachts": "Satoshi", "dates": "Jun"},
                       for_create=True)
    assert "hermes_interaction_context" in p
    assert "hermes_summary" not in p
    assert "hermes_data_source" not in p
    hs.money_guard(p)  # must not raise


def test_money_guard_blocks_forbidden():
    for bad in ("hermes_summary", "amount", "dealstage", "lifecyclestage",
                "hermes_data_source", "last_quote_amount_aed"):
        try:
            hs.money_guard({bad: "x", "hermes_interaction_context": "ok"})
            raise AssertionError("money_guard did not block " + bad)
        except RuntimeError as e:   # raises (not assert) so it survives python -O
            assert "forbidden" in str(e), str(e)


def test_compose_note_strips_bare_hourly_rate():
    note = hs.compose_note({"label": "WARM",
                            "importance_reasoning": "wants Bliss at 900/hr",
                            "suggested_action": "offered 800 for the day"})
    assert "900" not in note and "800" not in note   # 3-digit Dubriani rates stripped


def test_compose_context_strips_bare_rate_in_yacht_field():
    ctx = hs.compose_context({"label": "WARM", "yachts": "Bliss 900 special"})
    assert "900" not in ctx
    assert "Bliss" in ctx   # yacht name survives; only the bare rate is stripped


def test_compose_note_preserves_message_count():
    note = hs.compose_note({"label": "WARM", "message_count": "127"})
    assert "Messages exchanged: 127" in note   # structured count is NOT a price -> kept


# ── should_create_note (timeline debounce) ───────────────────────────
def test_should_note_first_time():
    assert hs.should_create_note("enquiry", None, 1000.0) is True


def test_should_note_status_change():
    prev = {"last_status": "enquiry", "last_note_ts": 1000.0}
    assert hs.should_create_note("quoted", prev, 1001.0) is True


def test_should_note_within_cooldown_same_status_false():
    prev = {"last_status": "enquiry", "last_note_ts": 1000.0}
    assert hs.should_create_note("enquiry", prev, 1000.0 + 60, cooldown_s=3600) is False


def test_should_note_after_cooldown_true():
    prev = {"last_status": "enquiry", "last_note_ts": 1000.0}
    assert hs.should_create_note("enquiry", prev, 1000.0 + 7200, cooldown_s=3600) is True


# ── sync_contact: dry-run makes no writes ─────────────────────────────
class _Hub:
    def __init__(self, existing_id=None):
        self.calls = []
        self.existing_id = existing_id

    def http(self, method, path, payload):
        self.calls.append((method, path, payload))
        if path.endswith("/contacts") and method == "POST":
            return (201, {"id": "NEW1"}, "")
        if "/contacts/" in path and method == "PATCH":
            return (200, {"id": self.existing_id}, "")
        if path.endswith("/notes") and method == "POST":
            return (201, {"id": "NOTE1"}, "")
        return (200, {}, "")

    def search(self, phone, http):
        return (True, self.existing_id)     # (ok, id) — search succeeded


_NEVER_EXCLUDE = lambda cid, res: False           # noqa: E731
_LID_RESOLVER = lambda c: "+971501234567"          # noqa: E731


def test_dry_run_matched_no_writes():
    hub = _Hub(existing_id="C99")
    rec = hs.sync_contact({"customer_id": "501234567@c.us", "label": "WARM",
                           "yachts": "Satoshi", "dates": "Jun"},
                          resolver=None, dry_run=True, http=hub.http,
                          search=hub.search, exclude=_NEVER_EXCLUDE)
    assert rec["action"] == "matched"
    assert rec["wrote_props"] is False and rec["wrote_note"] is False
    assert rec["would_note"] is True    # first sync -> would post a note (prediction only)
    # dry-run must NOT have issued any contact PATCH or contact/note POST
    assert not any((m == "PATCH")
                   or (m == "POST" and p in (hs._CONTACTS_PATH, hs._NOTES_PATH))
                   for (m, p, _) in hub.calls)


def test_dry_run_created_when_no_match():
    hub = _Hub(existing_id=None)
    rec = hs.sync_contact({"customer_id": "501234567@c.us", "label": "NEW"},
                          resolver=None, dry_run=True, http=hub.http,
                          search=hub.search, exclude=_NEVER_EXCLUDE)
    assert rec["action"] == "created"  # would create
    assert rec["wrote_props"] is False


def test_excluded_short_and_nophone():
    # excluded
    r1 = hs.sync_contact({"customer_id": "x@lid"}, resolver=_LID_RESOLVER,
                         dry_run=True, exclude=lambda c, r: True)
    assert r1["action"] == "excluded"
    # @lid unresolvable (no resolver) -> RETRYABLE (resolve_pending), not terminal no_phone
    r2 = hs.sync_contact({"customer_id": "deadbeef@lid"}, resolver=None,
                         dry_run=True, exclude=_NEVER_EXCLUDE)
    assert r2["action"] == "resolve_pending"
    # too short (<9 digits)
    r3 = hs.sync_contact({"customer_id": "12345@c.us"}, resolver=None,
                         dry_run=True, exclude=_NEVER_EXCLUDE,
                         search=lambda p, h: (True, None))
    assert r3["action"] == "too_short"


def test_search_error_skips_never_creates():
    # A transient/failed search (ok=False) must NOT create a duplicate contact.
    hub = _Hub(existing_id=None)
    rec = hs.sync_contact({"customer_id": "501234567@c.us", "label": "NEW"},
                          resolver=None, dry_run=False, http=hub.http,
                          search=lambda p, h: (False, None), exclude=_NEVER_EXCLUDE)
    assert rec["action"] == "error"
    assert not any(m == "POST" and p == hs._CONTACTS_PATH for (m, p, _) in hub.calls)


# ── sync_contact: execute path issues correct, money-safe writes ──────
def _scan_no_forbidden(calls):
    for (_m, _p, payload) in calls:
        props = (payload or {}).get("properties", {}) if payload else {}
        bad = set(props) & hs._FORBIDDEN_PROPS
        assert not bad, "forbidden prop written: %r" % bad


def test_execute_create_writes_contact_and_note():
    hub = _Hub(existing_id=None)
    rec = hs.sync_contact({"customer_id": "lidhash@lid", "name": "Jane Doe",
                           "label": "WARM", "yachts": "Satoshi", "dates": "Jun",
                           "importance_reasoning": "wants weekend charter"},
                          resolver=_LID_RESOLVER, dry_run=False, http=hub.http,
                          search=hub.search, exclude=_NEVER_EXCLUDE)
    assert rec["action"] == "created" and rec["contact_id"] == "NEW1"
    assert rec["wrote_props"] is True and rec["wrote_note"] is True
    methods = [(m, p) for (m, p, _) in hub.calls]
    assert ("POST", hs._CONTACTS_PATH) in methods       # contact created
    assert ("POST", hs._NOTES_PATH) in methods          # note created
    _scan_no_forbidden(hub.calls)
    # the created contact carries +E.164 phone, never name-keyed
    create_payload = next(pl for (m, p, pl) in hub.calls
                          if m == "POST" and p == hs._CONTACTS_PATH)
    assert create_payload["properties"]["phone"] == "+971501234567"
    assert "hermes_interaction_context" in create_payload["properties"]


def test_execute_match_patches_not_creates():
    hub = _Hub(existing_id="C42")
    rec = hs.sync_contact({"customer_id": "501234567@c.us", "label": "WARM",
                           "yachts": "Luna", "dates": "Jul"},
                          resolver=None, dry_run=False, http=hub.http,
                          search=hub.search, exclude=_NEVER_EXCLUDE)
    assert rec["action"] == "matched" and rec["contact_id"] == "C42"
    assert rec["wrote_props"] is True
    methods = [(m, p) for (m, p, _) in hub.calls]
    assert ("PATCH", hs._CONTACTS_PATH + "/C42") in methods
    assert ("POST", hs._CONTACTS_PATH) not in methods   # no create on a match
    _scan_no_forbidden(hub.calls)


def test_execute_never_raises_on_http_failure():
    class Boom:
        def http(self, m, p, pl):
            return (500, None, "HTTP 500 boom")

        def search(self, phone, http):
            return (True, None)     # search OK, no match -> create path, which 500s
    boom = Boom()
    rec = hs.sync_contact({"customer_id": "501234567@c.us", "label": "NEW"},
                          resolver=None, dry_run=False, http=boom.http,
                          search=boom.search, exclude=_NEVER_EXCLUDE)
    assert rec["action"] == "error"  # create failed, but no exception escaped


def test_lid_unresolved_is_retryable_not_terminal():
    # @lid whose live resolver returns "" (a WAHA miss) must be RETRYABLE, not skipped.
    rec = hs.sync_contact({"customer_id": "deadbeef@lid", "label": "NEW"},
                          resolver=lambda c: "", dry_run=True, exclude=_NEVER_EXCLUDE)
    assert rec["action"] == "resolve_pending"


def test_non_lid_no_phone_is_terminal():
    rec = hs.sync_contact({"customer_id": "garbage-no-digits", "label": "NEW"},
                          resolver=None, dry_run=True, exclude=_NEVER_EXCLUDE)
    assert rec["action"] == "no_phone"


def test_phone_memo_dedups_same_phone_in_run():
    # Two cids resolving to the SAME phone in one sweep -> ONE create, second PATCHes it.
    hub = _Hub(existing_id=None)
    memo = {}
    r1 = hs.sync_contact({"customer_id": "lidA@lid", "label": "NEW"},
                         resolver=lambda c: "+971501234567", dry_run=False,
                         http=hub.http, search=hub.search, exclude=_NEVER_EXCLUDE,
                         phone_memo=memo)
    r2 = hs.sync_contact({"customer_id": "971501234567@c.us", "label": "WARM",
                          "yachts": "X", "dates": "Jun"},
                         resolver=None, dry_run=False, http=hub.http,
                         search=hub.search, exclude=_NEVER_EXCLUDE, phone_memo=memo)
    assert r1["action"] == "created" and r1["contact_id"] == "NEW1"
    assert r2["action"] == "matched" and r2["contact_id"] == "NEW1"
    creates = [1 for (m, p, _) in hub.calls if m == "POST" and p == hs._CONTACTS_PATH]
    assert len(creates) == 1, hub.calls   # ONE create despite two same-phone cids


def test_create_does_not_retry_on_5xx():
    calls = []

    def http(m, p, pl):
        calls.append((m, p, pl))
        return (500, None, "boom")
    cid, _mode = hs._create_contact("971501234567", {"hermes_interaction_context": "x"}, http)
    assert cid is None
    posts = [1 for (m, p, _) in calls if m == "POST" and p == hs._CONTACTS_PATH]
    assert len(posts) == 1   # did NOT retry on 5xx (may have committed -> would duplicate)


def test_create_retries_minimal_on_4xx():
    seq = [(400, None, "bad prop"), (201, {"id": "X9"}, "")]
    cid, mode = hs._create_contact(
        "971501234567", {"hermes_interaction_context": "x", "hermes_message_count": "5"},
        lambda m, p, pl: seq.pop(0))
    assert cid == "X9" and mode == "created-minimal"


# ── read-side merge gate (hubspot_lookup) ────────────────────────────
def _stub_props():
    return {
        "firstname": "Jane", "lastname": "Doe",
        "hermes_summary": "Type: repeat client",
        "hermes_interaction_context": "Status: quoted · Yachts: Satoshi",
        "hermes_data_quality_flag": "false",
    }


def test_readside_gate_off_is_legacy(monkey_restore=None):
    os.environ.pop("HUBSPOT_CTX_READ_ENABLED", None)
    orig = hubspot_lookup._http_search
    hubspot_lookup._http_search = lambda k: _stub_props()
    try:
        block = hubspot_lookup._build_block("971501234567")
    finally:
        hubspot_lookup._http_search = orig
    assert "Type: repeat client" in block
    assert "Prior interaction (HISTORICAL" not in block   # gate OFF -> ctx not surfaced


def test_readside_gate_on_surfaces_context():
    os.environ["HUBSPOT_CTX_READ_ENABLED"] = "1"
    orig = hubspot_lookup._http_search
    hubspot_lookup._http_search = lambda k: _stub_props()
    try:
        block = hubspot_lookup._build_block("971501234567")
    finally:
        hubspot_lookup._http_search = orig
        os.environ.pop("HUBSPOT_CTX_READ_ENABLED", None)
    assert "Prior interaction (HISTORICAL" in block      # constraining reframe
    assert "do NOT assert any yacht is available" in block
    assert "Status: quoted" in block
    assert "Type: repeat client" in block               # both surfaced


# ── stdlib runner (box has no pytest) ────────────────────────────────
def _run():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    passed = failed_n = 0
    for t in tests:
        try:
            t()
            passed += 1
            print(f"  PASS {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed_n += 1
            print(f"  FAIL {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{passed} passed, {failed_n} failed, {len(tests)} total")
    return 1 if failed_n else 0


if __name__ == "__main__":
    sys.exit(_run())
