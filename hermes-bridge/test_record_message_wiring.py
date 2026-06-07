#!/usr/bin/env python3
"""PART 2 — wire the durable-store write-path (migration 010).

PART 1 built the FAIL-SAFE writer `server.record_message` + the pure
`_record_message_sql` builder. PART 2 wires it at every point a message body
is in hand so the analyzer/drafter stop depending on a live WAHA fetch:

  (a) INBOUND  — handle_draft records the lead's `incoming_message` (direction
      'in') BEFORE Hermes runs (record-before-AI), and handle_conversation_state
      records a `customer_message` body when n8n includes it (extended payload).
  (b) OUTBOUND — the claim-send WON branch records the approved draft bubbles
      (direction 'out') via `server.record_outbound_draft`, keyed by draft id so
      a replayed claim dedupes instead of double-recording.
  (c) ENDPOINT — POST /record-message (`handle_record_message`) lets n8n post a
      body it has (record-before-AI right after the WAHA Webhook node, and
      operator DIRECT fromMe replies). FAIL-SAFE: always 200, never raises,
      idempotent on msg_id.

Like _draft_log_write / record_message, every wired call MUST be fail-safe:
NEVER raise, NEVER block the message flow, and no-op cleanly BEFORE migration
010 is applied. These tests lock that contract WITHOUT a DB (record_message /
upsert_conversation_state are monkeypatched).

Run: python3 hermes-bridge/test_record_message_wiring.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import routes  # noqa: E402
import server  # noqa: E402


class _Send:
    """Capture a route handler's send(status, body) call."""

    def __init__(self):
        self.status = None
        self.body = None

    def __call__(self, status, body):
        self.status = status
        self.body = body


class _PatchRecord:
    """Swap server.record_message with a capturing/controllable stub for the
    duration of a block. record_outbound_draft + the route handlers all reach
    record_message via `from server import record_message` at call time, so
    patching the module attribute is sufficient."""

    def __init__(self, ret=True, raises=False):
        self.calls = []
        self._ret = ret
        self._raises = raises

    def _fn(self, cid, direction, body, msg_id=None):
        self.calls.append((cid, direction, body, msg_id))
        if self._raises:
            raise RuntimeError("record_message exploded")
        return self._ret

    def __enter__(self):
        self._orig = server.record_message
        server.record_message = self._fn
        return self

    def __exit__(self, *exc):
        server.record_message = self._orig
        return False


# --- (c) POST /record-message endpoint -------------------------------------

def test_endpoint_passes_through_and_reports_ok():
    with _PatchRecord(ret=True) as p:
        s = _Send()
        routes.handle_record_message(
            {"customer_id": "x@c.us", "direction": "in",
             "body": "hello", "msg_id": "M1"}, s)
    assert s.status == 200, s.status
    assert s.body.get("ok") is True, s.body
    assert p.calls == [("x@c.us", "in", "hello", "M1")], p.calls


def test_endpoint_idempotent_passes_msg_id():
    # n8n posts at-least-once; the provider msg_id flows through so SQL
    # ON CONFLICT dedupes a replay. Accepts message_id/id aliases too.
    with _PatchRecord(ret=True) as p:
        routes.handle_record_message(
            {"customer_phone": "x@c.us", "direction": "out",
             "text": "hi there", "message_id": "WAMID9"}, _Send())
    assert p.calls[0][3] == "WAMID9", p.calls


def test_endpoint_always_200_on_bad_input():
    # record_message itself rejects bad direction (returns False) — the
    # endpoint must STILL return 200 so the n8n inbound path never breaks.
    with _PatchRecord(ret=False):
        s = _Send()
        routes.handle_record_message(
            {"customer_id": "x@c.us", "direction": "sideways",
             "body": "hi"}, s)
    assert s.status == 200, s.status
    assert s.body.get("ok") is False, s.body


def test_endpoint_never_raises_even_if_writer_raises():
    # Defense in depth: even if record_message somehow raised, the endpoint
    # swallows it and returns 200 (the write-path runs on EVERY inbound).
    with _PatchRecord(raises=True):
        s = _Send()
        routes.handle_record_message(
            {"customer_id": "x@c.us", "direction": "in", "body": "hi"}, s)
    assert s.status == 200, s.status
    assert s.body.get("ok") is False, s.body


def test_endpoint_empty_payload_is_safe_200():
    with _PatchRecord(ret=False):
        s = _Send()
        routes.handle_record_message({}, s)
    assert s.status == 200, s.status
    assert s.body.get("ok") is False, s.body


# --- (b) OUTBOUND: record_outbound_draft -----------------------------------

def test_outbound_records_joined_bubbles_keyed_by_draft_id():
    draft = {"id": "d-123", "customer_phone": "x@c.us",
             "messages": ["Hi Sam,", "Your yacht is ready."]}
    with _PatchRecord(ret=True) as p:
        assert server.record_outbound_draft(draft) is True
    assert len(p.calls) == 1, p.calls
    cid, direction, body, msg_id = p.calls[0]
    assert cid == "x@c.us"
    assert direction == "out"
    assert "Hi Sam," in body and "Your yacht is ready." in body
    # draft id is the dedupe key (claim-send WON fires once, but be defensive)
    assert msg_id == "d-123", msg_id


