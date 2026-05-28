# Dubriani AI Sales Agent — System Report (2026-05-29)

A developer-oriented walkthrough of how the system is built, what each
button/flow does, the logic behind drafting / sending / nudging /
sorting / analysis / self-improvement, timings, and an honest
future-proofing assessment. Written for sharing with a developer / Claude
planning chat.

---

## 1. What it is (one paragraph)

A **human-in-the-loop WhatsApp sales agent** for a Dubai yacht-charter
business ("Maria"). Customers message on WhatsApp; the system drafts
replies with Claude; a single operator reviews/edits/approves each draft
in a **Telegram bot** before it's sent. On top of the reply loop there's
a **pipeline/CRM layer** (labels, scoring, proactive nudges, payment
tracking, self-improvement). One operator, ~hundreds of leads.

---

## 2. Architecture & components

| Component | Role | Notes |
|---|---|---|
| **n8n** (`n8n-n8n-1`) | Orchestration — webhooks, routing, the Telegram UI, all button handling | 5 workflows (below). Postgres-backed. |
| **hermes-bridge** | Python HTTP service (port 8788), the "brain" — all business logic, DB access, Hermes/WAHA/Telegram calls | systemd `--user` unit, ThreadingHTTPServer, `X-Bridge-Token` auth. ~6 modules: server, routes, payments, review, labels, waha, hermes_calls, db. |
| **Redis** (`n8n-redis-1`) | Live draft store + debounce buffers + locks | Sole draft store post-migration (see §10). |
| **Postgres** (`n8n-postgres-1`) | CRM persistence — customer_facts, labels, notes, behavior_rules, autonomous_sends, conversation_state, triggers | Also n8n's own DB. |
| **WAHA** (`n8n-waha-1`) | WhatsApp HTTP API | **WEBJS engine, CORE (free) tier** → can't send native file attachments (sends Drive links instead); Plus tier would fix. |
| **Telegram Bot API** | Operator UI (the approval cards + commands) | `dubriani_hermes_bot`, admin chat 5532831477. |
| **Nomod** | Payment links + payment confirmation | No real webhook reliability → polled every 2 min. |
| **Claude API (Anthropic)** | Customer-reply drafting (direct from n8n) | Separate from Hermes. |
| **Hermes CLI** | Claude wrapped as a CLI the bridge shells into | Used for nudges, refine, improve, analysis, fact extraction, /assist. ~7–45 s/call. |
| **Caddy** | Reverse proxy / TLS | n8n + WAHA behind sslip.io domains. |
| **Host** | **AWS EC2, 2 vCPU / 4 GB** | **The main scaling bottleneck** (see §11). |

### The 5 n8n workflows
1. **Phase 1B — WhatsApp + Telegram Approval** (`azPIy9OcDwiPV5uY`) — the main one: inbound messages, drafting, the Telegram cards, every button.
2. **Payment Poll Cron** — every **2 min** → bridge `/poll-payments` (Nomod has no usable webhook for us).
3. **Pipeline Analyze Cron** — every **1 hour** → scores/analyses leads (`hermes_analyze_lead`).
4. **Hourly Sweep** — every hour (`0 * * * *`) → re-evaluates labels (decay, past-date demotion, etc.).
5. **Pipeline Review Cron** — **05:00 & 13:00 UTC** → posts the scheduled `/review` digest.

Plus `cron-reminders.py` / `cron-daily-summary.py` (system-cron style helpers).

### Why two layers (n8n + bridge)?
n8n is great for the **event plumbing + Telegram UI** (webhooks, buttons, fan-out) but bad at complex logic. All real decisions live in the **bridge** (testable Python, version-pinned, no model-client deps in n8n). n8n nodes are thin: they call bridge endpoints and render the result. This is a **sound separation** and the single best structural decision in the codebase.

---

## 3. Data model (where state lives)

