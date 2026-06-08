# Root B — persist-on-live-path: design note (2026-06-08)

**STATUS: DESIGN ONLY. Nothing wired. No code/n8n change.** Next session starts
with the **canonical @lid↔@c.us map**, THEN this persist work. HEAD at writing: `9548dad`.

---

## Root cause (confirmed, read-only forensic `wpbvzy9d3` + code trace)
The live n8n inbound flow (`azPIy9OcDwiPV5uY`) calls **only** `behavioral-context`
(a READ — POST `:8788/feedback{action:behavioral-context}` → `behavioral_context()`,
server.py:1508) to build the drafter's context. It **never persists**. So on each
inbound, **nothing** writes `conversation_state` or `customer_facts`. The durable
record only appears ~1h26m later as the **intake-gap cron's hollow @c.us stub**
(`conversation_state` all-NULL → invisible to reengage/owe-reply/followup crons +
a never-analyzed NEW in /review). This is the Root B "invisibility."

## The two hooks that EXIST but are UNWIRED on the live path
- **`/record-message`** (`handle_record_message`, routes.py:1726) — docstring
  literally instructs: *"call this right after the WAHA Webhook node, BEFORE the
  Filter/Claude nodes. RECORD-BEFORE-AI… the +971568241103 silent-intake-drop root
  cause."* Writes the durable transcript only (`conversation_messages`). Fail-safe,
  always-200.
- **`customer_message` event** → `upsert_conversation_state(cid, 'customer_message')`
  (server.py:4024/4036). Bumps `conversation_state.last_customer_message_at = now()`
  — **this is what un-hollows the stub for the crons.** Currently called from ONLY
  one place: the event endpoint at routes.py:~1685 (also unwired on inbound).
- ⚠️ **NEITHER creates `customer_facts`.** That row is created only by the
  analyzer/cron extraction (`/refresh-facts`, `_upsert_facts_sql`). So /review
  visibility needs a `customer_facts` stub too — a small bridge add on whichever
  hook we wire.

## Approaches (decision deferred — DO NOT pick until the map exists)
**A — wire record-before-AI hooks in n8n (the intended design):** fail-safe HTTP
nodes right after the WAHA Webhook → `/record-message` (transcript) + the
`customer_message` event (conversation_state) + a tiny bridge tweak so that event
also ensures a minimal `customer_facts` NEW stub. Captures **every** inbound (even
non-drafted) → true drop-protection.
- *Risk:* **highest-risk change in the queue** — an edit to the LIVE inbound path
  every message flows through. Must be `safe_put` (NOT deploy_bridge), fail-safe
  nodes (always-200, can't break the flow), with a **test-inbound verification**.
  **Top-of-session, supervised — never an end-of-session change.**

**B — bridge-only (no n8n edit):** augment the `behavioral-context` handler
(routes.py:6804) to also bump `conversation_state` + ensure a `customer_facts`
stub. Zero n8n risk, but only fires on **drafted** messages (misses debounced /
excluded inbounds) → weaker drop-protection than A.

**Leaning A** (it's what the code was built for; record-before-AI is the only one
that protects every inbound), done carefully — but ONLY after the map.

## ⚠️ THE DEPENDENCY — build the canonical map FIRST
Persistence lands under the cid n8n passes (the **@lid**). Today there is **no
write-side canonical `@lid↔@c.us` map**, so persisting now would write @lid rows
that **duplicate** the cron's @c.us stubs — building on a missing foundation that
"the map later has to reconcile." So the order is:

1. **Canonical `@lid↔@c.us` map (NEXT — low-risk, additive read-side).** A durable
   mapping/dedup so an @lid lead and its @c.us stub resolve to ONE canonical id.
   **Verify it resolves the 3 dropped numbers to real phones** and dedupes their
   stubs: `971568241103` (`63977933553823@lid`), `97477660900`
   (`181303689359486@lid`), `971505580190` (`109024070606877@lid`).
2. **THEN this persist work** — lands under the canonical id the map provides
   (no duplicates). Prefer Approach A; `safe_put`; test-inbound verify; top-of-session.

### Distinction (so next session isn't confused about what exists)
- **Read-side phone→@lid RESOLVER = DONE (`25d9377`, this session).**
  `resolve_customer_by_phone` resolves @lid via the live WAHA lids map (cached
  10min, fail-soft); verified live Xeno `971506798997`→`104711420166164@lid`,
  Antonio `971588211789`→`31890199318638@lid`. Makes @lid leads findable by
  `/lead`/`/label`/`/info`. **This does NOT dedupe writes.**
- **Write-side canonical `@lid↔@c.us` map / dedup = PENDING.** This is the
  foundation that must precede persist. (NOT the same as the read-side resolver.)

## Bottom line for next session
START with the canonical `@lid↔@c.us` map (low-risk). Verify the 3 numbers resolve
+ dedupe. THEN wire persistence (Approach A, `safe_put`, test-inbound), as a fresh
top-of-session supervised action. Nothing here is wired yet.
