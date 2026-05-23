# Pre-Launch Audit — 4 surfaces

> Read-only audit. Date 2026-05-23. Latest commits checked: `48455fa`
> (follow-up engine), `6cc3940` (Cowork integration), `b0d30da`
> (prompt strengtheners).
> 142-node main workflow, 11-node sweep workflow, 6-node review cron.

---

## Surface 1 — Follow-up engine interactions

### Q1.1 — Follow-up card + new customer message coexistence

**✅ Safe.** Two draft cards for the same customer can coexist independently.

- `Queue & Format` (`workflows/phase-1b-telegram.json` jsCode) generates `id = $now.toMillis() + '_' + Math.random().toString(36).slice(2, 7)` for customer-message drafts.
- `Build Followup Card` (`workflows/pipeline-hourly-sweep.json` jsCode line 25) generates `id = Date.now() + '_' + Math.random().toString(36).slice(2, 7)` for follow-up drafts.
- Both are millisecond + 5-char base36 random — same shape, different identity. Collision probability is ~1 in 60M *per millisecond* AND requires both to fire in the same ms. Not a practical risk.
- Each draft sits at `draft:<id>` in Redis (distinct keys). Parse Callback routes each tap by its own `draft_id` after `:` split (`hermes-bridge/server.py` Parse Callback rewrite — lines around 1380).
- Each card has its own `telegram_message_id` so [Send]/[Skip] edits the correct one.

**Edge case I checked:** the new follow-up draft has `is_followup: true` while the normal draft does not. Parse Callback doesn't read `is_followup` — it just resolves the draft and routes by action prefix. The downstream Send chain is the same for both. The `Followup Sent?` IF (line ~2390 in synced workflow) gates the audit log on `$('Parse Callback').item.json.draft.is_followup`, so audit doesn't fire for normal drafts.

### Q1.2 — /draft-followup error during sweep

**✅ Safe — sweep continues for other customers.**

- `Hermes Draft Followup` HTTP node is `onError: continueRegularOutput`. If Hermes returns rc≠0, the bridge returns `{ok:false, degraded:true, draft_text:""}` (200 OK, not raise). Downstream continues with empty draft.
- `Build Followup Card` is **the one node without `onError`**, but it's defensive: handles `(fu.draft_text || '').trim()` → falls back to "⚠️ Hermes returned no draft text — skip recommended." A Telegram card still posts; operator sees the warning + can skip.
- `splitOut` fans items individually. A per-item failure in any downstream node only kills that item; the rest continue.
- **Retry behaviour:** if `_draft_followup` fails and returns ok:false, `upsert_conversation_state(cid, "nudge_drafted")` IS NOT CALLED on the failure path (`_draft_followup` exits at the rc!=0 branch before the nudge_drafted call). So `last_nudge_drafted_at` stays NULL → eligible next hour → retry. ✅ Confirmed retry behaviour works.

### Q1.3 — When is last_nudge_drafted_at set?

**✅ Set on draft GENERATION, not Send.** Correct per spec.

- `hermes-bridge/server.py:1299` — `elif event == "nudge_drafted":` triggered by the call inside `_draft_followup` (around line 2780) which fires after Hermes returns ok and BEFORE the response is sent back to the workflow.
- The dedup query in `scan_followup_eligibility` (line ~1351) checks `cs.last_nudge_drafted_at > cs.last_customer_message_at`.
- Natural reset: when customer replies, `Mark Customer Msg` updates `last_customer_message_at` → dedup comparison flips, customer eligible again.

**⚠️ Risk noted (spec gap, not bug):** The operator spec said "dedup releases when silence window changes tier (HOT 30min-2hr → HOT 2hr-24hr)". Current implementation: once `last_nudge_drafted_at > last_customer_message_at`, all future sweeps skip until the customer replies. So a HOT customer who got a draft at 45min silence will NOT get a second draft at 3-hr silence in the next window. By design this avoids spam, but contradicts the "escalate across tiers" spec line. Likely the right call for v1 — flag for operator awareness.

### Q1.4 — Send on stale follow-up after customer re-engages

**⚠️ Risk — no check in the Send chain.**

- `Prepare Send` (jsCode at the line shown in audit) reads `$('Parse Callback').item.json.draft.customer_phone` and `draft.messages[]` then fans into `Send One to Customer`. **No check for "customer replied between draft creation and Send tap".**
- Realistic scenario: operator delays approving a follow-up card by 1+ hour. In that hour, the customer messages on WhatsApp ("oh sorry, yes still interested!"). Operator now taps Send on the stale "have you given up?" card → WAHA sends the awkward message anyway.
- **Mitigation in practice:** Telegram chat shows the operator other recent activity for that customer (since the new customer message produces a NEW draft card on top of the follow-up card). Operator should visually catch this before tapping.
- **Severity:** moderate. Awkward UX, not a hard bug. Not blocking v1.