**Redis (live, ephemeral, 24 h TTL):**
- `draft:<id>` — the draft object (messages, customer_phone, status, telegram_message_id, file_key…). **The source of truth for what Send sends.**
- `drafts:bycustomer:<cid>` (ZSET), `drafts:active` (SET) — indexes.
- `tgmsg:<telegram_msg_id>` → draft_id — reverse index so "reply to a card" resolves.
- `draft:awaiting_edit:<chat>` / `draft:awaiting_amount:<chat>` — singletons (which draft is awaiting operator input).
- `draft:send_claim:<id>` — SET-NX lock (anti double-send).
- `filesend:<cid>:<file_key>` — SET-NX lock (anti double file-send).
- `debounce:*` — inbound message buffers.

**Postgres (durable CRM):**
- `customer_facts` — name, dates, yachts, party_size, **label**, importance_score, suggested_action, importance_analyzed_at, merged_into (de-dup pointer).
- `conversation_state` — last_customer_message_at, last_operator_reply_at, last_nudge_drafted_at, followup_count, last_analyzed_at.
- `customer_label_history` — every label transition (audit + self-improvement).
- `behavior_rules` — operator-taught rules (global + per-scenario).
- `customer_notes` — per-customer notes.
- `autonomous_sends` — every send/payment event (audit + dedup).
- `customer_triggers` — payment_link_sent, booking_intent, reminders.

---

## 4. End-to-end: inbound message → draft on Telegram

```
WhatsApp msg → WAHA webhook → n8n "Filter Inbound" → Debounce buffer (Redis)
  → [wait ~60s for the customer to finish typing / send follow-ups]
  → Flush → bridge: customer-facts extract + label-eval (Hermes, background)
  → n8n "Build Prompt" (Set node injects the 65k system prompt + history)
  → Claude API (direct) drafts the reply
  → "Queue & Format" builds the card → Persist Draft (Redis) → send card to Telegram
  → ~20s later: auto-improve pass (Hermes) re-polishes the card (atomic commit)
```

**Timings (typical):**
- Debounce window: **~60 s** (collapses rapid multi-message bursts into one draft — avoids drafting 4× for one thought).
- Customer-facts extraction (Hermes): **~7–15 s** (background priority).
- Drafting (Claude direct API): **~3–8 s**.
- Card appears in Telegram: **~debounce + 5–10 s** after the customer's last message.
- Auto-improve pass: fires **~20 s after** the card, re-writes it once.

**Why debounce?** Customers send "hi" / "I want a yacht" / "for friday" as 3 messages. Without debounce we'd draft 3 times. The 60 s window waits for them to finish.

**Drafting uses Claude *directly* (not Hermes)** — this is deliberate: the reply draft is latency-sensitive (operator is waiting), so it skips the Hermes CLI overhead and calls Anthropic from n8n with the full system prompt + WAHA history. **Hermes (the CLI) is used only for governance/secondary tasks** (nudges, refine, improve, analysis, facts, /assist).

---

## 5. The buttons — what each one does (pressing → effect)

Every card carries an inline keyboard. A tap = a Telegram **callback** → n8n `Parse Callback` (resolves the draft from Redis) → `Answer Callback` (instant toast, <1 s) → `Route Action` (a 16-way switch on the action) → the handler.

