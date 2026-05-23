# Hermes Current Operating-State Inventory

> **Audit date:** 2026-05-23
> **Mode:** READ-ONLY. No code/file changes, no deploys. Inventory only.
> **Purpose:** baseline for the Dubriani Cowork docs integration design.

---

## TL;DR

There are **TWO independent LLM-call paths**, each loading a different
system-prompt copy:

| Path | What runs the LLM call | System prompt source | Used by |
|---|---|---|---|
| **(A) Customer-message hot path** | n8n workflow's `Claude AI` httpRequest → Anthropic API directly | **Embedded** copy inside the workflow's `Build Prompt` Set node | Every inbound customer reply, refine, regen, `/lead` outbound |
| **(B) Hermes CLI path** | `~/.local/bin/hermes` invoked from the bridge | **File** at `~/hermes-bridge/system-prompt.md` | `/draft-followup` ([Draft nudge] button), `classify_feedback`, `extract_customer_facts` |

These two copies have **drifted** — they're 14 lines different (see §1.3
below). The system prompt that `deploy_bridge.py` updates is the (B) copy.
Customer-facing drafts are produced by (A), which is updated only by
workflow build scripts (`build_prompt_calibration.py` etc).

There's a **third surface** — `~/.hermes/skills/` on the box — containing
**17 Dubriani-prefixed markdown files** that the bridge does NOT load
into (B). Whether Hermes CLI auto-loads them depends on its config
(`~/.hermes/config.yaml` shows `skills.channel_prompts: {}` — empty per
the partial inspection we could do).

DB-driven rules layer is **thin**: 1 active global behavior_rule, 0
active customer_notes, 0 customer_triggers.

---

## 1. System prompt

### 1.1 Active master file

- **Path on box:** `/home/ubuntu/hermes-bridge/system-prompt.md`
- **Path in repo:** none — file is not committed (gitignored or untracked locally).
- **Size:** 584 lines · 33,850 bytes
- **Last modified:** 2026-05-23 12:14 (today, by `deploy_bridge.py`)
- **Constant in code:** `SYSTEM_PROMPT_PATH = os.path.join(BRIDGE_DIR, "system-prompt.md")`
  at `hermes-bridge/server.py:37`
- **Loaded by:** `load_system_prompt()` at `server.py:861`. Used by
  `build_query()` (bridge `/draft`) and `build_improve_query()` (bridge
  `/improve`).

### 1.2 Section headers (all 22 top-level + nested)

```
1   # Dubriani Yachts — WhatsApp Agent System Prompt (v2, draft-approval mode)
3   ## 0. YOUR ROLE — READ FIRST
9   ## OUTPUT FORMAT (STRICT)
28  ## 0.5 CONVERSATION CONTEXT — READ THIS BEFORE DRAFTING
39  ## 1. The Verified Rules (in priority order — based on 14,804 chats incl. 998 named VIPs)
41    ### THE TWO META-RULES THAT OVERRIDE EVERYTHING
56    ### VIP fast-track (the highest-leverage move)
74    ### Loss-reason recovery rules
99    ### Pricing rules
107   ### Payment rules
115   ### Repeat-customer outreach (gold mine)
124   ### B2B partner pricing (~50% off retail)
134   ### Multi-day proposals (Bahamas, week-long charters)
143 ## 2. Hard Rules (single consolidated list)
159   ### Length rules
165   ### Time-of-day rule
170 ## 3. Identity
183   ### Brand differentiators (lean into these)
193 ## 4. Voice & Tone
203 ## 5. Mandatory Qualification (collect these 4 — friendly, not interrogative)
220 ## 6. The Recommendation Rule
237   ### Live time-limited offers (only these 3 — everything else is baseline pricing)
248 ## 7. Yacht Catalog (use only these — flag in `notes_for_zayn` if unsure)
250   ### 7.1 Sunseeker Satoshi 70 ft — Dubriani-owned, prioritize
276   ### 7.2 Essential tier (budget — under AED 2,800/hr)
294   ### 7.3 Premium tier (mid AED 2,700–7,000/hr — Satoshi range and up)
318   ### 7.4 VIP tier (luxury AED 7,500+/hr — fine dining only)
344   ### 7.5 Doha Operations
347   ### 7.6 Retired / on-request only
361 ## 8. Catering — match menu to client tier
363   ### Pricing rule of thumb
368   ### Menu options
387   ### Beverages (Dubriani in-house, AED, VAT incl.)
397   ### Bar / corkage
403   ### Roberto's Beverage Packages
412 ## 9. Shisha
417 ## 10. Special Occasions
419   ### 🎉 Birthdays
429   ### 🌹 Romantic Dinners
435   ### 💍 Proposals
452 ## 11. Multi-day Itineraries
462 ## 12. Chauffeur / Pickup & Drop
471 ## 13. Onboard Hospitality (use to upsell + reassure)
481 ## 14. Negotiation Playbook
483   ### Price objections — never collapse, lead with value
488   ### Use "no"-questions to surface objections (Chris Voss style)
494   ### Trust signals when they hesitate
499   ### Language labels
504   ### Sequence
511 ## 15. Closing & Confirmation Flow
524 ## 16. Hard Stops — flag in `notes_for_zayn` and write a holding reply
542 ## 17. What You DON'T Do
560 ## 18. The Dubriani Mindset
570 ## 19. Payment Link Signal
```

