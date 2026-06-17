#!/usr/bin/env python3
"""Bug 2 (2026-06-17): the misleading '⚠️ analysis unreliable — history
incomplete' flag on /review cards whose analysis is actually fine. TWO causes,
two flag-gated fixes:

  Fix A (REVIEW_UNRELIABLE_NULL_IS_RELIABLE_ENABLED): a NULL stored verdict means
    the lead was never written by the pipeline-analyze path (e.g. /disregard-only
    leads) — NOT a starved analysis. Falling back to the broad legacy regex
    re-introduces the false positive on 'never replied'/'first contact' prose
    (Wijdane). When on, treat NULL as RELIABLE.
  Fix B (DISREGARD_WRITES_UNRELIABLE_ENABLED): the /disregard analyze snapshot
    recomputes + stores analysis_unreliable (mirroring the pipeline write) so a
    stale TRUE can CLEAR on re-analysis (…7506 stayed flagged forever).

Run: python3 hermes-bridge/test_review_unreliable_null.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import review  # noqa: E402
from test_disregard_second_press import _Harness, _call  # noqa: E402

# Reasoning that the legacy _EMPTY_HISTORY_RE matches ("never replied") — i.e.
# customer-behaviour prose, NOT a real history-availability problem.
_DEAD_REASON = ("Jun 6 date passed. Customer never replied to 3 follow-ups. "
                "Lead is dead.")


class _rflags:
    """Set review.py module-level flags for a block, then restore."""

    def __init__(self, **kw):
        self.kw = kw
        self._prev = {}

    def __enter__(self):
        for k, v in self.kw.items():
            self._prev[k] = getattr(review, k, None)
            setattr(review, k, v)
        return self

    def __exit__(self, *a):
        for k, v in self._prev.items():
            setattr(review, k, v)


# ---- Fix A: render gate ----------------------------------------------------
def test_null_verdict_reliable_when_flag_on():
    with _rflags(REVIEW_UNRELIABLE_FROM_STORE_ENABLED=True,
                 REVIEW_UNRELIABLE_NULL_IS_RELIABLE_ENABLED=True):
        row = {"analysis_unreliable": None, "message_count": 16,
               "importance_reasoning": _DEAD_REASON}
        assert review._analysis_unreliable_for_render(row) is False, \
            "NULL verdict must be treated as reliable when the flag is on"


def test_null_verdict_falls_back_to_regex_when_flag_off():
    with _rflags(REVIEW_UNRELIABLE_FROM_STORE_ENABLED=True,
                 REVIEW_UNRELIABLE_NULL_IS_RELIABLE_ENABLED=False):
        row = {"analysis_unreliable": None, "message_count": 16,
               "importance_reasoning": _DEAD_REASON}
        assert review._analysis_unreliable_for_render(row) is True, \
            "flag OFF preserves the legacy fallback (regex matches 'never replied')"


def test_stored_true_still_flags():
    with _rflags(REVIEW_UNRELIABLE_FROM_STORE_ENABLED=True,
                 REVIEW_UNRELIABLE_NULL_IS_RELIABLE_ENABLED=True):
        row = {"analysis_unreliable": True, "message_count": 7,
               "importance_reasoning": "history unavailable"}
        assert review._analysis_unreliable_for_render(row) is True, \
            "a genuine stored TRUE must still flag"


def test_stored_false_not_flagged():
    with _rflags(REVIEW_UNRELIABLE_FROM_STORE_ENABLED=True,
                 REVIEW_UNRELIABLE_NULL_IS_RELIABLE_ENABLED=True):
        row = {"analysis_unreliable": False, "message_count": 7,
               "importance_reasoning": _DEAD_REASON}
        assert review._analysis_unreliable_for_render(row) is False


# ---- Fix B: /disregard snapshot persists analysis_unreliable ---------------
def _disregard_snapshot(flag_on):
    prev = os.environ.get("DISREGARD_WRITES_UNRELIABLE_ENABLED")
    os.environ["DISREGARD_WRITES_UNRELIABLE_ENABLED"] = "1" if flag_on else "0"
    try:
        h = _Harness(label="NEW", prior_veto=False, hermes_verdict="keep_open")
        _call(h, {"customer_id": "x@lid"})
        return h.snapshot_writes
    finally:
        if prev is None:
            os.environ.pop("DISREGARD_WRITES_UNRELIABLE_ENABLED", None)
        else:
            os.environ["DISREGARD_WRITES_UNRELIABLE_ENABLED"] = prev


def test_disregard_writes_unreliable_when_flag_on():
    writes = _disregard_snapshot(True)
    assert any("analysis_unreliable =" in s for s in writes), \
        "flag ON: the /disregard snapshot UPDATE must persist analysis_unreliable"


def test_disregard_omits_unreliable_when_flag_off():
    writes = _disregard_snapshot(False)
    assert not any("analysis_unreliable =" in s for s in writes), \
        "flag OFF: snapshot SQL must be byte-identical (no analysis_unreliable)"


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