| Button | Action | What happens | Latency |
|---|---|---|---|
| **✅ Send** | `send` | `Prepare Send` claims an atomic SET-NX lock (anti double-send), fans the draft's `messages[]` to WAHA `sendText`, marks `sent`, **edits the card to "✅ SENT" and removes its buttons**. | ~1–3 s |
| **✏️ Edit** | `edit` | Sets the `awaiting_edit` lock (Redis singleton) + shows "tell Claude what to change". Your next message is treated as the edit → refine. | <1 s + refine |
| **🔁 Regen** | `regen` | Re-drafts (Claude) and **atomically** updates the card **and** Redis together (`regen-commit`) — what you see = what Send sends. | ~5–10 s |
| **💬 Draft nudge** | `nudge` | Hermes drafts a follow-up for a silent lead → posts a draft card. | ~10–40 s |
| **❌ Skip** | `skip` | Disables the card; marks `skipped` in Redis (leaves the active set). | <1 s |
| **💳 Send Link** | `paylink` | Creates a Nomod payment link (asks for amount if missing), posts it with the recipient phone for confirmation. | ~2–4 s |
| **📎 Send File** | `file` | Sends a registry file (Drive link on CORE tier) once (SET-NX dedup against double-taps). | ~2–4 s |
| **🤖 Auto** | `auto` | Hands the conversation to autonomous mode (arms an auto-send timer + safety caps). | <1 s |
| **ℹ️ Info** | `inf` | Pulls a customer dossier (facts, label, history). | ~1–3 s |
| **🛑 Disregard** | `disregard`/`disregard_force` | Hermes checks if it's really dead; if it says "keep open" you get **[🛑 Close anyway]**; confirming **edits the lead card in place to "DISREGARDED" with no buttons**. | ~5–15 s |

**Edit / reply resolution** (how the bot knows which draft you mean): your reply resolves via the `awaiting_edit` lock (if you tapped Edit) → else `tgmsg:<id>` reverse index (if you replied to a card). A reply that resolves to nothing **never** falls through to /assist anymore (that was the wrong-customer bug).

---

## 6. Sending logic (and the anti-double-send chain)

`Send` → `Prepare Send` does `SET NX draft:send_claim:<id>` — only the **first** tap wins; a double-tap / Telegram retry / queue replay gets `ok=false` and bails. Then it fans `messages[]` (a draft can be multiple bubbles) to WAHA `sendText`, marks the draft `sent`, and edits the card to "✅ SENT" **with the keyboard cleared** so you can't re-tap. File-send has its own `SET NX filesend:<cid>:<key>` 60 s lock for the same reason.

**WAHA constraint:** CORE (free) tier can't send native media. `send-file` sends the Drive **share link** as text. Buying WAHA Plus enables native attachments with zero code change.

---

## 7. Nudge logic (proactive follow-ups)

Two entry points: the **operator taps [💬 Draft nudge]**, or the **proactive engine** (driven by the Hourly Sweep / Pipeline Analyze) decides a silent lead deserves a follow-up.

The proactive engine selects a customer for a nudge only if **all** hold:
- they messaged in the last 30 min – 7 days (silent, but not dead),
- we haven't already replied since their last message,
- `followup_count < cap`,
- label isn't an operator-handled / paused state, no paylink in the last 24 h,
- **and (new 2026-05-29) we have NOT already nudged them since their last message** — one nudge per silence cycle; if they don't reply, we back off (the cycle resets when they message again).

The nudge draft itself is written by Hermes with the conversation history + an "anti-pushiness" instruction (don't re-pitch a customer who hasn't replied). This is the **anti-spam** guarantee: it directly prevents the "we sent the same pitch 4×" problem.

---

## 8. Sorting logic (pipeline `/review`)

Each lead gets a **score** = label-tier base + booking-urgency bonus + yacht-rate bonus + Hermes importance. Then the report is grouped into **label sections** rendered top-to-bottom: WAITING_FOR_PAYMENT → HOT → NEEDS_ATTENTION → WARM → NEW → COLD → **CONFIRMED (always at the bottom)**.

**Within each section, leads are sorted STRICTLY by the hourly rate of the yachts they want** (descending; e.g. AK Royalty 136 @18,000/hr > Tatti 110 @9,000 > Pershing 82 @5,500 > Bliss 55 @1,400). Rates come from the catalog (`YACHT_RATE` in review.py). The additive score is only a tiebreaker. **NEW** stays sorted by recency (no yacht chosen yet).

Cards are delivered one-at-a-time (350 ms apart) so Telegram keeps them in order; each card also carries a section badge (🔥 HOT / ✅ CONFIRMED) so it's self-identifying.