### 1.3 Section summaries + quoted phrases

**§0 YOUR ROLE** (lines 3–7) — defines Maria persona, draft-approval
mode. Quote: *"You are drafting WhatsApp replies for Zayn's review and
approval. Your output is a DRAFT — it is NEVER sent automatically to
the customer."*

**OUTPUT FORMAT** (9–27) — strict JSON: `{"messages": [...],
"notes_for_zayn": "30 words max"}`. Anti-fabrication rule: *"Never
invent prices, availability, yacht specs, policies, or add-ons."*

**§0.5 CONVERSATION CONTEXT** (28–37) — describes the
`conversation_history` block. Quote: *"Every request includes a
`conversation_history` block: the recent back-and-forth in this
WhatsApp chat, oldest first."*

**§1 META-RULES** (41–55) — two override-everything rules:
- *META-RULE A:* "Write like a real Dubai sales agent on WhatsApp.
  Mostly lowercase, light contractions, no exclamation marks beyond 1
  per message, no em-dashes."
- *META-RULE B:* "Mirror cultural micro-signals" — Russian/Ukrainian +7
  +380, Indian English idioms, Arabic-English code-switching.

**§1.VIP fast-track** (56–73) — VIP detected on named-customer signal,
yacht name in first 1-3 msgs, payment-link in first 5 msgs, tight
timeline. *"The named-VIP win rate is 4.7% (vs 0.51% average) — a 9.3×
lift."*

**§1.Loss-reason recovery** (74–98) — cooling-off ladders:
- *"At <1 hour: re-engage with a smaller yacht in their tier."*
- *"At 1-24 hours: don't re-engage — let them come back to you."*
- *"At 1-7 days: gentle pulse, 1 message max."*
- *"At 7+ days: dead, do not message."*

**§1.Pricing rules** (99–106) — never drop the rate; pivot to a
smaller yacht. Quoted: *"Sub-AED 2K → Élan 44, Cante 39, Cabo 35"* and
*"Free extras instead of discounts: complimentary jetski / chef /
photographer."*

**§1.Payment rules** (107–114) — *"Default ask: 100% payment upfront."*
80% paid full prior to charter. 88% paid via channels OTHER than Nomod
card link (cash, USDT, bank, 50/50).

**§1.Repeat-customer outreach** (115–123) — *"NYE, F1, summer
launches, birthdays known from previous bookings."*

**§1.B2B partner pricing** (124–133) — ~50% off retail. *"When customer
represents an agency / hotel / event planner, request license/business
proof before quoting."*

**§1.Multi-day proposals** (134–142) — Bahamas, week-long charters.

**§2 Hard Rules** (143–169) — single consolidated numbered list of 12
do/don't rules including:
- *"2. Sign as Maria. Warm, energetic, conversational. Write what
  Maria would type."*
- *"3. Don't volunteer that you're an AI. If a customer directly and
  persistently asks, you may acknowledge with grace."*
- *"4. Default to ONE message"*
- *"5. Lowercase opener has +3.7pp lift over baseline."*

