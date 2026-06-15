#!/usr/bin/env python3
"""Unit tests for the proactive follow-up (ghost-recovery) engine targeting
— BUG G fix, 2026-06-06.

The engine must target the "we replied → customer went silent (ghosted)"
population, NOT "customer spoke last, we owe a reply". Silence is measured
since OUR last reply. Cadence:
  - soft check-in  : 24-72h since our reply, HOT/NEEDS_ATTENTION/WARM
  - last shot      : 3-14d since our reply, HOT/NEEDS_ATTENTION/WARM/COLD,
                     ≥48h cooldown after the soft nudge (enforced in SQL)

Pure pieces only (no DB): _silence_window_for() band mapping and the
_followup_candidate_sql() builder (asserts the corrected predicates so the
semantic inversion can never regress).

Plain-assert style. Run: python3 hermes-bridge/test_followup_eligibility.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from server import (  # noqa: E402
    _silence_window_for,
    _followup_candidate_sql,
    GHOST_RECOVERY_PHRASES,
    GHOST_RECOVERY_WINDOWS,
)


# --- _silence_window_for: soft check-in (24-72h, active labels) --------------
def test_soft_checkin_active_labels_in_band():
    for lab in ("HOT", "NEEDS_ATTENTION", "WARM"):
        assert _silence_window_for(lab, 24.0) == "soft_checkin"
        assert _silence_window_for(lab, 48.0) == "soft_checkin"
        assert _silence_window_for(lab, 72.0) == "soft_checkin"


def test_soft_checkin_below_band_is_none():
    # < 24h since our reply — too soon to nudge.
    assert _silence_window_for("HOT", 23.9) is None
    assert _silence_window_for("WARM", 1.0) is None


# --- _silence_window_for: last shot (3-14d, incl COLD) ----------------------
def test_last_shot_in_band_all_active_labels():
    for lab in ("HOT", "NEEDS_ATTENTION", "WARM", "COLD"):
        assert _silence_window_for(lab, 72.1) == "last_shot"
        assert _silence_window_for(lab, 200.0) == "last_shot"
        assert _silence_window_for(lab, 336.0) == "last_shot"   # 14 days


def test_above_last_shot_band_is_none():
    # > 14d — engine stops; lifecycle (cold-decay → dormancy) takes over.
    assert _silence_window_for("WARM", 336.1) is None
    assert _silence_window_for("COLD", 1000.0) is None


# --- label coverage rules ---------------------------------------------------
def test_cold_gets_no_soft_checkin():
    # COLD is already cold — only the last shot, never the soft check-in.
    assert _silence_window_for("COLD", 48.0) is None


def test_new_never_eligible():
    # NEW is pre-qualification — excluded from the proactive engine entirely.
    assert _silence_window_for("NEW", 48.0) is None
    assert _silence_window_for("NEW", 200.0) is None


def test_terminal_labels_never_eligible():
    for lab in ("CONFIRMED", "WAITING_FOR_PAYMENT", "DISREGARDED", "LOST"):
        assert _silence_window_for(lab, 48.0) is None
        assert _silence_window_for(lab, 200.0) is None


# --- window/phrase consistency invariant ------------------------------------
def test_every_window_has_a_phrase():
    # Anything _silence_window_for can return MUST have a phrase, else the
    # consumer (_draft_followup) falls back silently.
    produced = set()
    for lab in ("HOT", "NEEDS_ATTENTION", "WARM", "COLD"):
        for hrs in (24.0, 48.0, 72.0, 100.0, 336.0):
            w = _silence_window_for(lab, hrs)
            if w:
                produced.add(w)
    assert produced == {"soft_checkin", "last_shot"}
    assert produced <= GHOST_RECOVERY_WINDOWS
    for w in produced:
        assert GHOST_RECOVERY_PHRASES[w].strip()


# --- _followup_candidate_sql: keystone population (BUG G-1) ------------------
def test_sql_targets_we_replied_population():
    sql = _followup_candidate_sql()
    # Correct population: we replied AFTER the customer's last message.
    assert "cs.last_operator_reply_at > cs.last_customer_message_at" in sql


def test_sql_does_not_use_inverted_predicate():
    # The original bug: selecting "customer spoke last, we owe a reply".
    sql = _followup_candidate_sql()
    assert "cs.last_operator_reply_at < cs.last_customer_message_at" not in sql


def test_sql_measures_silence_from_our_reply():
    sql = _followup_candidate_sql()
    assert "EXTRACT(EPOCH FROM (now() - cs.last_operator_reply_at))" in sql
    # timing band anchored on our reply, not the customer message
    assert "now() - cs.last_operator_reply_at" in sql


def test_sql_uses_48h_cooldown_not_oneshot():
    sql = _followup_candidate_sql()
    assert "interval '48 hours'" in sql
    # the old hard one-shot compared nudge-time to the customer message
    assert "cs.last_nudge_drafted_at > cs.last_customer_message_at" not in sql


def test_sql_keeps_followup_cap():
    sql = _followup_candidate_sql()
    assert "followup_count" in sql


def test_sql_excludes_analyzer_killed_leads():
    # Audit #7 (2026-06-07): analyzer_close/score0 demote dead leads to COLD,
    # which is reengage-eligible — don't ghost-recovery a declined/vendor lead.
    # REPAIRED (2026-06-14, belt-fix #3): the guard formerly read
    # cs.last_analysis_signal, a column never written with 'auto:analyzer%'
    # (0/592 rows in prod) — a dead no-op. It now reads the table the signal
    # actually lands in: customer_label_history.signal.
    sql = _followup_candidate_sql()
    assert "auto:analyzer%" in sql
    assert "customer_label_history" in sql
    # the dead predicate on the always-empty conversation_state column is gone
    assert "cs.last_analysis_signal NOT LIKE 'auto:analyzer%'" not in sql


def test_sql_excludes_merged_duplicate_rows():
    # Identity-merge guard (2026-06-07 Yogi spam): the engine must NOT nudge a
    # merged (non-canonical) conversation_state row. Its cooldown/cap state lives
    # on the CANONICAL row (upsert_conversation_state canonicalizes), so reading
    # the merged row (blank last_nudge/followup_count) re-nudges it forever.
    sql = _followup_candidate_sql()
    assert "cf.merged_into IS NULL" in sql


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} followup-eligibility tests passed")