`/review` also shows freshness: if a lead has new activity since its last analysis, it suppresses the stale recommendation and shows **"followed up X ago — awaiting reply"** or **"new activity since last analysis — re-check"** instead of a stale "send nudge".

---

## 9. Hermes analysis — when & why

| Trigger | What | Cadence | Why |
|---|---|---|---|
| **Per inbound message** | customer-facts extraction (name/dates/yachts/party) + label-eval | on each flush | keeps the CRM header + label current |
| **Pipeline Analyze cron** | `hermes_analyze_lead` → importance_score (0–100) + verdict (keep_open/close) + suggested_action + reasoning | **hourly** | ranks/triages leads, drives `/review` recommendations |
| **Hourly Sweep** | label re-evaluation (decay HOT→WARM→COLD, past-date demotion, sticky rules) | **hourly** | keeps labels honest as time passes |
| **Operator action** | nudge / refine / improve / /assist | on demand | interactive |

**Concurrency control (important):** all Hermes CLI calls share a pool capped at **3** on the 2-vCPU box, split into **2 interactive + 1 background** lanes. Interactive work (your edits/nudges/drafts) always gets a reserved slot so the hourly background sweep can't make the buttons unresponsive. Without this, a sweep coinciding with a live chat caused 40 s+ button lag.

Label rules (`compute_label`) are **deterministic regex/signal heuristics** (fast, no LLM): money mentioned → HOT, "book the yacht"/"let's do it" → HOT, payment confirmed → CONFIRMED, date passed → COLD, etc. The LLM analysis layers *importance/priority* on top.

---

## 10. Self-improvement

- **Operator feedback** (`/feedback <text>`): Hermes classifies it (global rule vs per-customer note vs scenario rule) → stored in `behavior_rules` / `customer_notes`. These are **injected back into future drafts** for that customer/scenario. The operator can `/rules`, `/approverule`, `/discardrule`.
- **Label-correction dampening:** when the operator manually overrides a label, the system records it. If an auto-signal keeps getting corrected (≥ N times in a window), its **confidence is dampened** so the auto-classifier stops fighting the operator on that signal. ("Sticky-upward" guard prevents a single weak signal from demoting an operator-set label.)
- **Payment reconciliation (new):** every poll, any customer with a real deposit on file but a non-CONFIRMED label is auto-promoted — self-healing CRM truth.

This is real but lightweight self-improvement — rules + dampening, not model fine-tuning.

---

## 11. "Talk freely" (`/assist`) — the natural-language operator assistant

Type anything to the bot **without a slash command** and it goes to `/assist`: Hermes classifies the intent and acts:
- `status_query` ("how's William?") → dossier
- `draft_nudge` ("follow up with Luke about the Bliss") → drafts a nudge card
- `send_paylink` ("send Madawi a link for 3500") → creates a Nomod link
- `send_file` ("send the food menu to Sara") → posts a [📎 Send File] confirmation card
- `find_customer` ("who's HOT?") → list/search
- `help` → suggestions

**Safety (hardened this week):** because it resolves customers by fuzzy name (and many customers have no name), every /assist action now **shows the resolved phone number + a confirm prompt** before anything sends, file-send is gated behind a button (no instant send), and unknown numbers are guided to `/send`. **A reply to a draft card never reaches /assist** (that was the wrong-customer-send root cause).

---

## 12. What I changed the last days (and why)

**Root-cause class A — "sent ≠ shown" (trust-critical):** the Telegram card and the Redis draft (what Send reads) could diverge after regenerate / auto-improve / refine, because they were written by two separate n8n nodes; an execution crash between them sent text you never saw. → Added an **atomic `regen-commit`** bridge action (edit card, then write Redis, only if the edit landed) and routed regen + improve + refine through it. Now what you see is what's sent.