**§3 Identity** (170–192) — brand: "Dubai-based luxury yacht charters
+ fine dining at sea". Differentiators: own Sunseeker Satoshi 70 ft;
in-house chef brand (Roberto's); proprietary fleet.

**§4 Voice & Tone** (193–202) — warm Dubai-Arabic-English mix. "Mostly
lowercase. Light Arabic / Russian phonetic micro-cues where the
customer's language calls for it."

**§5 Mandatory Qualification** (203–219) — 4 fields to collect: Date,
Duration, Pax, Occasion. Written casually: *"woven into a normal
reply, never as a standalone question."* The 11-field structured form
is explicitly forbidden.

**§6 The Recommendation Rule** (220–247) — *"Always send 3 yacht
options: 1 premium, 1 standard, 1 budget."* Plus the **media-share
rules** at lines 228–232: PDF brochure "operator-sent" (the model
cannot attach files), yacht video link share, Google Maps share.

**§6.Live time-limited offers** (237–247) — only 3 offers in the
allowed-to-mention list (Romantic Dinner / Birthday / Proposal).

**§7 Yacht Catalog** (248–360) — 6 sub-sections:
- §7.1 Sunseeker Satoshi 70 ft (250–275): owned, prioritize. Pricing
  AED 1,500/hr morning floor (B2C). Holds 25 pax. Hard-coded specs.
- §7.2 Essential tier (276–293): Élan 44, Cante 39, Cabo 35, Beneteau
  Trawler, Carina, Belle. AED 700–2,500/hr.
- §7.3 Premium tier (294–317): Pershing 5X, Princess, Riva, Galeon,
  Maiora, Satoshi range. AED 2,700–7,000/hr.
- §7.4 VIP tier (318–343): Pershing 82, Azimut 88, Sunseeker 88,
  Sanlorenzo SX88, Benetti, Baglietto, Thunder. AED 7,500+/hr. Fine
  dining only.
- §7.5 Doha (344–346): pricing not available — flag.
- §7.6 Retired (347–360): Diana 50, Élan 44 *flagged retired here but
  Élan 44 also appears in §7.2 as live* — contradiction noted §4 below.

**§8 Catering** (361–411) — pricing rule of thumb (~AED 250-400 pp).
Menu options: Premium BBQ, Fine Dining, Russian Fine Dining (Chef
Artem). Roberto's hot+cold packages. Beverage packages: Rome AED 125,
Milano AED 195, Roma AED 295.

**§9 Shisha** (412–416) — operator-supplied.

**§10 Special Occasions** (417–451):
- Birthdays (419–428): cake AED 250-400, decor AED 800-1,200.
- Romantic Dinners (429–434): *"Push only 2 menus: Premium BBQ (AED
  2,500 for 2 incl. chef) or Fine Dining (AED 2,500 for 2 incl. chef)."*
- Proposals (435–451): photo+video AED 2,500 (4 hrs), 100 Roses AED
  799, heart of petals AED 499.

**§11 Multi-day Itineraries** (452–461) — Bahamas, Greek islands.

**§12 Chauffeur / Pickup** (462–470) — Mercedes V-Class.

**§13 Onboard Hospitality** (471–480) — "use to upsell + reassure".

**§14 Negotiation Playbook** (481–510):
- Price objections: never collapse. Lead with value.
- Chris Voss "no"-questions: *"Is now the wrong time?"* etc.
- Trust signals: Google Maps, GMB reviews, brochure (operator-sent).
- Language labels: Russian/Ukrainian/Indian English/Arabic.
- 5-step sequence: surface → label → trade → silence → trial close.

**§15 Closing & Confirmation Flow** (511–523) — checklist for closing:
yacht confirmed, date+time, duration, pax count, full pricing
itemized, payment method, pickup address.

**§16 Hard Stops** (524–541) — flag in `notes_for_zayn` and write a
holding reply: pricing outside catalog, retired yacht, weather concerns,
multi-day, B2B without license, family minors policy, alcohol policy,
catering allergies.

**§17 What You DON'T Do** (542–559) — never quote prices below catalog
floor, never auto-confirm without operator review, etc.

**§18 The Dubriani Mindset** (560–569) — *"Every customer is a future
referral. Every chat is a first-impression."*

**§19 Payment Link Signal** (570–584) — instructs the model to set
`should_send_payment: true` only when yacht + date + duration + price
ALL confirmed AND the customer has clearly said they want to book.
*"Never paste or invent a URL in your reply — not `pay.nomodapp.com`,
not `[link]`, no markdown link, nothing."* Closing footer: *"v2 —
2026-05-19 — draft-approval mode, consolidated."*

### Customer-pattern recognition rules in the system prompt

- **VIP signal** (§1.VIP, 56–73): named yacht + tight timeline + 1-3
  msgs in.
- **Cultural micro-signals** (§1 META-RULE B): Russian (+7, Cyrillic),
  Ukrainian (+380), Indian English, Arabic code-switching.
- **B2B detection** (§1.B2B): customer represents an agency/hotel/event
  planner → request license/business proof.
- **Loss-reason staging** (§1.Loss): <1h / 1-24h / 1-7d / 7+d ladders.
- **Russian/Ukrainian flag for Chef Artem** (§8): trigger Russian Fine
  Dining offer.

### Pricing rules surfaced in the prompt

- Hard floor: Satoshi B2C AED 1,500/hr morning, AED 3,000/hr afternoon.
- Tier ceilings: Essential <AED 2,800/hr; Premium AED 2,700-7,000/hr;
  VIP AED 7,500+/hr.
- B2B: ~50% off retail (needs license proof).
- Live offers: Romantic Dinner (AED 2,500 for 2 incl. chef), Birthday
  (cake+decor combo), Proposal (Photo+video AED 2,500).
- Add-ons (§10.Proposals): 100 Roses AED 799, Heart-of-petals AED 499.

### Objection-handling templates

- 5-step sequence (§14.Sequence): **surface → label → trade → silence
  → trial close**.
