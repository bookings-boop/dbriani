#!/usr/bin/env python3
"""4-B (2026-06-09): re-point a merged dup's durable history (conversation_messages
+ conversation_state) onto its canonical survivor, so every read under
canonicalize_cid sees ONE unified thread instead of an orphaned split. Today the
merge writes only customer_facts.merged_into, stranding the dup's rows.

Collision-safe vs the partial-unique index (customer_id, msg_id) and the
conversation_state PK (customer_id): DELETE the dup's shared msg_ids first, then
UPDATE the rest to canon; state is INSERT…ON CONFLICT GREATEST-merge (never lose a
fresher timestamp) then DELETE the dup state.

Locks the pure SQL builder + the flag-gate. Run: python3 hermes-bridge/test_reconcile_repoint.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402

DUP = "208134232662176@lid"
CAN = "971544236263@c.us"


def test_builder_returns_four_statements():
    assert len(server._repoint_identity_sql(DUP, CAN)) == 4


def test_delete_shared_then_move():
    s = server._repoint_identity_sql(DUP, CAN)
    # 1: delete dup's msg_ids that already exist under canon (the shared echoes)
    assert "DELETE FROM conversation_messages" in s[0] and "EXISTS" in s[0]
    assert ("'%s'" % DUP) in s[0] and ("'%s'" % CAN) in s[0]
    # 2: move the remaining dup rows (+ NULL-msg_id rows) to canon
    assert s[1].startswith("UPDATE conversation_messages SET customer_id = '%s'" % CAN)
    assert ("WHERE customer_id = '%s'" % DUP) in s[1]


def test_state_greatest_merge_then_delete():
    s = server._repoint_identity_sql(DUP, CAN)
    assert "INSERT INTO conversation_state" in s[2]
    assert "ON CONFLICT (customer_id) DO UPDATE" in s[2]
    assert ("GREATEST(conversation_state.last_operator_reply_at, "
            "EXCLUDED.last_operator_reply_at)") in s[2]
    assert ("GREATEST(conversation_state.last_customer_message_at, "
            "EXCLUDED.last_customer_message_at)") in s[2]
    assert s[3] == "DELETE FROM conversation_state WHERE customer_id = '%s'" % DUP


def test_sql_escapes_single_quotes():
    s = server._repoint_identity_sql("a'b@lid", CAN)
    assert "'a''b@lid'" in s[3]


def test_flag_gate_and_repoint_wired_in_reconcile():
    import routes
    co = routes.handle_reconcile_identities.__code__
    names = co.co_names + co.co_varnames + tuple(str(c) for c in co.co_consts)
    assert "RECONCILE_REPOINT_ENABLED" in names, "flag-gate missing"
    assert "_repoint_identity_sql" in names, "re-point not wired into reconcile"


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