---

## Surface 2 — Payment link (Nomod) flow

### Q2.1 — should_send_payment trigger conditions

**✅ Documented in system prompt §19 (lines 570-584).** The LLM must include all 3 fields only when ALL of these hold:

1. Yacht confirmed
2. Date AND duration confirmed
3. Price confirmed
4. Customer has clearly said they want to book ("book it" / "let's do it" / "I'm in" / "send the link" / equivalent)

If any uncertain → `should_send_payment: false` + ask in words. The prompt also explicitly forbids the LLM from pasting any URL placeholder text (this rule was added in `6101e0a`).

### Q2.2 — Nomod API down handling

**✅ Safe — graceful degradation.**

- `nomod_create_link` (server.py around line 253) catches `urllib.error.HTTPError` AND general `Exception`. 10s timeout. Returns `(None, None, "Nomod ..err..")` tuple.
- `_payment_link` handler returns 200 with `{ok:false, error}` on any failure. Never raises.
- `Generate Payment Link` workflow node: `onError: continueRegularOutput`.
- `Build Payment Message` jsCode (live workflow) checks `link.ok === true && !!link.link_url` — if false, posts the card with `payment_header = '⚠️ PAYMENT LINK NOT CREATED — <error>'` and the LLM's reply WITHOUT a link appended.
- **The operator sees the warning header before tapping Send. ✅**

**⚠️ Risk noted:** when Nomod fails, the LLM's reply text may still imply "your link is on the way" (since the LLM didn't know Nomod would fail). The card warning is in the OPERATOR header — not in the reply text — so if the operator misses the header and taps Send, the customer gets a warm-confirm with no link. Header is loud (`⚠️` + ALL CAPS) — should be hard to miss but worth operator awareness.

### Q2.3 — should_send_payment with null/zero amount

**✅ Safe — sanity check enforced.**

- `_payment_link` handler reads `amount = float(payload.get("amount"))` in a try/except — `None` → `TypeError` → `amount = 0.0`.
- Then `if amount <= 0` → refuses with `ok:false, error="invalid amount: <val>"`. Logs warning.
- Build Payment Message then renders the ⚠️ header as in Q2.2.

**Side effect:** the LLM is told NOT to invent prices. If it omits `payment_amount` while setting `should_send_payment: true`, the bridge refuses. The card posts WITHOUT a link AND with the warning. Operator sees and either rejects or corrects.

### Q2.4 — should_send_payment on follow-up draft (is_followup: true)?

**✅ Cannot fire by design.**

- The `Check Payment Trigger` IF gates on `$('Parse Response').item.json.should_send_payment === true`. `Parse Response` is the node that parses the **main workflow's** `Claude AI` output for customer-message drafts.
- Follow-up drafts come from `_draft_followup` (Hermes CLI), and the resulting `draft_text` is fed DIRECTLY into `Build Followup Card` → `Persist Followup` → `Send Followup Card`. The workflow path NEVER touches `Check Payment Trigger`.
- Even if Hermes returned `should_send_payment: true` in its JSON, the bridge's `_draft_followup` only extracts the `messages` array (and `draft_text` join). It doesn't propagate `should_send_payment` in the response. The downstream Build Followup Card never sees it.
- The follow-up directive (server.py around line 2750) explicitly tells Hermes: *"DO NOT pitch add-ons, upsells, perks, or new options."* Plus the §19 trigger conditions wouldn't fire on a one-line ghost-recovery message anyway.

---

## Surface 3 — Callback collision risk

### Q3.1 — Two different draft cards same draft_id?

**✅ Effectively zero collision risk.**

- Both generators emit `<millisecond-timestamp> + '_' + 5-char base36 random`. Identical shape.
- For collision both must fire in the same millisecond AND draw the same `Math.random().toString(36).slice(2, 7)` (1 in 60,466,176).
- At ~10 customer-msgs/hour + ~10 follow-ups/hour, two-in-same-ms is already vanishingly rare; with the 60M-suffix dilution, **practical collision risk = ~0.** ✅
- Even if a collision happened, the second `Redis SET draft:<id>` would silently overwrite the first — the operator would see one of the two cards become "DRAFT EXPIRED" on its other action. Recoverable.

### Q3.2 — Parse Callback prefix routing

**✅ Correctly disambiguated.**