**Root-cause class B — the big one: missing `BRIDGE_TOKEN`.** The Redis migration added code-node calls to the bridge authenticated with `$env.BRIDGE_TOKEN`, but that env var was never set on the n8n container → every such call got 401 → editing, reply-resolution, and the awaiting-locks silently failed (and replies leaked to /assist → the wrong-customer send). One missing env var caused a whole cascade. Fixed at the infra layer.

**Other fixes:** Send-File rule reading the wrong variable; file double-send (idempotency lock); `/assist` recipient-phone confirmation + new-number handling; Hermes priority lanes (slowness); booking-signal labeling ("book the yacht" → HOT); WhatsApp name capture (was reading the wrong WAHA field — recovered 34 real names); `/review` de-duplication (merged rows); anti-pushy nudges; stuck-paid → CONFIRMED reconciliation; yacht-rate-based sorting; disregard-in-place (no lingering buttons); card-locks-after-send; country flags.

All committed to `bookings-boop/dbriani` (latest: `35a556e`).

---

## 13. Future-proofing assessment (honest)

### Strong / well-built
- **n8n-thin / bridge-thick separation** — logic is testable Python, not buried in n8n nodes. Good call.
- **Redis as the single live draft store** with reverse indexes + atomic locks — once finished, the right model.
- **Deterministic label rules + LLM importance on top** — fast, debuggable, cheap.
- **Cron workflows separated from the live flow** — the live path isn't blocked by batch analysis.
- **Audit trails everywhere** (label history, autonomous_sends) — enables the self-improvement + reconciliation.
- **Idempotency locks** on the money-/customer-facing actions (send, file, payment).

### Fragile / debt to address
1. **The Redis migration is a hybrid.** ~18 nodes still read/write n8n `staticData` vestigially; the BRIDGE_TOKEN cascade and several "sent ≠ shown" bugs all traced to this half-finished migration. **Recommend finishing Phase C** (rip out staticData entirely) — it's the #1 structural risk.
2. **2-vCPU / 4 GB box is the ceiling.** Hermes calls + Chromium (WAHA) + n8n + Postgres + Redis on 2 cores → the hourly sweep colliding with a live chat caused real slowness. The priority-lane fix mitigates; **4 vCPU is the real fix** for sustained volume.
3. **Two drafting paths** (Claude-direct for replies, Hermes-CLI for everything else) — pragmatic for latency but means two prompt/code paths to keep consistent.
4. **Scoring is additive with caps**, so the very top of a tier can flatten (mitigated by the explicit rate-first sort). Fine now; if ranking rules grow, consider an explicit lexicographic sort.
5. **Telegram message ordering** for rapid multi-message sends isn't guaranteed — worked around with send intervals; acceptable.
6. **WAHA CORE tier** — file attachments are links, not native. WAHA Plus removes this.
7. **n8n deploy footgun:** the deploy script's "workflow unchanged" optimization can skip importing real changes — must force-import. Documented; worth fixing the script.
8. **Telegram bot token was pasted in chat earlier** — rotated; ensure the server `.env` holds only the new token.

### Net
The **shape** is right (clean separation, single source of truth, audit + self-heal). The **debt** is concentrated in (a) the unfinished Redis migration and (b) the undersized box. Closing those two would make it genuinely robust for scaling. Nothing here is a dead-end rewrite — it's finishable, incremental work.

---

## 14. Quick timing reference

| Step | Typical |
|---|---|
| Button toast (Answer Callback) | <1 s |
| Send → WhatsApp | 1–3 s |
| Regenerate | 5–10 s |
| Refine (edit) | 10–30 s |
| Draft nudge | 10–40 s |
| Customer msg → draft card | ~60 s debounce + 5–10 s |
| Auto-improve pass | ~20 s after card |
| Hermes call (any) | 7–45 s (cap 3 concurrent, 1 reserved for background) |
| Payment detected | within 2 min (poll) |
| Pipeline re-analysis | hourly |
