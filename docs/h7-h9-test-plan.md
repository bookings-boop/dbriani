# Autonomous Mode — Safe Test Plan (H7–H9 + caps)

How to test autonomous mode end to end **with a phone you control** — without
risk to real customers.

> ⛔ **PREREQUISITE:** drafting must be working. As of the overnight build it is
> **down** — Hermes → Anthropic returns `401 invalid x-api-key` / `400 usage
> limit`. Fix `ANTHROPIC_API_KEY` in `~/.hermes/.env` (and/or Anthropic usage),
> `systemctl --user restart hermes-bridge`, confirm a normal draft works
> (test H1), **then** run the below.

## Setup
- Use a **second phone / WhatsApp number you own** as the "test customer".
- Send it the Dubriani number first so a conversation exists.
- Keep the n8n Executions view and the Telegram approval chat open.
- After testing, run `/manual` to clear all autonomous modes.

## H7 — Activate autonomous mode
1. From the test phone, message the Dubriani number (e.g. *"hi, yacht for
   saturday?"*).
2. A normal draft card appears in Telegram.
3. **Reply to that card** with `let it run`.
4. **Expect:** *"✅ Conversation mode — <name> → autonomous"* + a warning that
   Hermes will now reply without approval.
   *Verify:* `SELECT mode FROM conversation_modes WHERE customer_id='<test>'
   ORDER BY id DESC LIMIT 1;` → `autonomous`.

## H8 — Auto-send
1. From the test phone, send another message.
2. **Expect:** the reply **arrives on the test phone automatically** — no
   approval card — and a *"🤖 AUTO-SENT (autonomous mode)"* notice appears in
   your Telegram.
3. Confirm the reply text is sane. If not → `/manual` immediately.

## H8b — Per-conversation checkpoint cap (5 consecutive)
1. With the test phone still autonomous, send **5 messages** in a row (waiting
   for each auto-send). Messages 1–5 auto-send.
2. Send a **6th** message.
3. **Expect:** the 6th does **NOT** auto-send — it arrives as an approval card
   whose notes start *"🛑 autonomous → approval: checkpoint after 5
   consecutive…"*. This is the safety cap forcing a human checkpoint.
4. Approve (or refine) it normally — that counts as your intervention and the
   streak resets.

## H9 — Take back
1. Reply `take back` to any draft card from the test conversation.
2. **Expect:** *"✅ … → approval"*. Further messages from the test phone go
   back to normal approval cards.

## Cap inspection & kill switch
- Send `/caps` → expect the cap status (daily used/limit, per-conversation,
  sampling, autonomous count).
- Send `/manual` → expect *"all conversations set to approval"*; verify every
  row's latest mode is `approval`.

## Notes
- **Daily cap (20/day)** and **5% QC sampling** are hard to force by hand —
  trust them or temporarily lower `CAP_DAILY_LIMIT` / raise `CAP_SAMPLE_PCT`
  in `~/hermes-bridge/.env` + restart the bridge to observe them, then restore.
- Anything unexpected → `/manual` is the kill switch.
- Do **not** run H7–H9 on a real customer conversation.