- Chris Voss style "no"-questions: *"Is now the wrong time?"*, *"Is
  the price the only thing stopping you?"*
- Price-objection pivot script (§1.Pricing): *"if customer says price
  is too high — NEVER drop the rate. Pivot to a smaller yacht in their
  tier."*

---

## 2. Build Prompt embedded copy + drift

**Location:** `workflows/phase-1b-telegram.json`, Set node `Build Prompt`,
parameter `assignments.systemPrompt` (34,508 bytes, 598 lines).

**Used by:** the Anthropic `Claude AI` httpRequest node downstream
(every customer-message draft, refine, regen, `/lead`).

### 2.1 Drift versus the master file

| | Master `system-prompt.md` | Build Prompt embedded |
|---|---|---|
| Lines | 584 | **598** (+14) |
| Bytes | 33,850 | **34,508** (+658) |
| JSON schema | `{messages, notes_for_zayn}` | **`{messages, notes_for_zayn, break_condition}`** |
| Has `## BREAK-CONDITION CHECK` section | ❌ no | ✅ yes (lines 574–588) |
| `## 19. Payment Link Signal` heading | "Payment Link Signal" (Title Case) | "Payment link signal" (lowercase 'l') |
| Closing footer line | line 584 of master | line 588 of embedded |

The break-condition block, present only in the workflow embedded copy:

```
## BREAK-CONDITION CHECK

Also include a "break_condition" field in your JSON. Judge ONLY the customer's
newest message. Set "hit": true only if it clearly matches one of:
  - "discount_request": asks for a discount or a lower price, says "best
    price", says it is too expensive, or is haggling on price.
  - "human_request": asks to speak to a person / human / manager / "real
    person", or to be transferred off the bot.
  - "negative_sentiment": the customer is clearly upset, angry, frustrated, or
    complaining — NOT mild hesitation or an ordinary sales objection.
A normal price question ("how much is the 80ft on Saturday?") is NOT a break.
```

This was added by `build_break_detection_prompt.py` (FR-4 autonomous
mode). The corresponding update was **never back-ported** into
`system-prompt.md`. Consequently:

- The Claude API path enforces break detection on every draft.
- The Hermes `/draft-followup` path does NOT enforce break detection —
  its JSON schema is `{messages, notes_for_zayn}` without
  `break_condition`. (Probably not blocking anything in practice
  because draft-followup is not the customer-message hot path.)

---

## 3. Hermes skills directory

**Path:** `/home/ubuntu/.hermes/skills/`
**Contents:** 17 Dubriani-prefixed `.md` files + 26 unrelated subdirs
(apple/, github/, productivity/, etc.) inherited from the Hermes
default install.

**Backup directory:** `~/.hermes/skills.bak-2026-05-21/` (full backup
of the same content, dated May 21).

### 3.1 The 17 Dubriani / charter operating files

All last modified **2026-05-21 11:10** (2 days before this audit):

| File | Size | First-line summary (literal, from file) |
|---|---|---|
| `Dubriani Ad Scheduling - Country × Time.md` | 11.7 KB | "## How to read this — For each country we tracked:" |
| `Dubriani Conversion Rules — What Works & What Doesn't.md` | 14 KB | "## TL;DR — the 8 rules that survive verification" |
| `Dubriani Country Quick-Reference Card.md` | 6.4 KB | "## 🇦🇪 UAE  N=6,073 · 0.92% chat win · 8 msgs · 66 chars · **Style:** Casual, often local Arabic-English mix" |
| `Dubriani Customer Pattern Recognition.md` | 24 KB | "## Quick recognition cheat (under 60 seconds)" |
| `Dubriani Human Communication Rules.md` | 18 KB | "## 🚨 RULE #0: NEVER COPY-PASTE. TYPE EVERY MESSAGE LETTER BY LETTER" |
| `Dubriani Master Agent Skill.md` | 60 KB | "# PART I — THE MINDSET (READ DAILY) — You are not a salesperson. You are a host." |
| `Dubriani Name & Demographic Recognition.md` | 24 KB | "## 🎯 The 'Displaced Customer' Rule (most important insight) — 33% of named VIP customers (332 of 991) have a name origin that doesn't match their phone country." |
| `Dubriani Per-Country Message Templates.md` | 10 KB | "## 🇮🇳 INDIAN CUSTOMER (haggle expected — perk in pocket)" |
| `Dubriani Per-Yacht Conversion Analysis.md` | 6.4 KB | "## Top performers by win rate (yachts that close)" |
| `Dubriani VIP Re-engagement Strategy.md` | 11 KB | "## What I extracted from 998 named-VIP chats" |
| `Dubriani WhatsApp - Example Library.md` | 14 KB | "## 1. Length rule (the most important rule) — The team's median agent reply is 57 characters." |
| `Dubriani WhatsApp Agent - System Prompt.md` | **36 KB** | "## 0. The Verified Rules (in priority order — based on 14,804 chats incl. 998 named VIPs)" |
| `Dubriani WhatsApp Cheat Sheet.md` | 10 KB | "## ⏱ THE 15-MINUTE RULE — Reply within 15 min during 9 AM – 11 PM Dubai. Always." |
| `Dubriani Winning Phrases Library.md` | 9.8 KB | "## OPENERS — ### Variant A — Maria short [12×]" |
| `HNW Conversion Playbook.md` | 13 KB | "## What I now have access to — For your originally-listed 23 customers:" |
| `Loss Reason Analysis & Recovery Playbook.md` | 10 KB | "## Headline numbers (CORRECTED from full dataset)" |
| `RULES — Maria charter handling.md` | 57 KB | "## 0. FOUNDATION — what to load at session start" |

