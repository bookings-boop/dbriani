# Feature Backlog

Deferred feature requests for the Dubriani AI Agent — captured with enough
detail to be picked up later. Not yet built. See `docs/handoff-2026-05-21.md`
for live-system status and `docs/decisions.md` for the build history.

---

## FR-1 — Skip → conversational directions

**Requested by the operator (Zayn).**

After tapping **Skip** on a draft card, let the operator give Claude
conversational directions instead of just dismissing the draft — e.g.
*"ignore"*, *"ask him first how he wants to pay"*, *"suggest ways to put
pressure on him"* — and have Claude reply back to the operator so they decide
the next reply together (a short back-and-forth before anything goes to the
customer).

- **Status:** deferred until the Hermes integration is stable + re-deployed
  (this feature is most natural on the Hermes refinement loop).
- **Approval-gated:** any message that results still goes through the normal
  approval card before reaching the customer.

---

## FR-2 — New-lead intake via Telegram (outbound conversation start)

**Requested by the operator (Zayn), 2026-05-21.**

> "I want to be able to send to the Telegram bot a new lead with name and phone
> number and details if any, and have the bot start a chat with the customer."

### Intended flow
1. Operator messages the approval bot, e.g.:
   ```
   /lead
   Name: John Smith
   Phone: +971501234567
   Details: wants a 50ft yacht Saturday sunset, 8 guests
   ```
2. Telegram trigger → router detects the `/lead` command → **Parse Lead** node
   extracts name / phone / details and normalizes the phone to a WAHA chatId
   (`<digits>@c.us`).
3. Build an outbound drafting prompt — *"New outbound lead, no prior message.
   Draft Maria's first-contact WhatsApp opener to &lt;name&gt;. Context:
   &lt;details&gt;."* → Claude drafts the opening message.
4. Push to the `pendingQueue` (status `pending`, `customer_phone` = lead number,
   `customer_name` = name) → send the **normal Telegram approval card**
   (Send / Edit / Regen / Skip).
5. Operator taps **Send** → the existing Send path delivers it via WAHA to the
   new number.
6. The customer's reply arrives on the normal inbound WAHA-webhook flow — from
   there it is an ordinary conversation.

### Fixed design decisions
- **Approval-gated** — the first-contact message is *never* auto-sent. It goes
  through the same approval card as every other draft (project principle:
  zero autonomous sends).
- **Reuses the existing pipeline** — only the intake + first-prompt path is new
  (~4–5 nodes added to the live workflow via a surgery script, same pattern as
  `scripts/fix_reply_routing.py`).
- **Independent of Hermes** — this could be built on the *current* live
  pre-Hermes workflow; it does not need the paused Hermes integration. Held in
  the backlog per the operator's call, but can be un-deferred if wanted.

### ⚠️ Risk to flag before building
WAHA is the **unofficial** WhatsApp API. Messaging numbers that have **not**
messaged the business first (cold outreach) is the single biggest trigger for
WhatsApp number bans. Recommended guardrails:
- Use only for **warm** leads (referrals, inquiries from other channels — people
  who expect to be contacted), not cold lists.
- Keep daily outbound volume low.
- The proper long-term fix is **Phase 1C — Meta WhatsApp Cloud API**, which
  supports approved message templates for compliant first-contact.

### Resolved decisions (operator, 2026-05-21)
- **Q1 — input format: free-form.** The operator types the lead naturally — no
  rigid `Name:/Phone:/Details:` template. Parsing: a **regex extracts the phone
  number** (the routing anchor — it picks the digit run of phone length,
  ≥9 digits, so guest counts / "50ft" / dates like "25" are ignored); the full
  free-form text + the extracted phone is handed to Claude, which infers the
  name and context. If no valid phone is found, the bot replies to the operator
  ("couldn't find a phone number — please resend") rather than guessing.
- **Q2 — phone format: accept both.** International with a `+` prefix
  (`+9715…`) **and** local UAE mobiles (`05x xxx xxxx`). Normalization: strip
  `+` / `00` and inner spaces/dashes; a leading `0` on a local mobile →
  replace with `971`; numbers already starting `971` pass through. Result →
  `<digits>@c.us` for WAHA. (UAE country code assumed — Dubriani is Dubai-based.)
- **Q3 — opener style: fully Claude-drafted.** No fixed template — the opener
  is composed from the lead details. Subsequent replies adapt to the customer's
  responses through the existing conversation flow (nothing extra needed).

### Effort estimate
~4–5 nodes added to the live 40-node workflow via a surgery script; roughly
half a day including testing.

### Status
**Build-ready** — all open questions resolved (2026-05-21). Parked in the
backlog (task 3) per the operator; independent of Hermes, so it can be built
on the current live workflow on request.

---

## FR-3 — Batch rapid-fire customer messages (debounce before drafting)

**Requested by the operator (Zayn), 2026-05-21.**

> "When a customer sends 2 lines — 'good afternoon', 'yes for 25 Monday' — in
> Telegram it comes as 2 different messages. Take ~30 seconds to collect all
> the messages and context, then message me once, so I don't miss any message
> and don't get unnecessary separate messages to respond to."

### Problem
Customers often spread one thought across several WhatsApp messages sent
seconds apart. Each arrives as its **own** WAHA webhook event, so the workflow
drafts a separate reply to each — the operator gets **two (or more) draft
cards** for what is really one message, can miss the later one, or wastes a
reply on a fragment ("good afternoon" alone).

### Intended behaviour
When an inbound message arrives, hold it for a short **quiet-window**
(~30s, configurable). If the same customer sends more messages inside the
window, keep collecting and restart the timer. Once the customer has been
silent for the full window, draft **one** reply against the **combined**
messages + conversation context, and send the operator **one** draft card.

### Proposed design (n8n)
- After the webhook extracts `customer_phone` + text, a **Buffer Message** node
  appends `{text, ts}` to `staticData.global.inboundBuffer[phone]` and stamps
  `lastSeq[phone]` with a unique token for this message.
- A **Wait** node pauses this execution ~30s.
- A **Flush Check** node re-reads `staticData`:
  - if `lastSeq[phone]` still equals this execution's token (full 30s of
    silence) → **flush**: join all buffered messages, clear the buffer,
    continue into the existing `Build Prompt → Claude` drafting path;
  - if a newer message arrived (token changed) → this execution ends quietly;
    the newest message's execution does the flush. (Last-writer-wins.)
- Config: window length default **30s**, configurable.

### Trade-off
Every reply now appears **~30s later** (the quiet-window). Acceptable here —
the operator approves manually anyway and 30s ≪ approval time — but it is a
conscious choice, stated so.

### Bonus
Fewer duplicate draft cards ⇒ fewer `pendingQueue` entries ⇒ less queue bloat
and a smaller `telegram_message_id` collision surface (related to the
reply-routing bug fixed 2026-05-21).

### Risk
Touches the **live inbound path** — the most critical path — right after the
incident. Needs careful testing; rollback backups exist. The last-writer-wins
guard means a `staticData` race degrades, at worst, to today's behaviour (a
double draft) — no regression.

### Effort estimate
~4–6 nodes added to the live workflow via a surgery script; ~half a day with
testing.

### Status
**Deferred** per the operator (2026-05-21) — build later. Independent of
Hermes; can be built on the current live workflow whenever wanted.
