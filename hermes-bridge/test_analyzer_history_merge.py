#!/usr/bin/env python3
"""F1 (2026-06-09): build_analyzer_history dropped older WAHA history whenever the
durable store was partial — its top-up kept ONLY WAHA rows with ts > max(durable
ts), so a single recent durable echo hid every older WAHA inbound and the analyzer
read a hollow "first contact" thread (Devanshu / Youssra @lid class).

Fix = merge ALL WAHA rows deduped against durable by msg_id (durable authoritative)
AND by (direction, normalized body) — the SAME bubble carries a bridge draft id in
durable but a WAHA 'true_<lid>_<hash>' id, so msg_id-only dedup would double-show it.
id-less WAHA rows get a stable synthetic id so the read-path-persist dedupes them
across analyses instead of re-appending a NULL-msg_id row every time.

These lock the PURE merge/dedup helper (no DB / no WAHA).
Run: python3 hermes-bridge/test_analyzer_history_merge.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from server import _merge_waha_topup, _waha_synthetic_id  # noqa: E402


def _d(ts, direction, body, msg_id):
    return {"ts": ts, "direction": direction, "body": body, "msg_id": msg_id}


def test_includes_older_waha_when_durable_partial():
    # THE BUG: durable holds only a recent outbound; WAHA holds older real
    # inbound. Old `ts > max(durable)` dropped them; merge-all keeps them.
    durable = [_d(2000, "out", "here are a few options for you", "draft1")]
    waha = [_d(1000, "in", "Hi good afternoon", "w1"),
            _d(1500, "in", "This is Youssra, EA of Mr Q", "w2")]
    bodies = [r["body"] for r in _merge_waha_topup("c@lid", durable, waha)]
    assert "Hi good afternoon" in bodies, bodies
    assert "This is Youssra, EA of Mr Q" in bodies, bodies


def test_dedupes_by_msg_id_durable_authoritative():
    durable = [_d(1000, "in", "hello", "m1")]
    waha = [_d(1000, "in", "hello", "m1")]
    assert _merge_waha_topup("c", durable, waha) == []


def test_dedupes_same_bubble_across_id_namespaces():
    # Same outbound bubble: durable has the bridge draft id, WAHA the true_* id.
    body = "for a collaboration discussion, please reach out to bookings@dubriani"
    durable = [_d(1000, "out", body, "1780930973238_30gmy")]
    waha = [_d(1000, "out", body, "true_208134232662176@lid_3EB0E972FFD3CE68B53238")]
    assert _merge_waha_topup("c", durable, waha) == [], "content-dedup must catch it"


def test_synthesizes_id_for_idless_waha_row():
    durable = [_d(2000, "out", "x", "d1")]
    waha = [_d(1000, "in", "ping?", None)]
    topup = _merge_waha_topup("c@lid", durable, waha)
    assert len(topup) == 1
    assert topup[0]["msg_id"] == _waha_synthetic_id("c@lid", 1000, "ping?")
    assert topup[0]["msg_id"].startswith("waha:c@lid:1000:")


def test_synthetic_id_is_stable_and_body_sensitive():
    a = _waha_synthetic_id("c", 1000, "hello")
    assert a == _waha_synthetic_id("c", 1000, "hello")           # stable
    assert a != _waha_synthetic_id("c", 1000, "HELLO different")  # body-sensitive


def test_skips_empty_bodies():
    durable = [_d(2000, "out", "x", "d1")]
    assert _merge_waha_topup("c", durable, [_d(1000, "in", "   ", "w1")]) == []


def test_no_duplicate_within_waha_batch():
    # WAHA returns the same bubble twice -> only one survives.
    durable = [_d(3000, "out", "anchor", "d1")]
    waha = [_d(1000, "in", "same msg", "w1"), _d(1001, "in", "same msg", "w2")]
    assert len(_merge_waha_topup("c", durable, waha)) == 1


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
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
