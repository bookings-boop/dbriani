# Autonomous Mode — design & status

Autonomous mode lets the operator release a single conversation to Hermes:
Hermes drafts **and sends** without per-message approval.

> **Status (2026-05-21): BUILT & DEPLOYED, dormant.** Every conversation
> defaults to `approval`; the auto-send branch only fires for a conversation
> explicitly set to `autonomous`. Nothing auto-activates. **Not yet behaviourally
> tested** — see "Testing" below; test with a number you control before using
> it on real customers.

---

## How it works (live)

**`conversation_modes` table** — one row per mode change. `get_mode()` reads the
latest; **fail-closed** (error / missing / unknown → `approval`).

**Drafting path** — after `Parse Response`, the `IF Autonomous` node checks
`conversation_mode`:
- `=== 'autonomous'` → `Auto-Send to Customer` (WAHA) → `Auto-Send FYI`
  (a Telegram notice to the operator — no buttons).
- anything else → `Queue & Format → Send Draft to Telegram` (normal approval).

The `IF` node routes every non-`autonomous` value (incl. empty/error) to the
approval branch — it cannot misfire to auto-send.

**Operator commands** (operator Telegram text):

| Operator sends | Effect |
|---|---|
| reply `let it run` / `you continue` to a draft | that customer → `autonomous` |
| reply `take back` to a draft | that customer → `approval` |
| reply `pause` to a draft | that customer → `paused` |
| `/manual` | **kill switch** — every conversation → `approval` |

Commands route `Process Text Reply → Route Text Action[set_mode] → Set
Conversation Mode` (bridge `POST /set-mode`) `→ Confirm Mode`.

## ⚠️ Built but NOT yet included — safety caps & auto-break (spec §5.7)

The following are **not implemented** — flagged for review:
- **Daily cap** (≤20 auto-sends/day) and **per-conversation cap** (≤5
  consecutive) — NOT enforced.
- **Automatic break conditions** (negative sentiment, hard-stop topics, payment
  steps auto-revert to approval) — NOT implemented.

**Consequence:** a conversation set to `autonomous` will auto-send a reply to
**every** customer message until the operator intervenes (`take back` or
`/manual`). The current safety is: per-conversation opt-in + the `/manual`
kill switch only. **Recommend adding the caps before real use** — design is in
spec §5.7; counters can live in `conversation_modes.break_conditions` (JSONB).

## Testing (do before real use — supervised, with a test WhatsApp number)
1. Reply `let it run` to a draft from a number you control → expect a
   "✅ Conversation mode … → autonomous" confirmation.
2. Message again from that number → expect the reply to **auto-send** (no
   approval card) + a "🤖 AUTO-SENT" notice to you.
3. Reply `take back` to one of its drafts → confirm it returns to `approval`.
4. Send `/manual` → confirm every conversation is back to `approval`.
5. Only then consider it usable. **Never enable it by default; never
   auto-activate it.**
