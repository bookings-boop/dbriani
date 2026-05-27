# Hermes Training Extract — Analysis Brief

> Copy this whole document into the other Claude Code session as the prompt.
> The output is a single comprehensive markdown file describing everything
> Hermes (the drafting LLM) needs to know to stop making the same mistakes.

---

## Context (read first)

You are analyzing a corpus of WhatsApp chats from Dubriani Yachts (Dubai
yacht-charter sales). The operator (one human) types feedback into a
Telegram bot that approves/refines draft messages produced by an LLM
agent named **Hermes** (Claude Sonnet). Hermes:

- Drafts every customer reply for operator approval
- Knows a 685-line system prompt (style, fleet catalog, rules)
- Receives the last 100 WhatsApp messages + customer facts (name, dates,
  yachts, party size) + a "Fetch Behavioral Context" block listing
  operator-feedback rules at the bottom of its system prompt

Hermes is failing in three repeated ways:

1. **Memory loss** — forgets prior pricing, prior agreements, prior
   corrections after a few turns
2. **Illogical drafts** — replies that don't fit the conversation state
   (e.g., "let me check the rate" 15h after a price was agreed)
3. **Rule ignorance** — operator types the same correction many times,
   Hermes keeps making the same mistake

Your job: read every chat, distill what Hermes needs to know, and write
ONE markdown file with all of it. The engineer maintaining Hermes will
fold your output into the system prompt + behavior_rules database.

---

## Inputs you have access to

If you have access to the EC2 box (operator will tell you):

1. **WAHA chat list** —
   `docker exec n8n-waha-1 curl -s -H "X-Api-Key: $WAHA_API_KEY" http://localhost:3000/api/default/chats?limit=500`
   Returns every chat with `id`, `name`, `lastMessage.body`, `timestamp`.

2. **WAHA chat messages** (per customer cid) —
   `docker exec n8n-waha-1 curl -s -H "X-Api-Key: $WAHA_API_KEY" "http://localhost:3000/api/default/chats/<cid>/messages?limit=200&downloadMedia=false"`
   Returns each message with `from`, `fromMe`, `body`, `timestamp`,
   `notifyName`. `fromMe=true` means it came FROM Dubriani (either
   from the operator manually, or from Hermes after approval).

3. **Postgres tables** (via `docker exec -i n8n-postgres-1 psql -U n8n -d n8n`):
   - `customer_facts` — extracted facts per customer
   - `customer_label_history` — label transitions (NEW/HOT/WARM/COLD/
     WAITING_FOR_PAYMENT/CONFIRMED/PAUSED_*/DISREGARDED)
   - `customer_notes` — operator-saved per-customer notes
   - `behavior_rules` — operator-saved global/scenario rules
   - `autonomous_sends` — operator and automated outbound log
   - `customer_triggers` — payment intents, booking intents
   - `conversation_state` — last-customer-message-at, last-operator-reply-at

4. **Bridge source code** at `~/hermes-bridge/` — read but don't modify.
   `system-prompt.md` is the current 685-line master prompt.

5. **Telegram bot history** — n8n's `execution_data` table holds
   serialized workflow runs. Don't query this for analysis (too noisy)
   — use it only to confirm specific edge cases.

If you don't have EC2 access, the operator will provide a snapshot file
(JSON dump of WAHA chats + customer_facts CSV).

---

## Output — single file: `hermes-corpus-analysis.md`

Structure that file with the following sections, in order. **Each section
must include concrete chat-evidence with timestamps and customer IDs.**
Vague observations are worthless; specific examples that can be cited in
a rule are what we need.

### Section 1: Operator Persona & Voice

Goal: capture the operator's actual writing voice from their own
(`fromMe=true`) messages that DON'T look like Hermes drafts. Look for
messages that have:
- All-lowercase casual tone, or formal capitalized tone — which?
- Punctuation style (no full stops? em-dashes? ellipses?)
- Emoji frequency (count emojis per 100 messages)
- Common opening phrases ("hey", "hi", "perfect", "no problem")
- Common closing phrases ("let me know", "send when ready", "🌹")
- Phrases the operator NEVER uses (these are the ones Hermes invents)
- Sentence length distribution: median + max + min word counts