- Parse Callback (`workflows/phase-1b-telegram.json`, the rewritten jsCode shown above):
  - `pipeline = (action === 'nudge' || action === 'snz' || action === 'inf')` → pipeline-review callbacks (Pipeline Review buttons)
  - Everything else (`send` / `skip` / `edit` / `regen` / `auto` / `takeover` / `feedback_*`) → looks up `draft_id = parts[1]` in staticData + Redis fallback.
- Follow-up cards use the SAME `send:/skip:/edit:` prefixes as normal drafts (intentional — they should route through the existing Send chain). Differentiation happens downstream via `draft.is_followup` for the audit-log arm only.
- No prefix is reused with different meaning. No routing ambiguity.

### Q3.3 — Operator taps buttons on 2 cards within seconds

**✅ Safe for different drafts. ⚠️ Theoretical race for same draft tapped twice.**

- Different drafts → independent Redis keys (`draft:<id1>` vs `draft:<id2>`) → independent execs → no contention.
- Same draft tapped twice (rare, since Telegram clients typically debounce inline-keyboard presses):
  - Both execs call `_draft_update` which does read-modify-write on `draft:<id>` in Redis. NO MULTI/transaction.
  - Last-write wins. If exec A sets `status='sent'` and exec B sets `status='skipped'` racing, one of the two transitions is lost.
  - **Functional impact: minimal.** The Send chain (`Send One to Customer`) fires WAHA send unconditionally for the action it processed — both execs would send the same draft text twice if they tap Send twice. **That IS a bug.**

**⚠️ Real bug surfaced:** If operator double-taps [✅ Send] (or "send" callback fires twice for any reason — Telegram retry, finger fumble), WAHA may send the same message to the customer twice. No `messages_sent_count` increment-and-check protects this.