### 3.2 YAML frontmatter

**None of the 17 files have YAML frontmatter** (no `description:`,
`trigger:`, or `tags:` blocks). They start directly with markdown
content. This means Hermes' frontmatter-based skill-loading mechanism
(if one exists) has no description to gate activation on — the skills
either all load or none do.

### 3.3 Is the bridge actually loading these?

**No** — the bridge's `build_query()` (`server.py:872`) only loads the
file at `SYSTEM_PROMPT_PATH` (= `~/hermes-bridge/system-prompt.md`).
It does NOT read from `~/.hermes/skills/`.

**Maybe via Hermes CLI** — the bridge invokes
`~/.local/bin/hermes --profile default chat -q <query> -Q --source tool --yolo -t memory`
at `server.py:921`. Hermes itself may auto-load skills from its
`~/.hermes/skills/` directory when invoked with `--profile default`.
The partial inspection of `~/.hermes/config.yaml` showed a
`skills.channel_prompts: {}` mapping (empty) — suggesting skills are
NOT actively channeled into the default profile, but a full
confirmation requires inspecting the binary's behaviour (not done in
this audit).

**Functionally:** even if Hermes auto-loads them, they would only
affect the **Hermes CLI path (B)** — not the customer-message hot path
(A). Path (A) only goes through Anthropic via the workflow.

### 3.4 The redundant 36-KB Dubriani WhatsApp Agent - System Prompt.md

This file in the skills dir is roughly the same size as the bridge's
own `system-prompt.md` (33.8 KB). They appear to be **parallel
versions** of the same prompt — but the timestamps differ (skills file
May 21; bridge file May 23). The bridge file is newer and includes
post-May-21 edits (e.g., the lowercase-opener calibration, payment
anti-URL clause). The skills-dir copy is **stale** relative to the
bridge file.

---

## 4. Postgres tables that feed draft generation

### 4.1 `behavior_rules`

```
 id | scope    | scope_value | rule_text                                                                 | active | created_via   | reasoning
----+----------+-------------+---------------------------------------------------------------------------+--------+---------------+----------
  5 | _discarded |           | Always address Dubriani Yachts customers as Mr or Ms ...                  | false  | edit_feedback | Operator explicitly said 'always' and framed it as a policy ...
  8 | global   |             | Never offer phone calls in any draft or communication.                    | true   | feedback      | (none)
  9 | global   |             | Sunset slots can be booked for a minimum of 2 hours, not 3. Do not state. | false  | edit_feedback | The operator corrected a factual policy claim ...
```

**Active count: 1.** Only id=8 ("Never offer phone calls") is feeding
into drafts via `fetch_behavior_rules()` and `behavioral_context()`.

### 4.2 `customer_notes`

**Active count: 0.** No customer-specific notes are currently shaping
drafts. The table exists and is wired (see Pipeline Review +
`/feedback`), but operators haven't yet captured any notes.

### 4.3 `customer_facts` (top 10 by `updated_at`)

```
customer_id          | name | yachts            | dates           | party       | msgs | label
---------------------+------+-------------------+-----------------+-------------+------+------
274942918680787@lid  | Mark | Satoshi 70        | this Saturday   | 2 guests    |  26  | WARM
28063417008351@lid   |      |                   |                 |             |   1  | NEW
183438170693737@lid  |      | Élan 44, Diana 50 | Sat May 23      | 6-7 guests  |   8  | NEW
274319577976890@lid  |      |                   |                 |             |   1  | NEW
156251681997028@lid  |      |                   |                 |             |   1  | NEW
135953029026042@lid  |      | Élan 44           |                 |             |   1  | NEW
0@c.us               |      |                   |                 |             |   1  | NEW
196568070275292@lid  |      | Satoshi 70ft      | Thu Jan 28 2027 | 2 guests    |   7  | NEW
```