Deliverable: a 200–400 word "voice guide" with 10 representative
quotes (verbatim, with customer + timestamp).

### Section 2: Fleet Catalog — Ground Truth

Goal: every yacht ever mentioned in chats, with the canonical and
informal names, plus specs the operator has stated.

For each yacht, extract:
- **Canonical name** (e.g., "Sunseeker Satoshi 70")
- **Informal aliases** ("Satoshi", "Satoshi 70", "the Satoshi")
- **Capacity** (day guests / overnight)
- **Hourly rate** (regular + discounted variants — capture the
  conditions for the discount: "UAE resident", "weekday morning",
  "off-peak", etc.)
- **Day-rate / event-rate** if mentioned
- **Class** (sport, motor, sailing, mega, jetski)
- **Page URL** if ever shared in a chat
- **Operator notes** — anything the operator has said about this
  yacht's quirks ("Élan 44 has fixed price", "Pershing not available",
  "Bliss 55 is the introductory deal")
- **Date last mentioned** in any chat

Sort by frequency-of-mention (most discussed first).

Special: list every yacht name Hermes has INVENTED or mis-spelled
that the operator corrected (e.g., "Pershing 8X" doesn't exist).

### Section 3: Add-On / Service Catalog

For each non-yacht service ever mentioned:
- Item name (e.g., "rose bouquet", "premium BBQ", "Seabob")
- Price + unit (per person, flat, per hour)
- Availability constraint (on which yachts, lead time required)
- Any operator-specified policy ("flowers must be pre-ordered",
  "alcohol must be brought on board, not provided")

Especially: **food packages, drink packages, jet ski rates, water
toys, captain/host services, photographer, decor, route extensions
(World Islands), birthday/proposal/wedding packages.**

### Section 4: Pricing Memory Bank

Goal: every price ever quoted to a specific customer. This is what
Hermes needs to AVOID re-quoting later (the Madawi-class bug).

Format as a table or list:
- Customer name (or cid)
- Date quoted
- Yacht + duration + party size
- Price quoted (AED)
- Outcome (paid / declined / silent / pending)
- Operator notes about pricing rationale ("gave 30% discount because
  repeat customer", "matched competitor", "introductory rate")

Highlight ANY case where Hermes later re-quoted a DIFFERENT price to
the same customer — these are training failures.

### Section 5: Booking Workflow — Canonical Sequence

Walk through 5–10 BOOKED chats end-to-end (ones that ended in
`status=CONFIRMED`). Extract the canonical flow:

1. Lead arrives (channel, opening message pattern)
2. Greeting (operator's voice + tone in the first reply)
3. Information-gathering questions (in what order? guest count first?
   date first? yacht preference?)
4. Quote presentation (format: bullets, single line, structured?)
5. Negotiation (any discount discussion, add-ons, route changes)
6. Payment link sent (when in the flow, what triggers it)
7. Payment received → confirmation message format
8. Pre-trip messages (location, parking, host introduction)
9. Day-of-trip touchpoints (any?)
10. Post-trip (review request? thank-you?)

For each step, give:
- The "ideal" message format/wording (cite a real example)
- Common variations
- Anti-patterns (what the operator has corrected Hermes on)

### Section 6: Customer-Segment Patterns

Identify segments and their distinct conversational signatures:

- **Form-lead** (came via website "Hi Dubriani, I'd like to check
  availability. Date: XXX Guests: X-X Occasion: XXX Page: / Ref: XXX") —
  what's the typical follow-up? When do they convert?
- **Repeat customer** — operator's known name; what tone shift is
  appropriate (more casual? less greeting?)
- **B2B / agency** ("we own a yacht", "partnership", "fleet
  collaboration") — operator says these get handled offline by Zayn;
  what should Hermes say?
- **Pricing-shopper** (lots of "how much" questions, no commitment) —
  signals + ideal handling
- **High-intent same-day** ("today", "tomorrow", "this evening") —
  signals + urgency in reply
- **Birthday / proposal / wedding** — special handling, decor,
  surprise-keeping rules
- **Group / corporate** (10+ guests, multi-yacht needed)
- **Influencer / press** — different terms?

For each, capture 2–3 quoted exchanges as exemplars.

### Section 7: Failure Case Studies

Look for chats where the operator EDITED a draft via Telegram refine
(you can find these by chats where a `fromMe` message arrives shortly
after a customer message, often within 2 min, and the message is
short / corrective).

For each failure:
- **Customer + timestamp**
- **What Hermes drafted** (if recoverable from the chat — look for
  patterns where Hermes' draft is in the operator's editing memory)
- **What the operator sent instead**
- **Diagnose the gap** (Hermes invented info / forgot prior message /
  wrong tone / wrong format / etc.)
- **Suggested rule** to prevent this

Aim for **20+ failure cases** across different failure types.

### Section 8: Success Case Studies — Exemplary Drafts

Mirror Section 7 but for cases where the operator approved a Hermes
draft as-is (no edit). Find 10 such cases and use them as
**positive exemplars** Hermes can be prompted with as few-shot examples.

For each, just quote the message + give the conversation context
in one paragraph. Bonus: tag with WHY it worked (right tone, right
length, right info density).

### Section 9: FAQ Bank

Every customer question pattern that recurs across 3+ chats. For each:

- **Question pattern** (e.g., "do you do sunset cruises?", "is the
  yacht private?", "what's included in the price?")
- **Canonical answer** (the operator's actual wording, condensed)
- **Customer follow-up patterns** (what they usually ask next)
- **Common misunderstandings** to address proactively

Aim for **30+ FAQs**.

### Section 10: Refusals & Difficult Conversations

Patterns where the operator says NO. Capture the exact tone:

- Refunds requested
- Price haggling beyond comfortable range
- Off-platform payment
- "Cash on arrival" requests
- Refusing to confirm without deposit
- Refusing impossible dates (yacht booked, weather, permitting)
- Refusing inappropriate requests (parties on small yachts, etc.)
- Gracefully redirecting B2B inquiries

For each, give 2 exemplars + a one-line rule.

### Section 11: Conversation State Taxonomy

For each label (`NEW`, `HOT`, `WARM`, `COLD`, `WAITING_FOR_PAYMENT`,
`CONFIRMED`, `PAUSED_SPAM`, `PAUSED_B2B`, `PAUSED_PERSONAL`,
`DISREGARDED`, `NEEDS_ATTENTION`):

- What signals push a customer INTO this state (from chat content)?
- What's the appropriate DRAFTING POSTURE in this state?
- What are the exit signals (when do they leave this state)?
- What rule must Hermes NEVER violate in this state?

### Section 12: Edge Cases & Special Scenarios

- **Multi-day bookings** — how to present (24h vs "2-day"), pricing model
- **Late-night / overnight** — additional crew, alcohol policy, etc.
- **Multi-yacht events** — operator handling
- **Group splits** (one person paying for several) — Nomod payer
  mismatch (production bug — Musawi paid for Qurbani's link)
- **Cancellations / reschedules** — what's the policy, refund window?
- **Weather cancellations** — operator's voice in these messages
- **Government / VIP yachts** — anything to never say?
- **Forwarded payment links** — how to handle "a friend will pay"
- **Multi-language customers** — does the operator switch languages?

### Section 13: Recurring Operator Phrases (Voice Anchors)

Top 50 multi-word phrases the operator uses (≥3 occurrences in their
own messages). These are voice-anchors for Hermes to mimic. Include
frequency count.

Also: top 30 phrases Hermes uses that the operator DOESN'T (these
are the ones to forbid). Look for things like "totally understand",
"sweet!", "absolutely!", "of course", "enjoy life", "sort that out".

### Section 14: Suggested System Prompt Additions

Concretely, based on everything above, write proposed additions to
the master system prompt. For each, give:

- **Section header** (e.g., "## Pricing memory protocol")
- **Verbatim text** to add
- **Why it's needed** (link back to evidence in earlier sections)

Aim for 5–10 substantial additions, each 50–200 words.

### Section 15: Suggested behavior_rules to seed

A list of 30–50 specific rules that, if loaded into the
`behavior_rules` table as active globals, would close the most
common failure modes. Each rule:

- **Rule text** (single sentence, actionable, specific)
- **Evidence**: cite the failure case from Section 7
- **Scope**: global / scenario:<name> / customer:<cid>

These will be SQL-inserted, so write them as plain English not JSON.

### Section 16: Open Questions for the Operator

Things you can't determine from the chats alone — e.g.:
- "When the customer asks for 'a quiet boat', which yachts qualify?"
- "Is the AED 450 bouquet price firm or negotiable?"
- "What's the policy on photographers — included, paid extra, BYO?"
- "Do you have a no-show policy?"

Each question should be answerable in 1–2 sentences by the operator;
the answer will be folded into the system prompt later.

---

## Quality criteria

A great extract:
- **Cites evidence**: every claim is backed by a specific chat
  (customer name + timestamp + verbatim quote)
- **Distinguishes operator voice vs Hermes voice** — separates the two
  even when both messages are `fromMe=true`
- **Avoids generic LLM platitudes** ("be respectful and professional"
  is worthless; "operator never opens with 'Hello', always uses 'hi'
  lowercase" is gold)
- **Quantifies frequencies** when possible ("12 out of 18 booking
  confirmations followed this exact 3-line format")
- **Surfaces contradictions** — places where the operator has been
  inconsistent (different prices to similar customers) for human
  resolution

A bad extract:
- Vague generalities ("be friendly")
- Single examples treated as patterns
- Inventing rules not grounded in the actual chats
- Confusing operator-generated content with Hermes-generated content

---

## Workflow suggestion (how to actually do this)

1. **Hour 1 — corpus survey**: fetch the chat list, count chats,
   identify the top 50 most-active customers, the 20 most-recent
   leads, and 20 random older chats. Total ~90 chats.

2. **Hour 2 — chat reading**: read each chat end-to-end. Take notes
   into a scratch file: every operator-sent message, every visible
   refine pattern, every quoted price, every yacht mention. Don't
   try to fit into sections yet.

3. **Hour 3 — DB joins**: pull `customer_facts`, `behavior_rules`,
   `customer_notes`, `autonomous_sends`, `customer_label_history`
   for these customers. Cross-reference with chats.

4. **Hour 4 — synthesis**: organize notes into Sections 1–16. Cut
   anything not backed by at least one citation. Prefer brevity over
   exhaustiveness in low-signal sections.

5. **Hour 5 — sharpen**: re-read your draft. For every rule you
   propose, ask "could Hermes follow this with zero ambiguity?" If
   not, rewrite to be more concrete.

Save the final result as `hermes-corpus-analysis.md` and share with
the engineer (this conversation's user). Don't push to git — the
engineer will integrate selectively.

---

## What NOT to do

- Do not send any WhatsApp messages
- Do not modify any database table
- Do not modify `system-prompt.md` directly — write proposed additions
  to Section 14 of your output
- Do not write code into the bridge — that's the engineer's job
- Do not delete or rotate any credentials
- Do not include personally identifying customer info (phone numbers,
  emails, full names) beyond what's necessary as evidence — strip or
  mask in your output where reasonable
- If you find any leaked credentials in the corpus, flag them in a
  separate "SECURITY ISSUES" appendix and stop investigating that chat

---

## Done criteria

You're done when `hermes-corpus-analysis.md` has all 16 sections, every
section has evidence-backed observations, and the operator can read it
in 30 minutes and feel "yes, this captures what Hermes needs to know".

Length target: 8,000–15,000 words. Less than 5,000 = under-researched.
More than 20,000 = padding or weak evidence-filtering.