def test_outbound_filters_non_string_bubbles():
    draft = {"id": "d-9", "customer_phone": "x@c.us",
             "messages": [{"junk": 1}, "real text", 42, ""]}
    with _PatchRecord(ret=True) as p:
        assert server.record_outbound_draft(draft) is True
    assert p.calls[0][2] == "real text", p.calls


def test_outbound_no_body_does_not_touch_writer():
    # All bubbles non-string/empty -> nothing to record -> never call writer.
    draft = {"id": "d-1", "customer_phone": "x@c.us",
             "messages": [{"x": 1}, ""]}
    with _PatchRecord(ret=True) as p:
        assert server.record_outbound_draft(draft) is False
    assert p.calls == [], p.calls


def test_outbound_missing_phone_is_safe_false():
    with _PatchRecord(ret=True) as p:
        assert server.record_outbound_draft(
            {"id": "d", "messages": ["hi"]}) is False
    assert p.calls == [], p.calls


def test_outbound_non_dict_is_safe_false():
    with _PatchRecord(ret=True) as p:
        assert server.record_outbound_draft(None) is False
        assert server.record_outbound_draft("not a draft") is False
    assert p.calls == [], p.calls


def test_outbound_never_raises():
    # record_outbound_draft mirrors record_message discipline: never raises.
    with _PatchRecord(raises=True):
        # writer raises -> helper swallows -> False, no exception escapes
        assert server.record_outbound_draft(
            {"id": "d", "customer_phone": "x@c.us",
             "messages": ["hi"]}) is False


# --- (a) INBOUND: conversation_state customer_message body -----------------

class _PatchConvState:
    """Stub upsert_conversation_state so handle_conversation_state runs
    DB-free; pair with _PatchRecord to capture the inbound record."""

    def __init__(self):
        self.calls = []

    def _fn(self, cid, event):
        self.calls.append((cid, event))
        return ("", None)

    def __enter__(self):
        self._orig = server.upsert_conversation_state
        server.upsert_conversation_state = self._fn
        return self

    def __exit__(self, *exc):
        server.upsert_conversation_state = self._orig
        return False


def test_conversation_state_records_inbound_body_when_present():
    with _PatchConvState(), _PatchRecord(ret=True) as p:
        s = _Send()
        routes.handle_conversation_state(
            {"customer_id": "x@c.us", "event": "customer_message",
             "body": "is the boat free saturday?", "msg_id": "M7"}, s)
    assert s.status == 200 and s.body.get("ok") is True, s.body
    assert p.calls == [("x@c.us", "in", "is the boat free saturday?",
                        "M7")], p.calls


def test_conversation_state_no_body_does_not_record():
    # Today's payload carries NO body -> the timing UPSERT still runs, but we
    # must NOT fabricate an empty inbound. (n8n change documented to add body.)
    with _PatchConvState(), _PatchRecord(ret=True) as p:
        routes.handle_conversation_state(
            {"customer_id": "x@c.us", "event": "customer_message"}, _Send())
    assert p.calls == [], p.calls


def test_conversation_state_non_customer_event_does_not_record():
    with _PatchConvState(), _PatchRecord(ret=True) as p:
        routes.handle_conversation_state(
            {"customer_id": "x@c.us", "event": "operator_reply",
             "body": "ignored"}, _Send())
    assert p.calls == [], p.calls


# --- dispatch wiring + import lock -----------------------------------------

def _flat_consts(code):
    out = set()
    for c in code.co_consts:
        out.add(c)
        if isinstance(c, tuple):
            out.update(c)
    return out


def test_endpoint_is_dispatched_in_do_post():
    code = server.Handler.do_POST.__code__
    consts = _flat_consts(code)
    assert "/record-message" in consts, (
        "/record-message is not referenced in do_POST — not in the allow-list "
        "and/or not dispatched")
    assert "handle_record_message" in code.co_names, (
        "do_POST never calls handle_record_message")


def test_server_imports_endpoint_handler():
    assert server.handle_record_message is routes.handle_record_message


def test_draft_handler_records_inbound():
    # handle_draft must reference record_message (the in-bridge record-before-AI
    # inbound capture). Cheap structural lock — no Hermes call needed.
    assert "record_message" in routes.handle_draft.__code__.co_names, (
        "handle_draft does not record the inbound message body")


def test_claim_send_records_outbound():
    # handle_queue's claim-send WON branch must reference record_outbound_draft.
    assert "record_outbound_draft" in routes.handle_queue.__code__.co_names, (
        "claim-send does not record the approved outbound draft")


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn(); print("PASS", fn.__name__)
        except AssertionError as e:
            failed += 1; print("FAIL", fn.__name__, "-", e or "assert")
        except Exception as e:
            failed += 1; print("ERROR", fn.__name__, "-", repr(e))
    print(f"{len(fns)-failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
