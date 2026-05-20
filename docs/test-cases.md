# Phase 1B — Deployment Acceptance Tests

Run after the workflow is imported, credentials linked, and the workflow
activated (HANDOFF.md §6, step 4). Tests T1–T6 must all pass before Phase 1B
is considered live.

| Test | Action | Expected |
|------|--------|----------|
| **T1: Send** | Send "test send" from a personal WhatsApp to the Dubriani number. Tap **✅ Send** in Telegram. | Customer receives the draft. Telegram message updates to "✅ SENT to …". Buttons disappear. |
| **T2: Skip** | New customer message. Tap **❌ Skip**. | Customer receives nothing. Telegram updates to "❌ SKIPPED …". |
| **T3: Edit** | New customer message. Tap **✏️ Edit**, then type your replacement text. | Customer receives *your* text. Telegram confirms "✏️ SENT YOUR VERSION …". |
| **T4: Regen** | New customer message. Tap **🔁 Regen**. | Telegram message is replaced with a fresh draft + buttons. Send/Skip work on the regenerated draft. |
| **T5: Multi-pending** | Send 2 customer messages in quick succession. | 2 separate Telegram draft messages, each independently actionable. |
| **T6: Unauth** | From a *different* Telegram account, message the bot. | Bot ignores it. The N8N execution shows `Verify Admin` dropped the update. |

## Known limitations during MVP testing

- **T5 is a race, not a guarantee (risk R10).** The pending queue lives in N8N
  workflow static data, which is loaded per-execution and saved on success.
  Two *truly simultaneous* customer messages can have one execution overwrite
  the other's queue write — losing a draft. T5 will usually pass when messages
  are 1–2 s apart; it can fail under genuine concurrency. A green T5 is *not*
  proof the queue is concurrency-safe. Redis migration (deferred, step 12)
  fixes this properly.

- After **✏️ Edit** is tapped, the draft's buttons are removed — the only way
  forward is to type a replacement. There is no "cancel edit" in the 4-button
  MVP.

## Risk fixes folded into this deployment (Step 4)

Per the approved plan, these are applied to the workflow before activation:

- **R1** — error-output branches on Claude AI / Send to Telegram / Send to
  Customer → a single "Alert Zayn" Telegram message with failure context.
  The workflow does not hard-fail; the draft stays in the queue.
- **R3** — the Telegram preview shows *all* `messages[]` (numbered); on Send,
  they go to WhatsApp sequentially with a 1–2 s gap.
- **R4** — a 2nd Edit is refused while another draft is `awaiting_edit`
  (`answerCallbackQuery` returns "Resolve draft A first."); no state change.
- **R8** — `Filter Inbound` gains a 4th condition: `payload.from` ends with
  `@c.us` (Phase 1 handles personal chats only).

Deferred to post-MVP: **R5** (double-send idempotency guard) and **R9**
(regen failure rendering / `max_tokens` bump).