`Mark Sent` increments `messages_sent_count` but happens AFTER `Send One to Customer`, so the second tap reads `messages_sent_count=0` (still 0 because exec A hasn't reached Mark Sent yet) and proceeds to send.

**Severity: medium.** Operator-induced. Not blocking v1 because Telegram clients debounce taps in practice, but operators tapping fast under stress could hit it. Recommended fix: gate Send chain on `draft.messages_sent_count === 0` and reject if already sent.

---

## Surface 4 — Data consistency

### Q4.1 — conversation_state vs customer_facts update paths

**⚠️ Two parallel arms can diverge if one fails.**

- `customer_facts` writer: single node `Customer Facts` (POST /customer-facts) — fires per customer message via Buffer Message → Flush Check chain.
- `conversation_state` writers: 4 nodes
  - `Mark Customer Msg` (POST /conversation-state event=customer_message) — parallel arm off Customer Facts
  - `Mark Op Reply (Approved)` (event=operator_reply) — after `Send One to Customer`
  - `Mark Op Reply (Auto)` (event=operator_reply) — after `Auto Send WAHA`
  - `Mark Draft Posted` (event=draft_posted; Redis only) — parallel arm off `Send Draft to Telegram`
- Customer Facts AND Mark Customer Msg fire **in parallel** off the same upstream. Both `onError: continueRegularOutput`. If bridge has transient hiccup affecting one but not the other, they diverge.
- Realistic divergence vector: bridge restart mid-fire. Either both fail (workflow recovers next time) or both succeed (normal). Selective single-arm failure is unlikely.

**Verdict:** Acceptable v1. Not a hot bug. The sweep tolerates stale `last_customer_message_at` (it just skips; eventually catches up).

### Q4.2 — conversation_modes killswitch state

**ℹ️ Informational, not a bug.** Current state (live DB):

| customer_id | mode | activated_by | when |
|---|---|---|---|
| 157208872501273@lid | approval | manual_killswitch | 2026-05-22 21:37 |
| 5532505120995@lid | approval | manual_killswitch | 2026-05-22 21:37 |
| 971509767187@c.us | approval | manual_killswitch | 2026-05-22 21:37 |
| fr4-test@c.us | approval | manual_killswitch | 2026-05-22 21:37 |
| 253570691645643@lid | **autonomous** | operator | 2026-05-23 13:20 |
| 274942918680787@lid (Mark) | **autonomous** | operator | 2026-05-23 12:33 |
| 80762783182909@lid | **autonomous** | operator | 2026-05-23 13:42 |

3 customers in autonomous mode right now. The 4 killswitch customers from yesterday remain in approval — they don't auto-revert. **To restore one: operator types `/auto` while replying to that customer's draft card** (the existing FR-4 mode command). Or types `/auto` standalone for the active conversation.

**Recommendation:** none for code. Operator may want to audit those 4 customers manually and either /auto them or leave in approval depending on situation.

### Q4.3 — autonomous_sends.kind distinguishability

**✅ Safe — kinds are queryable separately.**

Live distribution:
```
intervention | 37   (break-condition fired)
auto         | 18   (autonomous-mode auto-send)
checkpoint   |  2   (safety cap checkpoint)
```

No `proactive_followup_*` rows yet (the engine just shipped — production sweeps haven't yielded eligible customers yet; my live test cleaned itself up).

When the engine fires, it will use:
- `proactive_followup_drafted` — every sweep that generates a draft
- `proactive_followup_sent` — operator tapped [✅ Send]
- `proactive_followup_skipped` — operator tapped [❌ Skip]
- `proactive_followup_edited` — operator tapped [✏️ Edit] (logged via the same path)

Each has a `notes` JSONB with `{label, silence_window, silence_hours, draft_text}` (from migration 003). Existing rows (auto/intervention/checkpoint) have `notes=NULL`.

Standard SQL filters: `WHERE kind LIKE 'proactive_followup_%'` vs `WHERE kind = 'auto'` vs `WHERE kind IN ('intervention','checkpoint')`. ✅

### Q4.4 — /note propagation to next draft

**✅ Propagates immediately. No bridge restart required.**

- The operator's `/feedback` (action=save) writes to `customer_notes` via direct INSERT (`hermes-bridge/server.py`, in the `_feedback` handler — `INSERT INTO customer_notes`). Commit is immediate.
- On the next customer message, the workflow's `Fetch Behavioral Context` httpRequest (URL `/feedback`, body `{action:"behavioral-context", customer_id}`) calls `behavioral_context(customer_id)` which queries:
  ```
  SELECT note_text FROM customer_notes
  WHERE customer_id='<cid>' AND active=true
  ORDER BY id DESC LIMIT FEEDBACK_MAX_PER_CUSTOMER
  ```
- No in-process cache. Direct DB read each time. ✅
- `Build Prompt` then injects the returned `formatted` block (the "## Behavioral context" header + bullet list) into the LLM input. Next draft sees the note.

---

## Summary

### ❌ Bugs found (must fix before v1)

1. **(S3.3) Double-Send race on the same draft.** Two callback execs for the same `send:<draft_id>` can both fire `Send One to Customer` before `Mark Sent` increments `messages_sent_count`. WAHA receives 2 sends. **Recommended fix:** In `Prepare Send`, return empty array if `draft.messages_sent_count > 0`. Same guard logic that the FR-5 improver-skip uses for autonomous drafts.

### ⚠️ Risks found (monitor in production)

1. **(S1.3) No tier escalation in follow-up dedup.** A customer who gets a follow-up at the HOT 30min-2hr window and never replies won't get a second one at the 2hr-24hr window. Confirmed safe v1 behavior (avoids spam) but contradicts the spec line about tier escalation. Decide later if you want a second-tier draft.
2. **(S1.4) Send on stale follow-up after customer re-engages.** No "customer replied since draft was generated" check in the Send chain. Awkward UX possible if operator delays approval and customer texts in the meantime. Mitigated by Telegram showing recent activity. Acceptable v1.
3. **(S2.2) Nomod failure leaves reply text implying a link.** When `should_send_payment: true` but Nomod returns an error, the card shows the operator a ⚠️ warning header, but the LLM's reply text already says "tap to lock it in" or similar. Operator must read the header before tapping Send.
4. **(S4.1) Customer Facts ↔ Mark Customer Msg can diverge** if one parallel arm errors while the other succeeds. Both use `continueRegularOutput`. Unlikely under normal conditions; the sweep tolerates stale state.

### 🛠 Improvements (optional, low priority)

1. **(S4.2) Killswitch-state audit.** 4 customers stuck in approval mode from the 2026-05-22 21:37 killswitch. Operator may want to review and `/auto` selectively.
2. **(S1.4) Optional Send-time freshness check.** Before WAHA send, check if `last_customer_message_at > followup_drafted_at`. If yes, post a confirmation card to operator ("customer replied since this draft was generated — send anyway?") before firing. ~5 nodes to add. Not blocking.
3. **(S3.3 mitigation alternative)** instead of guarding messages_sent_count, edit the inline keyboard to remove the [✅ Send] button after the first tap via `editMessageReplyMarkup`. UX-cleaner; ~3 nodes.

**Verdict:** 1 must-fix bug, 4 monitor-worthy risks, 3 optional polish items. v1 is shippable after fixing the Double-Send race (S3.3). Everything else is operationally acceptable.

---

**End of audit.** Read-only — no code changes made.
