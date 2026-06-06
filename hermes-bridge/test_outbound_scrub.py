#!/usr/bin/env python3
"""Send-boundary scrub: internal operator notes / names must NEVER reach a
customer.

2026-06-06 incident (Xeno Accounts, +971506798997): the customer received,
twice, a file caption containing the verbatim internal operator note
"Premium BBQ menu - sending alongside fine dining per operator rule 23
(Zayn to also send catering-fine-dining manually)". "Zayn" is the OPERATOR's
name (the /draft response field is `notes_for_zayn`). The leak path was a
file-send caption (sendFile, with a sendText fallback) that bypassed
sanitize_draft_messages() entirely (that only scrubbed the `messages` list).

Fix: a scrub_outbound() applied at the WAHA send boundary
(waha_send_text/file/image) so EVERY customer-facing path is covered.
Telegram operator cards go through _tg_post (a different path) and are NOT
scrubbed, so the operator still sees the notes.

Run: python3 hermes-bridge/test_outbound_scrub.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import waha  # noqa: E402
from waha import scrub_outbound  # noqa: E402

# The exact text that leaked to the customer (2026-06-06).
LEAK = ("Premium BBQ menu - sending alongside fine dining per operator "
        "rule 23 (Zayn to also send catering-fine-dining manually)\n\n"
        "https://drive.google.com/file/d/1pE5ldeMUcYWddxWFeAwLijc3UcpLCFSu/view")


def test_strips_the_real_leak():
    out = scrub_outbound(LEAK)
    low = out.lower()
    assert "zayn" not in low, out
    assert "operator rule" not in low, out
    assert "manually" not in low, out
    assert "to also send" not in low, out
    # the legit, customer-safe parts survive
    assert "premium bbq menu" in low, out
    assert "drive.google.com/file/d/1pE5" in out, out


def test_preserves_legit_caption():
    legit = ("Bliss 55 yacht brochure with photos\n"
             "https://drive.google.com/file/d/1BQiwLN6zll4Peg/view")
    out = scrub_outbound(legit)
    assert "Bliss 55 yacht brochure with photos" in out, out
    assert "drive.google.com/file/d/1BQiwLN6zll4Peg" in out, out


def test_strips_standalone_operator_name():
    out = scrub_outbound("let me have Zayn confirm your slot and get back to you")
    assert "zayn" not in out.lower(), out
    assert "your slot" in out.lower(), out


def test_strips_parenthetical_internal_instruction():
    out = scrub_outbound("the fine dining menu for 2 (send manually per operator rule 23)")
    low = out.lower()
    assert "fine dining menu for 2" in low, out
    assert "operator rule" not in low, out
    assert "manually" not in low, out


def test_strips_notes_for_zayn_marker_block():
    out = scrub_outbound("here are the photos\n\nnotes_for_zayn: push the upsell hard")
    assert "notes_for_zayn" not in out.lower(), out
    assert "push the upsell" not in out.lower(), out
    assert "here are the photos" in out.lower(), out


def test_empty_and_none_safe():
    assert scrub_outbound("") == ""
    assert scrub_outbound(None) == ""


def test_send_text_applies_scrub():
    captured = {}

    def fake_post(path, body, timeout=30):
        captured["path"] = path
        captured["body"] = body
        return {"ok": True}, None

    orig = waha._waha_post
    try:
        waha._waha_post = fake_post
        ok, err = waha.waha_send_text("123@c.us", LEAK)
        assert ok and err is None, (ok, err)
        assert "zayn" not in captured["body"]["text"].lower(), captured
        assert "operator rule" not in captured["body"]["text"].lower(), captured
    finally:
        waha._waha_post = orig


def test_send_file_caption_scrubbed():
    captured = {}

    def fake_post(path, body, timeout=30):
        captured["body"] = body
        return {"ok": True}, None

    orig = waha._waha_post
    try:
        waha._waha_post = fake_post
        ok, err = waha.waha_send_file("123@c.us", "https://x/f.pdf", caption=LEAK)
        assert ok and err is None, (ok, err)
        assert "zayn" not in captured["body"]["caption"].lower(), captured
        # the file itself still sends (legit payload), caption just cleaned
        assert captured["body"]["file"]["url"] == "https://x/f.pdf"
    finally:
        waha._waha_post = orig


def test_send_text_blocks_when_scrub_empties_message():
    # A message that is ENTIRELY an internal note must not be sent as a blank.
    captured = {"called": False}

    def fake_post(path, body, timeout=30):
        captured["called"] = True
        return {"ok": True}, None

    orig = waha._waha_post
    try:
        waha._waha_post = fake_post
        ok, err = waha.waha_send_text("123@c.us", "notes_for_zayn: internal only")
        assert ok is False, (ok, err)
        assert captured["called"] is False, "must not POST a blank message"
    finally:
        waha._waha_post = orig


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} outbound_scrub tests passed")
