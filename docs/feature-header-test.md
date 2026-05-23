# Customer-Header Feature — Test Results

> **Status:** all listed tests PASS as of 2026-05-23.
> Companion doc: `docs/feature-header-plan.md`. Feature commits begin at
> `ff0c81e` (gate + builder) through `4d5542a` (close-out).

The customer-header feature surfaces the operator's running facts about a
customer (name, dates, yacht, party size, message count) at the top of every
approval-mode draft card. It's driven by the `Customer Facts` httpRequest
node calling `POST /customer-facts` on the bridge, which maintains the
`customer_facts` Postgres table and returns a pre-rendered header string for
`Queue & Format` to prepend.

---

## Tests 1–4 (from the initial close-out, 2026-05-22 — execs 663/665/666/667)

| # | Scenario | Pass criterion | Result |
|---|---|---|---|
| 1 | First message — *"hi, im mark, 6 of us, looking at Satoshi for Dec 14"* | extraction runs, message_count=1, full header on card | ✅ exec 663 — `extracted=True msg#1`; card shows 👤 Mark / 📅 Sat Dec 14 / 🛥️ Looking at: Thunder, Pershing 82, Sunseeker Satoshi 70 / 👥 6 guests / 🔢 #1 + 30-char divider |
| 2 | Skip-gate — *"sounds good thanks"* (no numbers / yacht keywords / date / name / booking words) | extraction skipped, message_count increments, facts preserved | ✅ exec 665 — `extracted=False msg#2`, facts unchanged |
| 3 | `/lead` regression (outbound first contact) | lead card posts, **no header**, no crash | ✅ exec 666 — lead card posted, `Customer Facts` not in chain, no regression |
| 4 | Refine regression (operator edits a draft) | refine status:success, no extraction increment | ✅ exec 667 — refine ran clean |

---

## Test 5 — bridge-down graceful degradation (2026-05-23)

**Purpose:** verify the workflow does NOT crash and the draft still posts when
the bridge is unreachable. Expected: minimal header in place of the rich
Customer Facts block; no fatal node errors.

**Method:**
1. `systemctl --user stop hermes-bridge` → confirmed `inactive`, `/health`
   unreachable (HTTP 000 / connection refused).
2. Operator sent one WhatsApp message from the test phone.
3. Trace the resulting execution end-to-end.
4. `systemctl --user start hermes-bridge` → confirmed `active`, `/health` 200.
5. Operator sent a second message; trace it.

### Test 5a — bridge down (exec 795, *"good morning"*)

The chain executed in this order; `[err→cont]` marks nodes that errored on
the bridge call and continued via `onError: continueRegularOutput`.

| Node | Outcome |
|---|---|
| `Debounce Buffer` | `[err→cont]` ECONNREFUSED `172.18.0.1:8788` |
| `Buffer Message` | output `{ phone, token:"", degraded:true }` — fail-open path fired |
| `Debounce Wait` (30 s) | ran |
| `Debounce Flush` | `[err→cont]` ECONNREFUSED |
| `Flush Check` | fell open (`bm.degraded=true`) → processed the message anyway |
| `Get Chat History` (WAHA) | ran fine |
| `Format Context` | ran fine |
| `Fetch Behavioral Context` | `[err→cont]` ECONNREFUSED — `formatted` undefined → Claude AI got just `systemPrompt`, empty behavioural block |
| `Build Prompt` | ran |
| `Claude AI` (Anthropic, no bridge dep) | ran — draft generated |
| `Parse Response` | ran |
| **`Customer Facts`** | `[err→cont]` ECONNREFUSED — `$json.customer_header` absent |
| `Queue & Format` | `try { customerHeader = $('Customer Facts').item.json.customer_header || '' }` resolved to `''` → fell through to the **minimal header** branch (`'📜 History: N prior messages'` only — no rich block) |
| `Send Draft to Telegram` | **✅ message_id 265** — draft posted |
| `Save Telegram MsgID` | ran |
| `Auto Gate` | `[err→cont]` `/autosend-check` ECONNREFUSED |
| `Auto Gate Failed` → `Build Alert` → `Alert Zayn` | informational `🔴 AUTONOMOUS-SEND FAILED` posted to admin chat (autonomous branch correctly reports it can't reach the bridge) |

**Result:** exec **status = `success`**. No fatal crash. Card posted with the
**minimal header** as designed:

```
📩 from 274942918680787@lid
📜 History: 14 prior messages

They said:
"good morning"
---
💬 DRAFT:
good morning Mark! 🌹 hope you're doing well — …
```

PASS — every bridge-dependent node failed cleanly, every fail-open path fired,
the draft still posted, and the operator got an informational alert about the
autonomous branch (an expected side-effect when the bridge is unavailable).

### Test 5b — bridge up (exec 796, *"good afternoon"*)

After `systemctl start hermes-bridge` + confirming `/health` HTTP 200, a fresh
customer message produced:

- `Customer Facts: { ok:true, message_count:24, customer_header:"<full>" }`
- `Fetch Behavioral Context: { ok:true, globals:["Never offer phone calls in any draft or communication."], … }` — bonus confirmation that the `/feedback` rule saved earlier (`behavior_rules.id=8`) is being loaded into drafts.
- Card text contains all 5 rich-header markers: `Interested in:`, `Looking at:`, `Party size:`, `Message #`, the 30-char divider.

```
📩 from 274942918680787@lid
👤 Mark
📅 Interested in: this Saturday
🛥️ Looking at: Satoshi 70
👥 Party size: 2 guests
🔢 Message #24 in conversation
──────────────────────────────

They said:
"good afternoon"
---
💬 DRAFT:
good afternoon Mark! 😊 hope you're having a great day — the Satoshi is still held for you this Saturday. ready to lock it in? 🌹
```

PASS — full header restored on the very next message after the bridge came
back up. No state corruption from the down period.

---

## Summary

| Test | Pass |
|---|---|
| 1 — first message, full header | ✅ |
| 2 — skip-gate preserves facts | ✅ |
| 3 — `/lead` regression | ✅ |
| 4 — refine regression | ✅ |
| 5a — bridge down, minimal header, no crash | ✅ |
| 5b — bridge up, full header returns | ✅ |

Feature is production-ready and fail-safe. The header path is non-blocking:
every bridge dependency along it uses `onError: continueRegularOutput` and
the `Queue & Format` formatter has a clean empty-header fallback.
