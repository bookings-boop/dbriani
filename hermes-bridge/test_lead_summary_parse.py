#!/usr/bin/env python3
"""read_lead_summary parse hardening — the Gül parse-drop bug (2026-06-10).

Python's str.strip() treats \\x1f (the 0x1F unit-separator that #B1 made the
field delimiter) as WHITESPACE ('\\x1f'.isspace() is True). A brand-new lead
(NEW, mc=1) has every concat_ws field after message_count empty, so its line
ends in a run of 24 \\x1f bytes — and with no ORDER BY the newest row can be
emitted LAST, putting that run at the very end of psql stdout. The bare
.strip() at the top of the parse loop ate it, collapsing 31 fields to 7, and
the silent `len(parts) < 20` guard dropped the lead from /review, the owed
digest AND the owe-reply sweep simultaneously (live case: 201962398167271@lid
"Gül", invisible 2026-06-09→10).

Locks:
  - a last row whose trailing fields are all empty survives the parse;
  - a genuinely short row is dropped WITH a log line (never silently).

Run: python3 hermes-bridge/test_lead_summary_parse.py  (or pytest)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402

SEP = "\x1f"


def _row(cid, name="Anchor", label="WARM", mc="5", tail=None):
    """One 31-field v_lead_summary line exactly as psql -tA emits it.
    Fields 1-7: customer_id, name, label, label_updated_at,
    label_locked_until, locked-bool, message_count; fields 8-31 = `tail`
    (24 fields, default all empty — the brand-new-lead signature)."""
    head = [cid, name, label, "2026-06-09 18:30:00+00", "", "f", mc]
    rest = list(tail) if tail is not None else [""] * 24
    assert len(head) + len(rest) == 31
    return SEP.join(head + rest)


def _run(out):
    """Call read_lead_summary with _psql faked to return `out`; collect log
    lines. Returns (rows, logged)."""
    logged = []
    real_psql, real_log = server._psql, server.log
    server._psql = lambda sql, timeout=12: (out, None)
    server.log = lambda *a: logged.append(" ".join(str(x) for x in a))
    try:
        return server.read_lead_summary(None), logged
    finally:
        server._psql, server.log = real_psql, real_log


def test_last_row_with_all_empty_tail_fields_survives():
    """The Gül case: newest unanalyzed lead emitted last, 24 trailing \\x1f."""
    out = _row("100@c.us") + "\n" + _row("201962398167271@lid",
                                         name="Gül", label="NEW",
                                         mc="1") + "\n"
    rows, _ = _run(out)
    cids = [r["customer_id"] for r in rows]
    assert cids == ["100@c.us", "201962398167271@lid"], \
        f"last row was dropped: {cids}"
    gul = rows[1]
    assert gul["name"] == "Gül"
    assert gul["label"] == "NEW"
    assert gul["message_count"] == 1
    assert gul["importance_score"] is None


def test_single_row_output_with_empty_tail_survives():
    """Degenerate case: the ONLY row is also the last row."""
    rows, _ = _run(_row("777@lid", label="NEW", mc="1") + "\n")
    assert [r["customer_id"] for r in rows] == ["777@lid"]


def test_short_row_is_dropped_with_a_log_line():
    """A genuinely truncated/garbage row must still be skipped — but loudly."""
    good = _row("100@c.us")
    bad = SEP.join(["999@c.us", "Mangled", "NEW"])  # 3 fields, mid-output
    rows, logged = _run(good + "\n" + bad + "\n" + _row(
        "200@c.us", tail=[""] * 23 + ["2026-06-20"]) + "\n")
    assert [r["customer_id"] for r in rows] == ["100@c.us", "200@c.us"]
    assert any("999@c.us" in ln for ln in logged), \
        f"short-row discard was silent (logged={logged})"


def test_normal_full_rows_parse_unchanged():
    """Anchor: rows with populated tails parse exactly as before."""
    tail = [""] * 24
    tail[12] = "ok note"          # recent_notes (field 20 / idx 19)
    tail[13] = "55"               # importance_score (field 21 / idx 20)
    tail[23] = "2026-06-20"       # booking_date_abs (field 31 / idx 30)
    out = _row("100@c.us") + "\n" + _row("300@c.us", label="HOT", mc="9",
                                         tail=tail) + "\n"
    rows, _ = _run(out)
    assert len(rows) == 2
    assert rows[1]["importance_score"] == 55
    assert rows[1]["recent_notes"] == "ok note"
    assert rows[1]["booking_date_abs"] == "2026-06-20"


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