**Fields populated by `extract_customer_facts()`:** `name`, `yachts`,
`dates`, `party_size`. **Always populated:** `customer_id`, `label`,
`message_count`, `created_at`, `updated_at`. **Pipeline-Review-added
columns** (all populated by the new flow): `label_updated_at`,
`label_locked_until`, `label_locked_reason`.

Used by `Customer Facts` httpRequest in the workflow → injected into
`Build Prompt` via the `customer_header` field. The header text appears
above every approval-mode draft card to the operator (NOT inside the
prompt to the LLM — it's purely operator-facing).

### 4.4 `customer_triggers`

**Row count: 0.** No detected payment-promised / callback-promised /
booking_intent triggers in the table. The bridge's `save_trigger()`
writes to this on `detected_trigger` JSON from Claude, but apparently
no trigger has been detected recently — or the trigger detection prompt
in §1 above isn't firing matches.

### 4.5 `conversation_modes` (latest per customer)

```
customer_id          | mode       | activated_by      | activated_at
---------------------+------------+-------------------+-------------
157208872501273@lid  | approval   | manual_killswitch | 2026-05-22 21:37
274942918680787@lid  | autonomous | operator          | 2026-05-23 12:33
5532505120995@lid    | approval   | manual_killswitch | 2026-05-22 21:37
971509767187@c.us    | approval   | manual_killswitch | 2026-05-22 21:37
fr4-test@c.us        | approval   | manual_killswitch | 2026-05-22 21:37
```

**5 distinct customers, 47 total rows** (audit-log style). Only Mark is
currently in autonomous mode; everyone else got reset to approval by
the manual killswitch on May 22 21:37. Hot path reads via
`get_mode()` (bridge) and uses it to decide whether to fire
autonomous-send after the operator wait window.

### 4.6 Other tables in the Hermes data layer

| Table | Rows used | Purpose |
|---|---|---|
| `customer_label_history` | append-only | Pipeline Review audit — every label transition. |
| `conversation_state` | 1 row per customer | Pipeline Review timing (last_customer_message_at, last_operator_reply_at, last_analyzed_at, etc.). |
| `label_corrections` | append-only | Self-improvement dampening source — every operator `/label` override. |
| `autonomous_sends` | append-only | FR-4 autonomous-mode audit (kind: auto/checkpoint/intervention). |
| `conversation_health` | append-only | The `health` field from each draft is persisted here. |

---

## 5. Hardcoded references in bridge code (`hermes-bridge/server.py`)

### 5.1 Yacht names

- `server.py:85` — `YACHT_NAMES` tuple (46 entries), used by the
  facts-extract gate `_facts_extract_gate()`:

  ```python
  YACHT_NAMES = (
      "satoshi", "enigma", "aurora", "azimut", "sunseeker", "ferretti",
      "pershing", "benetti", "beneteau", "galeon", "riva", "princess",
      "lamborghini", "sanlorenzo", "maiora", "baglietto", "elan", "elise",
      "diana", "zenith", "bliss", "von dutch", "cabo", "belle", "monaco",
      "cante", "carina", "haigan", "zirve", "luna", "notorious", "asya",
      "zeta", "dolce vita", "tatti", "sapphire", "odysea", "royalty",
      "mila", "athena", "skyfall", "finesse", "sofiya", "eclipse",
      "royal mirage", "yacht",
  )
  ```

- `server.py:1093` — `YACHT_KEYWORD_RE` regex (Pipeline Review):
  ```python
  r"\b(yacht|boat|satoshi|pershing|sunseeker|thunder|catamaran|cruise|charter)\b"
  ```
  Much smaller subset of yacht names. Drift between this and
  `YACHT_NAMES` (e.g., `thunder` is here but not in `YACHT_NAMES`;
  `ferretti`, `galeon`, etc. in `YACHT_NAMES` but not here).

### 5.2 Prices

- `server.py:265` — Nomod currency hardcoded `"AED"` (never taken from
  LLM, by design).
- `server.py:1075` — `MONEY_RE` includes literal `\bAED\b|aed\b`.

**No hardcoded yacht prices in server.py** — all pricing lives inside
the system prompt content (which the LLM may quote into drafts).

### 5.3 Message templates / customer-facing scripts

**None** — all customer-facing language is generated by the LLM. The
bridge contains a few **operator-facing** templates:

- `_payment_link()` builds the booking summary line:
  *"Dubriani Yachts — {customer_name}"*, *"Yacht charter"* default
  summary (`server.py:261-263`).
- `sameday_interrupt_check()` returns the alert text:
  *"⚡ SAME-DAY ASK — {name} · {dates} · we haven't drafted yet"*
  (`server.py:1240`).
- `_humanize_signal()` operator-facing English translations of raw
  signal names (e.g., `money_mentioned` → "Mentioned money/budget").
- `_label()` PAUSED reasons:
  *"manual:/label PAUSED"* (`server.py:2900`).
- `FEEDBACK_CLASSIFIER_PROMPT` (`server.py:296-308`) — a long Hermes
  classifier prompt for `/feedback`.

### 5.4 Customer responses

None. The bridge never embeds a literal customer reply. Even error
fallbacks are operator-facing markdown (`⚠️ ...`).

---

## 6. Hardcoded references in workflow code (`workflows/phase-1b-telegram.json`)

### 6.1 Hardcoded admin chat / phone numbers

| Value | Occurrences | Meaning |
|---|---|---|
| `5532831477` | ~32 | Admin Telegram chat ID (Zayn / operator) |
| `971589502303` | 1 | The bot's own WhatsApp number (= WAHA session `me.id`) |
| `97145506309` | 1 | A Dubriani contact number — found inside the Build Prompt embedded systemPrompt content (operator-side reference) |

### 6.2 Hardcoded prices

All hardcoded prices in the workflow JSON are **inside the Build
Prompt embedded systemPrompt content** (which is itself the LLM's
input). Examples (extracted as raw substrings):

```
AED 1,000   AED 1,200   AED 1,400   AED 1,500   AED 1190
AED 12,000  AED 125    AED 2,500   AED 400 pp   AED 500/hr
AED 7,000/hr   AED 799   2192 AED   500/hr   1hr
```

These are not hardcoded in code logic — they're prose embedded inside
the prompt content. The same numbers appear in the master
`system-prompt.md`, just with slightly different formatting.

### 6.3 Hardcoded yacht refs (in workflow)

Same story: all yacht names in the workflow JSON appear inside the
Build Prompt embedded systemPrompt content. Examples:

```
Satoshi 70    Pershing 82    Pershing 5X   Diana 50   Élan 44
Sunseeker     Ferretti       Galeon        Carina     Belle
```

### 6.4 Embedded long strings in nodes other than Build Prompt

After scanning every node for string literals > 200 chars: **all hits
land in `Build Prompt`**. No other node carries inline customer-facing
copy. Operator-facing strings (acks, error messages, button labels) are
all short.

---

## 7. Files / brochures / media references

### 7.1 In the system prompt (master + embedded both)

- **§6 (lines 228–232):**
  - *"📄 Branded PDF brochure — operator-sent. You can say 'i'll have
    the brochure sent across shortly' but never 'sending now' (you can't
    attach files yourself)."*
  - *"📍 Google Business / GMB link with reviews — share the link
    directly in text."*
  - *"🎥 Yacht video or Instagram reel — share the link directly in
    text."*
