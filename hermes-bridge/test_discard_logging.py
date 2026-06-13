#!/usr/bin/env python3
"""Silent-discard class batch (same family as the Gül \\x1f bug, 2026-06-11).

Six sites converted from silently-dropping/corrupting to 0x1F-safe + loud:
 1. scan_followup_eligibility — SQL was psql-default '|' joined; a pipe in a
    pushName shifted fields → float() failed → candidate INVISIBLE forever,
    zero log. Now concat_ws(0x1F, COALESCE...) + strip("\\n") + logged drop.
 2a _parse_conv_rows — already 0x1F; drops now logged.
 2b resolve_customer_by_name — '\\t' join, NO COALESCE; tab/newline in a name
    truncated/shattered matches. Now 0x1F + newline scrub + logged drop.
 2c resolve_customer_by_phone — logged drop (COALESCE already present).
 2d behavioral_context scenario rules — '\\t' join, NO COALESCE; a tab in an
    operator rule silently AMPUTATED the rule tail (the "rules don't work"
    class). Now 0x1F + COALESCE + newline scrub + split(sep,1) + logged drop.
 2e get_current_label_row — '|' join; one malformed row = the customer's
    ENTIRE label state nulled (locks silently bypassed) across 16 call
    sites. Now 0x1F + COALESCE(label) + strip("\\n") + logged before None.
 3  hourly pipeline-analyze SELECT — missing merged_into IS NULL: 30 merged
    ghosts in rotation, 3 of the next 24 slots burned, forever. One line.

Run: python3 hermes-bridge/test_discard_logging.py  (or pytest)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402
import routes  # noqa: E402

FS = "\x1f"


def _patch(mod, **attrs):
    old = {}
    for k, v in attrs.items():
        old[k] = getattr(mod, k)
        setattr(mod, k, v)
    return old


def _restore(mod, old):
    for k, v in old.items():
        setattr(mod, k, v)


def _capture_log():
    lines = []
    return lines, lambda *a: lines.append(" ".join(str(x) for x in a))


# ------------------------------------------------- 1. followup eligibility
def _followup_row(cid="55@c.us", name="Eva", label="WARM", hrs="30.5",
                  we_replied="t", locked="f", mode="approval",
                  nudged="f", count="0"):
    return FS.join([cid, name, label, hrs, we_replied, locked, mode,
                    nudged, count])


def test_followup_sql_uses_x1f_and_coalesce():
    sql = server._followup_candidate_sql()
    assert "concat_ws(E'\\x1f'" in sql, sql[:120]
    assert "COALESCE(cf.label, '')" in sql


def test_followup_pipe_in_name_survives():
    logs, fake_log = _capture_log()
    out = _followup_row(name="Tariq | VIP 30-12") + "\n"
    old = _patch(server, _psql=lambda s, timeout=15: (out, None),
                 _redis=lambda a: ("", None), log=fake_log,
                 FOLLOWUP_ENGINE_ENABLED=True)
    try:
        cands = server.scan_followup_eligibility()
    finally:
        _restore(server, old)
    assert len(cands) == 1, (cands, logs)
    assert cands[0]["name"] == "Tariq | VIP 30-12"


def test_followup_short_row_logged_not_silent():
    logs, fake_log = _capture_log()
    out = FS.join(["99@c.us", "Mangled", "NEW"]) + "\n" + _followup_row()
    old = _patch(server, _psql=lambda s, timeout=15: (out, None),
                 _redis=lambda a: ("", None), log=fake_log,
                 FOLLOWUP_ENGINE_ENABLED=True)
    try:
        cands = server.scan_followup_eligibility()
    finally:
        _restore(server, old)
    assert len(cands) == 1
    assert any("99@c.us" in ln for ln in logs), logs


# ------------------------------------------------- 2a. _parse_conv_rows
def test_parse_conv_rows_logs_malformed():
    logs, fake_log = _capture_log()
    good = FS.join(["1781000000", "in", "hello", "mid1"])
    old = _patch(server, log=fake_log)
    try:
        rows = server._parse_conv_rows("junk-no-fields\n" + good)
    finally:
        _restore(server, old)
    assert len(rows) == 1
    assert any("junk-no-fields" in ln for ln in logs), logs


# ------------------------------------------------- 2b/2c. resolvers
def test_resolve_by_name_x1f_and_log():
    logs, fake_log = _capture_log()
    out = FS.join(["55@c.us", "Vitali 11-03\tSerdal"]) + "\nmangled-line"
    old = _patch(server, _psql=lambda s, timeout=12: (out, None),
                 log=fake_log)
    try:
        cid, matches = server.resolve_customer_by_name("vitali")
    finally:
        _restore(server, old)
    assert cid == "55@c.us"
    assert matches[0]["name"] == "Vitali 11-03\tSerdal"  # tab survives
    assert any("mangled-line" in ln for ln in logs), logs


def test_resolve_by_phone_logs_malformed():
    logs, fake_log = _capture_log()
    old = _patch(server, _psql=lambda s, timeout=12: ("only-one-field", None),
                 log=fake_log)
    try:
        server.resolve_customer_by_phone("971501234567")
    finally:
        _restore(server, old)
    assert any("only-one-field" in ln for ln in logs), logs


# ------------------------------------------------- 2d. scenario rules
def test_scenario_rule_with_tab_survives_intact():
    logs, fake_log = _capture_log()
    rule = "ALWAYS include\tthe parking pin AND the drop-off pin"

    def router(sql, timeout=12):
        if "scope='scenario'" in sql:
            assert "concat_ws(E'\\x1f'" in sql and "COALESCE" in sql, sql
            return (FS.join(["pricing", rule]) + "\nmangledrow", None)
        return ("", None)

    old = _patch(server, _psql=router, log=fake_log)
    try:
        ctx = server.behavioral_context("")
    finally:
        _restore(server, old)
    sc = ctx.get("scenario") or []
    assert len(sc) == 1, (sc, logs)
    assert sc[0]["rule"] == rule  # full rule incl. tab — no amputation
    assert any("mangledrow" in ln for ln in logs), logs


# ------------------------------------------------- 2e. get_current_label_row
def test_label_row_x1f_trailing_empties_survive():
    # WARM lead, no lock/yachts/dates — trailing-empty fields + strip("\n")
    line = FS.join(["WARM", "2026-06-10T10:00:00+00", "", "8", "Eva", "", ""])

    def router(sql, timeout=12):
        if "concat_ws" in sql and "customer_facts" in sql:
            return (line + "\n", None)
        return ("", None)

    old = _patch(server, _psql=router, canonicalize_cid=lambda c: c)
    try:
        row = server.get_current_label_row("199@lid")
    finally:
        _restore(server, old)
    assert row is not None
    assert row["label"] == "WARM" and row["message_count"] == 8 \
        and row["name"] == "Eva" and row["dates"] == ""


def test_label_row_malformed_logged_before_none():
    logs, fake_log = _capture_log()

    def router(sql, timeout=12):
        if "customer_facts" in sql:
            return (FS.join(["WARM", "x", "y"]), None)
        return ("", None)

    old = _patch(server, _psql=router, canonicalize_cid=lambda c: c,
                 log=fake_log)
    try:
        row = server.get_current_label_row("199@lid")
    finally:
        _restore(server, old)
    assert row is None
    assert any("199@lid" in ln for ln in logs), logs


# ------------------------------------------------- 3. hourly merged filter
def test_hourly_analyze_sql_filters_merged():
    import inspect
    src = inspect.getsource(routes.handle_pipeline_analyze)
    # Anchor on the ORDER BY (the rotation column is now flag-selected via
    # {_rot_col}, so don't pin the literal column name).
    i = src.find("ASC NULLS FIRST")
    assert i != -1
    assert "merged_into IS NULL" in src[max(0, i - 900):i], \
        "hourly SELECT still missing merged_into IS NULL"
    # T-1 (2026-06-13): the rotation must be able to order on
    # last_analyze_attempt_at (SWEEP_ATTEMPT_ROTATION_ENABLED) so a chronic
    # Hermes-failer moves to the back instead of starving the stalest-first front.
    assert "last_analyze_attempt_at" in src, "T-1 attempt-rotation column missing"
    assert "SWEEP_ATTEMPT_ROTATION_ENABLED" in src, "T-1 rotation flag missing"


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn(); print("PASS", fn.__name__)
        except AssertionError as e:
            failed += 1; print("FAIL", fn.__name__, "-", e or "assert")
        except Exception as e:  # noqa: BLE001
            failed += 1; print("ERROR", fn.__name__, "-", repr(e))
    print(f"{len(fns)-failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
