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

### Open questions to resolve before build
- **Q1 — input format:** structured `Name: / Phone: / Details:` lines (proposed),
  or a free-form one-liner?
- **Q2 — phone format:** what will the operator paste — always `+971…`
  international, or local `05x`? (Determines phone normalization rules.)
- **Q3 — opener style:** fully Claude-drafted each time, or anchored to a fixed
  greeting template?

### Effort estimate
~4–5 nodes added to the live 40-node workflow via a surgery script; roughly
half a day including testing.