- **§8 catering (lines 372–374):** *"per PDF"* references for menu
  options (Premium BBQ / Fine Dining / Russian Fine Dining) — the
  brochure-or-menu PDF is operator-attached, not model-attached.

### 7.2 In code

- **No file paths to PDFs, images, or attachments** in `server.py` or
  the workflow JSON.
- **No WAHA `sendImage` / `sendDocument` nodes** — the workflow only
  uses `sendText` via WAHA.
- **No URL list / catalog of media** — the LLM is instructed in §6 to
  reference media generically but never to construct URLs.

### 7.3 Conclusion

Hermes currently has **zero attachable assets**. Every reference to a
brochure, menu PDF, or video tells the model that "the operator will
send it" — which means in practice nothing is sent unless the operator
manually uploads.

---

## What's missing (vs the planned 20 Cowork docs)

The 20 Cowork operating docs are not yet visible to me from this
audit. Based on what the 17 existing `~/.hermes/skills/Dubriani*.md`
files cover (which are NOT currently being loaded into draft
generation), the following content is **drafted but dormant**:

1. **Country-specific message templates** (Per-Country Message
   Templates.md) — UAE / India / Russia / UK / etc. openers and lines.
   The system prompt has the META-RULE B (mirror cultural signals) but
   no actual templates.
2. **Per-yacht conversion analysis** (Per-Yacht Conversion Analysis.md)
   — win rates per yacht, msg counts to close. The system prompt names
   the yachts but doesn't carry conversion data.
3. **Country × time ad scheduling** (Ad Scheduling - Country × Time.md)
   — not relevant to drafting but exists.
4. **Master Agent Skill** (60 KB) — the comprehensive "mindset"
   playbook. Some of this is in §18 of the system prompt but the
   skills file is 10× larger.
5. **Name & Demographic Recognition** — the "Displaced Customer" 33%
   rule. The system prompt mentions cultural micro-signals but not
   this specific finding.
6. **VIP Re-engagement Strategy** — 11 KB of follow-up plays. The
   system prompt has §1.VIP and §1.Repeat-customer but not the play-
   book detail.
