#!/usr/bin/env python3
"""Unit tests for the label vocabulary + ranking invariants.

apply_label_transition() itself does DB writes (not pure) — covered by
integration tests. Here we lock down the PURE constants that drive
labeling decisions across the codebase:

  - LABELS frozenset must contain every state the system can produce
  - _LABEL_RANK must order them correctly (sticky-upward guard depends
    on this)
  - DISREGARDED must rank above CONFIRMED (terminal-closed,
    auto-classifier can't bounce it back)
  - PAUSED_* labels must NOT appear in _LABEL_RANK (they're score-floored,
    not ranked)
  - _HARD_DEMOTE_SIGNALS membership

Run: python3 hermes-bridge/test_label_transition.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from server import LABELS, _LABEL_RANK, _HARD_DEMOTE_SIGNALS  # noqa: E402


def test_labels_contain_active_states():
    # Every label the auto-classifier or operator can apply must be in
    # LABELS — _label endpoint rejects anything not in this set.
    for required in ("NEW", "WARM", "HOT", "NEEDS_ATTENTION", "COLD",
                     "WAITING_FOR_PAYMENT", "CONFIRMED", "DISREGARDED",
                     "PAUSED_SPAM", "PAUSED_B2B", "PAUSED_PERSONAL"):
        assert required in LABELS, f"{required} missing from LABELS"


def test_labels_frozen():
    # LABELS is intentionally a frozenset — guards against accidental
    # mutation during request handling.
    assert isinstance(LABELS, frozenset)


def test_rank_ordering_active_tiers():
    # The sticky-upward guard reads _LABEL_RANK to decide whether a
    # weak new signal can demote a strong existing state. Order MUST be:
    # NEW < COLD < WARM < HOT < NEEDS_ATTENTION < WAITING_FOR_PAYMENT
    # < CONFIRMED < DISREGARDED
    expected = ["NEW", "COLD", "WARM", "HOT", "NEEDS_ATTENTION",
                "WAITING_FOR_PAYMENT", "CONFIRMED", "DISREGARDED"]
    for lo, hi in zip(expected, expected[1:]):
        assert _LABEL_RANK[lo] < _LABEL_RANK[hi], (
            f"{lo}({_LABEL_RANK[lo]}) must rank below "
            f"{hi}({_LABEL_RANK[hi]})")


def test_disregarded_above_confirmed():
    # DISREGARDED is terminal — operator (or Hermes) closed the lead.
    # MUST rank above CONFIRMED so the sticky-upward guard refuses to
    # auto-promote them back into /review on a stray HOT/WARM signal.
    assert _LABEL_RANK["DISREGARDED"] > _LABEL_RANK["CONFIRMED"]


def test_scam_in_labels_and_terminal_rank():
    # SCAM (crypto/fraud — Mike) is a terminal state the analysis_guard
    # close-router can produce, so it MUST be in LABELS (the /label endpoint
    # rejects anything not in this set).
    assert "SCAM" in LABELS
    # SCAM is the STICKIEST terminal state — ranked ABOVE LOST (the previous
    # top) so the sticky-upward guard can never bounce a scam lead back into
    # the active queue on a stray HOT/WARM signal. Rank must stay collision-
    # free (test_no_rank_collisions guards the whole map).
    assert _LABEL_RANK["SCAM"] > _LABEL_RANK["LOST"]
    assert _LABEL_RANK["SCAM"] > _LABEL_RANK["DISREGARDED"]


def test_paused_not_in_rank():
    # PAUSED_* labels are handled by score_lead (-10000) — they don't
    # participate in the rank-based promotion ladder. Verify they're
    # NOT in _LABEL_RANK so any code path that does .get(label, 0) on
    # a PAUSED label correctly treats it as floor-rank 0.
    for paused in ("PAUSED_SPAM", "PAUSED_B2B", "PAUSED_PERSONAL"):
        assert paused not in _LABEL_RANK


def test_hard_demote_signals():
    # These signals are strong enough to demote regardless of confidence
    # dampening. Lock the contract.
    for sig in ("date_passed", "cold_decay", "confirmed_terminal", "locked"):
        assert sig in _HARD_DEMOTE_SIGNALS, (
            f"{sig} missing from _HARD_DEMOTE_SIGNALS — sticky-upward "
            "guard will refuse to demote stale leads on this signal")


def test_no_rank_collisions():
    # Every label in _LABEL_RANK must have a UNIQUE rank value —
    # otherwise the sticky-upward guard can't make a deterministic
    # decision between two same-rank labels.
    values = list(_LABEL_RANK.values())
    assert len(values) == len(set(values)), (
        f"_LABEL_RANK has duplicate ranks: {_LABEL_RANK}")


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} label_transition tests passed")
