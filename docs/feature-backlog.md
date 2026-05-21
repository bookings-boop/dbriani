# Feature Backlog

Deferred feature requests for the Dubriani AI Agent — captured with enough
detail to be picked up later. Not yet built. See `docs/handoff-2026-05-21.md`
for live-system status and `docs/decisions.md` for the build history.

---

## FR-1 — Edit button → Claude feedback loop (shared capability)

**Requested by the operator (Zayn). Re-scoped 2026-05-21.**

The draft card keeps its four buttons — **Send / Edit / Regen / Skip** — but
their roles are made consistent: **Send is the only button that ever delivers
a message to the customer. Edit, Regen and Skip are all internal.**

The change is **Edit**. Today Edit means "type the final message yourself" —
and that typed text is sent **straight to the customer** with no further
approval (see *Current behaviour* below). Re-scoped:

> **Edit (new):** press Edit → the bot asks *"What should Claude change?"* →
> the operator types **feedback / directions for Claude** (not the final text)
> — e.g. *"ask his date first"*, *"shorter, drop the price"*, *"he's a VIP, be
> warmer"*, *"suggest how to pressure him"* → Claude re-drafts using
> `{current draft + conversation context + feedback}` → a **new draft card** is
> shown with the same four buttons. **Nothing is sent.** Press Edit again to
> iterate — this is the refinement loop. The customer receives a message only
> when the operator presses **Send**.

**Regen** — re-draft with no feedback (unchanged). **Skip** — dismiss
(unchanged). Both internal.

### Current behaviour (live workflow — to be changed)
Verified 2026-05-21 against the live workflow. Today a customer can be messaged
**four** ways and only one is the Send button:
1. **Send** button → `Prepare Send` → `Send One to Customer`. *(the intended one)*
2. **Edit** button → `Set Awaiting Edit` (marks the draft `awaiting_edit`) →
   prompts for text → the operator's typed text → `Process Text Reply` (the
   `awaiting_edit` branch) → `Send to Customer (Manual)` → **delivered
   immediately.**
3. **Reply to a draft card** with text (e.g. a payment link after Send) →
   `Process Text Reply` (the `reply_to_message` branch) → `Send to Customer
   (Manual)` → delivered immediately.
4. **`/send <chatId> <message>`** slash command → `Send to Customer (Manual)`.

The re-scope removes #2: the `awaiting_edit` text routes into a new **refine**
sub-path (build refine prompt → Claude → render a new draft card) instead of
`Send to Customer (Manual)`. #3 and #4 — see the open question below.

### Entry points for the loop
- **Edit button** on any draft card — the primary entry point.
- **New-lead openers (FR-2)** — the opener card has the same four buttons, so
  Edit refines it the same way.

### Design notes
- **Approval-gated** — a re-drafted message still goes out only on **Send**.
- **Not Hermes-dependent** — the refine call is just another Claude call
  (`{draft + context + feedback} → revised draft`), built like the existing
  `Claude AI (Regen)` path. Buildable on the current live workflow.
- **Mechanism:** repoint `Set Awaiting Edit` / the `awaiting_edit` status from
  "awaiting replacement text" to "awaiting feedback"; add a Build Refine Prompt
  → Claude → render-card sub-path; route the `awaiting_edit` text there instead
  of to `Send to Customer (Manual)`.

### ⚠️ Open question — the other non-Send send paths
"Only Send sends" is *not* true today (paths #3 and #4 above). Path #3
(reply-to-card) is the operator's quick way to fire a follow-up like a payment
link — and is the path the wrong-customer bug was on (now fixed, newest-match).
**Pending operator decision:** keep #3/#4 as deliberate direct-sends, or fold
them into a confirm-then-Send too?

### Effort estimate
~4–6 nodes: repurpose the Edit path, add the refine sub-path (Build Refine
Prompt → Claude → render card), rewire `Route Text Action`. ~half a day to
1 day with testing.

### Status
**Deferred** — bundled with FR-2 (shared loop). Buildable on the live workflow;
does **not** need Hermes.

---

## FR-2 — New-lead intake via Telegram (outbound conversation start)

**Requested by the operator (Zayn), 2026-05-21.**

> "I want to be able to send to the Telegram bot a new lead with name and phone
> number and details if any, and have the bot start a chat with the customer."

### Intended flow
1. Operator messages the approval bot — `/lead` keyword, then **free-form** text:
   ```
   /lead John Smith +971501234567 — returning client, wants a 50ft yacht
   Saturday sunset, 8 guests, push the sunset package, no hard-selling
   ```
2. Telegram trigger → router detects the `/lead` keyword → **Parse Lead** node
   regex-extracts the phone, normalizes it to a WAHA chatId (`<digits>@c.us`),
   and passes the remaining free-form text to Claude as context.
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
- **Depends on FR-1** — the conversational handling (below) is the FR-1 loop;
  FR-2 and FR-1 are built together.

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

### Conversational handling — uses FR-1 (operator decision, 2026-05-21)
Beyond instructions written into the lead text, the operator can **steer a
lead conversationally** — the FR-1 Edit feedback loop applies to new-lead
openers. After the opener draft card appears (and on any later draft in that
conversation), the operator presses **Edit** and types feedback for Claude —
*"ask his date first"*, *"shorter"*, *"he's a VIP, be warmer"*, *"suggest how
to pressure him"* — and the bot re-drafts, iterating before Send. So per-lead
handling instructions can be given **two ways**:
- **at intake** — written into the free-form lead text (one-shot); and/or
- **in the loop** — conversationally, on the draft card (FR-1).

This makes FR-1 and FR-2 one connected capability — **FR-2 is built together
with FR-1**.

### Effort estimate
Intake + outbound path ~4–5 nodes, plus the FR-1 loop ~3–5 nodes. Built
together, roughly **1 day** including testing.

### Status
**Build-ready** — all decisions resolved (2026-05-21). Parked in the backlog
(task 3) per the operator. Bundled with FR-1 (shared conversational loop);
neither needs Hermes — both can be built on the current live workflow on
request.

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