7. **Winning Phrases Library** — exact opener variants ("Maria short
   [12×]" etc.). The system prompt has no such corpus.
8. **Example Library** — calibration examples (length p50=57 chars,
   p75=98). The system prompt has rules but not exemplars.
9. **WhatsApp Cheat Sheet** — 15-minute response rule lives here. The
   system prompt doesn't carry the 15-minute rule literally.
10. **HNW Conversion Playbook** — high-net-worth-specific plays.
11. **Loss Reason Recovery Playbook** — the system prompt has loss-
    reason ladders (§1.Loss) but the skills file has the analysis +
    recovery scripts.
12. **Customer Pattern Recognition (24 KB)** — the 60-second cheat.
    The system prompt has some pattern signals but not this depth.

The 20-doc Cowork integration likely overlaps with most of items 1–12
plus possibly new operating doctrine. Until the actual 20 docs are
visible, the gap can't be quantified more precisely.

---

## What's redundant / outdated

1. **`Dubriani WhatsApp Agent - System Prompt.md` in
   `~/.hermes/skills/` (May 21, 36 KB)** is essentially a stale
   parallel copy of `~/hermes-bridge/system-prompt.md` (May 23, 34 KB).
   Newer prompt-calibration edits (lowercase-opener, payment anti-URL
   clause) live only in the bridge file. **Recommend retiring the
   skills-dir copy** — it has no consumer that the bridge or workflow
   touch, and any operator who reads it gets out-of-date guidance.
2. **`~/.hermes/skills.bak-2026-05-21/`** — a full backup of the skills
   directory dated May 21. Safe to archive elsewhere; it's not loaded.
3. **`server.py:85` `YACHT_NAMES` tuple** drift from the Pipeline
   Review `YACHT_KEYWORD_RE` (line 1093). The two are independent and
   serve different gates. If yacht-keyword detection is intended to
   match the same fleet, they should be unified — but that's a polish
   item, not a hot bug.
4. **`behavior_rules.id=5` and `id=9`** — both `_discarded` /
   `inactive`. Harmless but visible in any query that doesn't filter
   `active=true`. Optional cleanup.
5. **§7.6 "Retired / on-request only" in the system prompt** still
   mentions yachts that also appear in §7.2 (e.g., Élan 44, Diana 50).
   The customer_facts data confirms operators are *actively* discussing
   Élan 44 and Diana 50 (msg #8 for customer 183438170693737@lid).
   Contradiction noted in §contradictions below.

---

## What's contradictory

1. **Master system-prompt.md vs Build Prompt embedded copy** —
   covered in §1.3. The break-condition output field exists in (A)
   only, payment-link section heading casing differs. (A) is the
   production customer-message path; (B) is the dormant `/draft-
   followup` path. Anyone editing the master file expecting it to
   change customer-facing behaviour will see no effect.
2. **§7.6 Retired vs §7.2 Live conflict for Élan 44 / Diana 50** —
   the prompt lists these in both Essential tier and Retired tier
   sections. `customer_facts` shows operators are still extracting
   them from inbound messages, so customers are still asking about
   them. The prompt doesn't tell the model which side is correct.
3. **`YACHT_NAMES` vs `YACHT_KEYWORD_RE` divergence** — server.py
   line 85 vs line 1093 (see §6 above). Two different lists. Possible
   that extraction gate and label-eval gate consider different yacht
   universes.
4. **`behavior_rules.id=8` "Never offer phone calls" applies
   globally** but is enforced only via the rule-injection layer in
   `fetch_behavior_rules()`. The system prompt itself does NOT contain
   this rule. If `fetch_behavior_rules()` fails (DB outage), the LLM
   has no instruction to avoid phone-call offers — the safety net is
   absent in the prompt.
5. **`~/.hermes/skills/` contains 17 high-quality operating docs that
   no production code actively loads.** The skills exist but the
   bridge does not read from `~/.hermes/skills/` in either
   `build_query()` or `build_improve_query()`. Whether Hermes CLI
   itself auto-loads them when invoked is not confirmed. The
   intended-but-not-wired state is the largest contradiction in the
   current architecture.

---

## Inventory mechanics (audit reproduction)

This document was assembled by:
1. SCP'd `~/hermes-bridge/system-prompt.md` from the box →
   `wc -l` + `grep -nE "^#"` for headers.
2. `python3 -c "import json; ..."` against
   `workflows/phase-1b-telegram.json` to extract the Build Prompt's
   `systemPrompt` assignment.
3. `diff` between master + embedded.
4. `ssh dubriani-ec2 'ls -la ~/.hermes/skills/'` to inventory skill
   files; `awk` to read first content section of each.
5. `psql` queries via `docker exec n8n-postgres-1` for tables.
6. `grep -nE` on `server.py` and `workflows/phase-1b-telegram.json`
   for hardcoded yacht names, prices, phone numbers, file references.

No file was modified. No code was deployed. No DB write occurred.
