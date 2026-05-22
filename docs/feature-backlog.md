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

### Resolved — the other non-Send send paths (operator, 2026-05-21)
Paths #3 (reply-to-card follow-up, e.g. a payment link) and #4 (`/send`)
**stay as deliberate direct sends** — they are explicit "send this now"
actions, not draft review. Only the **Edit** button (#2) is re-scoped. So after
FR-1: among the *buttons*, only Send delivers to the customer; reply-to-card
and `/send` remain quick manual send tools by design.

### Effort estimate
~4–6 nodes: repurpose the Edit path, add the refine sub-path (Build Refine
Prompt → Claude → render card), rewire `Route Text Action`. ~half a day to
1 day with testing.

### Status
**✅ Deployed 2026-05-21** — live workflow now 49 nodes. Edit re-scoped to the
feedback loop (Prep Refine → Build Refine Prompt → Claude AI (Refine) → Parse
Refine → Edit Telegram (Refine)). Built by `scripts/build_fr1_fr2.py`.
Awaiting operator behavioural test.

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
**✅ Deployed 2026-05-21** — live workflow now 49 nodes. `/lead` intake live
(Prep Lead Draft → Build Lead Prompt → Claude AI (Lead) → Parse Lead Response
→ the existing Queue & Format → Send Draft to Telegram → Save Telegram MsgID).
Built by `scripts/build_fr1_fr2.py`. Awaiting operator behavioural test.

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
**✅ Deployed 2026-05-21** — live workflow now 52 nodes. 30s quiet-window via
Buffer Message → Debounce Wait → Flush Check inserted before Get Chat History;
Format Context drafts against the combined buffered messages. Built by
`scripts/build_fr3.py`. Awaiting operator behavioural test.

---

## FR-4 — Supervised autonomous mode ("Auto" — delayed auto-send with an intervention window)

**Requested by the operator (Zayn), 2026-05-21.**

> "I want another button to tell Claude to continue the chat itself — but it
> has to always send the updates in the Telegram bot. Never respond faster than
> 1 min, sometimes 2 min, sometimes 3–5 min — too-fast responding shows we have
> nothing to do. When the customer responds it should show the customer's
> response in Telegram, show its suggested response, and the time until it will
> respond — giving the customer time to speak more and giving me time to give
> feedback before it's sent."

### Concept
A **5th button — "Auto"** — on the draft card. Pressing it hands the
conversation to Claude, which continues the back-and-forth itself — but **never
silently**. Every customer message and every proposed reply appears in
Telegram, and every outgoing reply waits out a **visible delay** during which
the operator can review, steer, or stop it.

### Flow (a conversation in Auto mode)
1. Customer sends a WhatsApp message → posted to Telegram (the customer's text).
2. Claude drafts the reply → posted to Telegram as an **Auto card** showing:
   - the proposed reply text,
   - a **countdown** — e.g. *"sending in 3m 40s"*,
   - buttons: **[Edit] [Send now] [Cancel] [Take over]**.
3. A **human-like delay** runs before the reply sends — randomised **1–5 min**
   (never under 1 min; varied so replies don't look robotic).
4. During the window:
   - **Customer sends more** → the new messages are folded in, Claude re-drafts,
     the Auto card + countdown update. (The window deliberately gives the
     customer room to finish — see FR-3.)
   - **Operator presses Edit** → the FR-1 feedback loop: type feedback → Claude
     re-drafts → updated Auto card + countdown.
   - **Send now** → skip the wait, send immediately. **Cancel** → drop this
     reply. **Take over** → exit Auto for this conversation, back to manual
     approval.
5. Countdown hits zero with no intervention → the proposed reply **auto-sends**
   to the customer via WAHA, logged in Telegram as sent.

### Why the delay (operator's rationale)
- **Looks human** — instant replies signal idleness; a luxury brand replies
  considered, not robotic.
- Gives the **customer** time to add follow-up messages before a reply commits.
- Gives the **operator** a window to review and steer before anything sends.

### ⚠️ This introduces autonomous sends
A deliberate, explicit departure from strict "approval-first": after the window
a message reaches the customer **without** the operator pressing Send. Built-in
safeguards: per-conversation opt-in (the Auto button), every message visible in
Telegram, the intervention window, the Take-over exit.

The residual risk is the **unsupervised** window — if the operator isn't
watching (asleep, busy) an Auto conversation keeps replying on the 1–5 min
delays alone. **Resolved (operator, 2026-05-21): add hard safety caps** on top
of the delay window, reusing the Hermes §5.7 work:
- a **daily auto-send cap** (default ~20/day; configurable);
- a **consecutive-checkpoint** — after N auto-replies with no operator input,
  Auto pauses that conversation and asks the operator to confirm before
  continuing;
- an optional **quiet-hours pause** — during a configured window Auto holds
  (does not auto-send) and queues the proposed reply for the operator instead.

Auto mode still runs, but cannot run away unattended.

### Relationship to other items
- **FR-1** (Edit feedback loop) — how the operator steers a proposed auto-reply
  during the window. **FR-4 depends on FR-1.**
- **FR-3** (message debounce) — the Auto window naturally absorbs rapid-fire
  customer messages; FR-4's window and FR-3's quiet-window should share the
  message-buffering logic rather than be built twice.
- **Paused Hermes autonomous mode** — the Hermes integration already built a
  per-conversation autonomous mode (`IF Autonomous` branch, `Auto-Send to
  Customer`, the §5.7 safety caps, `conversation_modes` table, `let it run` /
  `take back` / `/manual` commands). **FR-4 is a refined, *supervised* version
  of it** — reuse/adapt that work; don't rebuild from scratch.

### Design decisions (proposed — confirm when building)
- 5th button label: **"🤖 Auto"**. Normal draft card → 5 buttons
  (Send / Edit / Regen / Skip / Auto). Once in Auto, the card becomes the
  **Auto card** (countdown + Edit / Send now / Cancel / Take over).
- Delay: random **1–5 min** per reply, configurable; never < 1 min.
- Exit: **Take over** on any Auto card → that conversation back to manual; a
  global kill-switch command stops *all* Auto conversations at once.
- Per-conversation — Auto applies only where the operator pressed the button;
  every other conversation stays manual-approval.

### Effort estimate
The largest of the FRs. n8n: a scheduled/cancellable delayed-send mechanism
(a staticData "pending auto-send" record + a Wait branch + a last-writer-wins
"still current?" check), the countdown rendering, the re-draft-on-new-input
loop, the auto-send branch, the exit. Reuses the paused Hermes autonomous-mode
nodes. **~2–3 days**, built **after FR-1**.

### Status
**Deferred** — fully spec'd 2026-05-21 (safety caps confirmed). Depends on
FR-1; reuses the paused Hermes autonomous-mode + §5.7 cap work.

---

## FR-5 — Background draft improvement + continuous learning

**Requested by the operator (Zayn), 2026-05-21. Refined the same day.**

> "We can do pattern recognition and check all skills to generate the best
> response and learn. Response within 1 min is less important than the best
> response."
> "Don't delay time, keep it fast — but if a message is not immediately sent,
> let it do research to see if the draft is the best possible, or there is
> space for improvement. Keep the learning loop and improve when and where
> needed."

### The model
- **Fast first draft** — generated and shown to the operator immediately, no
  artificial delay. ("Keep it fast.")
- **Background improvement while pending** — a draft then *sits unsent* in the
  approval queue until the operator taps Send (seconds, often minutes). For as
  long as it is pending, a background pass researches whether it is the best
  possible reply and improves it where there is room — updating the card in
  place. If the operator sends instantly, nothing extra runs.
- **Continuous learning loop** — learn from outcomes; grow and improve the
  behaviour-rules / pattern store over time, "when and where needed."

The earlier framing ("add a wait window for deep processing") is **superseded**
— the operator does not want added delay. Improvement happens in the
background, off the critical path. (FR-3's debounce and FR-4's auto-delay still
stand; they serve different purposes — not double-drafting, and looking human
in autonomous mode.)

### What the background pass does
- **Pattern recognition** — read the customer's intent and stage; match against
  what has worked in similar past conversations.
- **Use all available knowledge** — system-prompt knowledge, conversation
  history, learned behaviour rules — to find a better reply.
- **Learn** — capture what works into a growing behaviour-rules / pattern store.

### This is the Hermes "learning brain" — and it now belongs OFF the critical path
The paused Hermes integration was exactly this brain (pattern/trigger
detection, a `behavior_rules` store, conversation-health scoring, a learning
loop). It was rolled back 2026-05-21 because its drafts were slow (20–50s,
sometimes >120s).

**The refined model fixes the root cause of that incident.** The original
integration put Hermes *on the critical path* — every customer reply waited on
Hermes, so when Hermes was slow, everything broke (502s, missed/misrouted
sends). In this model the **fast direct-Claude draft stays the critical path**,
and **Hermes runs as the background improver** on pending drafts. Hermes being
slow no longer matters — it has the whole pending-window to work, and if it is
slow or fails, the fast draft is already there. This is the safe way to revive
Hermes: **additive, not a replacement.**

### Resolved
- **"Check all skills" (operator, 2026-05-21):** "use all our knowledge" — the
  full system prompt, conversation history, learned behaviour rules and Hermes
  memory. No separate skill-module system.
- **"Learn":** persistent cross-conversation learning via the `behavior_rules`
  store, operator-approved before rules take effect (`active=false` until
  approved) — the established Hermes design.

### Status
**✅ Complete (2026-05-21).** All phases deployed: Phase 1 (diagnosis — no
latency bug), Phase 3 (design), Phase 4.1 (bridge `/improve`), 4.2 (n8n
improver branch — 57 nodes), 4.3a (bridge `/learn` + `/rules`), 4.3b (n8n
learning loop — 64 nodes). The background improver and the learning loop are
live. **Pending: real-traffic behavioural testing** of FR-3, the improver, and
the learning loop. See `docs/decisions.md` and `docs/hermes-revival-design.md`.

---

## BUG-1 — Autonomous auto-send unreliable (`Auto Decide` / `staticData`)

**Found 2026-05-22 during H7–H9 behavioural testing.** Full trace in
`docs/h7-h9-test-plan.md`.

Autonomous mode activates, the autonomous branch engages, the countdown card
posts and `Auto Wait` runs — but the auto-send **does not fire**. After the
countdown, `Auto Decide` re-checks the draft with
`pendingQueue.find(id).status === 'pending'`, reading the queue from n8n
**`staticData`**. The draft *is* pending, but the read returns no usable
result, so `Auto Decide` returns `[]` and the branch silently stops before
`Auto Commit` / `Auto Send WAHA`. Zero `autonomous_sends` `kind=auto` rows.

**Root cause:** n8n `staticData` is not consistent across a Wait-node resume
or across concurrent executions — the *same* architectural flaw that made
FR-3's debounce broken-by-design.

**Severity:** autonomous mode cannot be used for real customers. Fails safe —
the draft remains a normal approval card, nothing wrong is auto-sent.

**Fix direction:** the autonomous branch must not depend on `staticData` for
post-wait draft state. Hold draft/queue state in **Redis** (already on the
box) — the same redesign FR-3 needs. Until then, autonomous mode is
effectively non-functional and should stay off.

**Build-ready fix design (2026-05-22):** `docs/bug-1-autonomous-send-fix.md` —
confirmed diagnosis, Redis-backed design, node-level changes, build order,
test plan.

**✅ RESOLVED (2026-05-22).** Built (bridge `/autosend-state` + workflow
Arm/Disarm/Get Autosend, `Auto Decide` rewritten to decide from Redis),
deployed, and **verified by a live test** — execution 646: autonomous
auto-send fired end-to-end, WAHA-confirmed delivery, `autonomous_sends`
`kind=auto` logged. Minor residual — `Mark Auto Sent`'s `staticData`
queue-`status` write — also **fixed (2026-05-22)**: the unreliable write is
removed and the FR-5 improver now skips autonomous drafts via the Redis
autosend key. See BUG-2.

---

## BUG-2 — `/caps` operator command not wired — ✅ RESOLVED (2026-05-22)

**Found 2026-05-22 during the H7–H9 suite.** Sending `/caps` to the operator
bot fell through `Process Text Reply → Route Text Action → Ack No Pending`
(the "no pending draft" fallback) — no cap status was returned.

The bridge `/caps` endpoint worked (a direct POST returns `caps_status_text()`),
but **nothing in the workflow routed the `/caps` command to it** —
`Process Text Reply` had no `/caps` branch.

**Severity:** minor — operator convenience, not a safety path.

**✅ Fixed & verified (2026-05-22).** `scripts/build_bug2_caps_command.py`:
- `Process Text Reply` — new `(F) /caps` branch → `{action:'caps_cmd'}`.
- `Route Text Action` — new switch output #6 `caps_cmd`.
- `Hermes Caps` (new) — `POST http://172.18.0.1:8788/caps` (token-gated,
  `Hermes Bridge` credential) → `Send Caps Reply` (new) relays the bridge's
  `text` to the operator via Telegram `sendMessage`.

Wiring: `Route Text Action [out 6] ─▶ Hermes Caps ─▶ Send Caps Reply`.

**Live test — PASSED.** Execution 656: operator sent `/caps`, the chain ran
end-to-end (`success`), the bridge returned the cap status, and the operator
received the `🧮 Autonomous-mode safety caps` message in Telegram.

### `Mark Auto Sent` `staticData` residual — ✅ RESOLVED (2026-05-22)

`Mark Auto Sent` wrote `d.status='sent'` (+ `d.auto_sent`,
`d.messages_sent_count`) into `pendingQueue` via `$getWorkflowStaticData` —
an unreliable write (BUG-1's concurrent-execution race). A literal "port the
write to Redis" would *orphan* it: `pendingQueue` lives entirely in
`staticData`, and the only real consumer of an auto-sent draft's `status` is
`Check Pending` (the FR-5 improver gate), which reads `staticData`.
(`auto_sent` / `messages_sent_count` had zero readers.)

**Fix** (`scripts/build_mark_auto_sent_residual.py`) — make the FR-5 improver
skip autonomous drafts outright, via the Redis autosend key that
`Arm Autosend` already sets:
- `Check Autosend Key` (new) — `POST /autosend-state {action:get}`, inserted
  `Improve Wait ─▶ Check Autosend Key ─▶ Check Pending`.
- `Check Pending` — `if (armed === true) return []` (fail-open: a bridge
  error → proceed). Runs ~20s after the draft posts — well inside the
  autosend key's 3600s TTL, and long after `Arm Autosend`. No race, no TTL risk.
- `Mark Auto Sent` — the `staticData` block is removed entirely; the node is
  now a pure pass-through to `Edit Auto-Sent Card`.

Deployed 2026-05-22 (97 nodes), structurally verified. A full behavioural
test (an autonomous draft running past the improver) is the one remaining
optional check — the FR-5 improver path itself has never been behaviourally
tested.
