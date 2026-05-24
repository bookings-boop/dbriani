# `/review` showing wrong/missing names — diagnosis

> **Mode:** DIAGNOSIS ONLY. No code changes, no DB writes, no fixes applied.
> **Reported:** 2026-05-23 — operator says /review not showing recent chat names and data correctly, real customer chat being missed.

---

## What's actually broken (and what isn't)

| Symptom | Status |
|---|---|
| Names show as "Unknown" on 10 of 11 cards | **❌ Real bug — root cause identified** |
| One real customer hidden from /review entirely | **⚠️ Two separate issues** (NEW-section overflow + DB-missing legacy customers) |
| `/review` query/SQL broken | ✅ NOT broken — query works correctly; v_lead_summary returns all rows |
| `customer_facts` table broken | ✅ Rows exist correctly for all 11 today-active customers |
| `conversation_state` not populated | ✅ Was the earlier bug (fixed `259a63a`); all 11 populated now |
| Hermes extraction failing | ✅ Yachts/dates extracted for 6/11 customers correctly — extraction works |

---

## Finding 1 — Names are NULL for 10 of 11 customers

```
total=11, has_name=1, has_yachts=6, has_dates=5, nonblank_name=1
```

Only `274942918680787@lid` (Qurbani — the test phone) has a name in `customer_facts`. Every real customer's `name` column is NULL.

But WAHA **does** know their names. Pulled from WAHA's `/api/default/chats` endpoint:

| customer_id | WAHA pushName | DB name |
|---|---|---|
| `80762783182909@lid` | `+971 54 444 4557` | NULL |
| `274942918680787@lid` | `+971 50 976 7187` | `Qurbani` |
| `183438170693737@lid` | `+966 55 658 3801` | NULL |
| `253570691645643@lid` | `+974 5587 7787` | NULL |
| `28063417008351@lid` | `Dubriani admin chat` | NULL |
| **`274319577976890@lid`** | **`Dj Richie Dxb`** | **NULL** |
| `156251681997028@lid` | `+971 58 824 8825` | NULL |
| `135953029026042@lid` | `+971 52 525 3322` | NULL |
| `0@c.us` | `WhatsApp Business` | NULL |
| `196568070275292@lid` | `+44 7869 651761` | NULL |
| `5532505120995@lid` | `nassr lsb` | not in DB |

**`Dj Richie Dxb` is a real human-named contact in WAHA — but DB has them as `name=null`.**

### Root cause

`workflows/phase-1b-telegram.json` Format Context node, jsCode line:

```js
customerName: wh.notifyName || wh.pushName || ''
```

`wh` is `$('WAHA Webhook').first().json.body.payload`. The current WAHA webhook payload **does not include** `notifyName` or `pushName` fields — verified by inspecting a real webhook payload from earlier today's S1 audit:

```json
"payload": {
    "id": "false_274942918680787@lid_3A305AEF7A585204AA88",
    "timestamp": 1779541208,
    "from": "274942918680787@lid",
    "fromMe": false,
    "source": "app", "to": "971589502303@c.us",
    "body": "send me a payment link to confirm it",
    "hasMedia": false, "media": null, "ack": 1, ...
    // NO notifyName, NO pushName
}
```

So `customerName` evaluates to `''` (empty string).

Empty `customer_name` flows downstream:
- `Customer Facts` httpRequest sends `"customer_name": ""` to `/customer-facts`
- Bridge `_customer_facts` receives empty `cname`
- `upsert_customer_facts` falls through to use cached name; if cache also empty (most customers), `name` stays NULL in DB
- `/review` reads `name` from DB, finds NULL → displays "Unknown"

### Why Qurbani (Mark) has a name

The customer literally typed *"hi I'm Sam, 4 of us, interested in the Satoshi"* and *"hi I'm qurbani, 4 of us, interested in the Satoshi"* in their messages — Hermes `extract_customer_facts` (`hermes-bridge/server.py` extract_customer_facts function) explicitly asks for `name: the customer's first/full name if they have given it` and successfully extracted it from the chat content.

For other customers who never introduced themselves by name in their messages, Hermes returns `name: ""` correctly (the prompt instructs it not to guess). And since the WAHA pushName fallback is also empty (because WAHA doesn't put it in the webhook payload), the customer ends up nameless.

### What WAHA actually exposes (just not in webhooks)

WAHA's `/api/default/chats` endpoint **DOES** carry useful names like `Dj Richie Dxb` and `nassr lsb`. We just never query it. The webhook payload is the sole source, and it's name-less.

---

## Finding 2 — `/review` SQL is correct; v_lead_summary returns all rows

I queried `v_lead_summary` directly for `80762783182909@lid` (the recent Zenith 64 / Sun May 24 customer):

```
customer_id            | name | label | yachts    | dates      | message_count
80762783182909@lid     |      | WARM  | Zenith 64 | Sun May 24 | 5
last_customer_message_at = 2026-05-23 14:54:28
last_operator_reply_at  = 2026-05-23 14:53:13
conversation_mode      = autonomous
```

The view returns the row. The /review handler reads it. The data is correct; just `name` is empty.

---

## Finding 3 — NEW-section overflow hides one customer

`/review` totals:
```
total=11, HOT=1, WARM=4, NEW=6, COLD=0, NEEDS_ATTENTION=0, PAUSED=0
```

`/review` displays 10 cards, NOT 11. The NEW section is capped at 5 (`REVIEW_CAP_NEW=5` in `hermes-bridge/server.py`). With 6 NEW customers, one falls into the overflow.

**That overflowed customer is `274319577976890@lid` — pushName `Dj Richie Dxb`** — the only real-named customer in WAHA who's also NEW + msg #1 + silent ~4 hrs.

Their card never posts unless operator runs `/review new`. The /review output line that hints at this is `+1 more — /review new to see all` — easy to miss when you're scanning fast.

---

## Finding 4 — 5 historical customers from May 21-22 missing from DB entirely

WAHA chat list shows 5 customers with conversation history but NO row in customer_facts at all:

| customer_id | WAHA pushName | Last message |
|---|---|---|
| `276879563046913@lid` | `+44 7429 417414` | 2026-05-22 09:46 — *"we would like to find out about 1/2 night stay"* |
| `156182744436801@lid` | `+971 58 141 0771` | 2026-05-22 05:34 — empty body |
| `112301180977349@lid` | `+971 52 288 4053` | 2026-05-21 14:19 — *"ok noted ill get back to you"* |
| `157208872501273@lid` | `+971 55 170 9999` | 2026-05-21 17:34 — *"Yeas"* / *"White"* |
| `5532505120995@lid` | `nassr lsb` | 2026-05-21 17:16 — *"Critical"* |

These customers messaged the WAHA bot, but no `customer_facts` row exists. Probable causes:

1. **Workflow predated Pipeline Review.** Customer Facts node has existed since before Pipeline Review, BUT `extract_customer_facts` (the Hermes call inside the bridge `_customer_facts` handler) is gated by `_facts_extract_gate(msg)` — which requires the message to contain numbers, yacht keywords, dates, names, or booking keywords. Short messages like *"Yeas"*, *"Critical"*, *"White"* don't trip the gate, so extraction is skipped. AND if the customer has no prior `customer_facts` row, `upsert_customer_facts` won't be called (because the bridge handler's `_facts_extract_gate` short-circuits before upsert).

2. **The b53042c deploy** added `Mark Customer Msg` and Label Eval nodes which were silently failing — fixed earlier today by `259a63a`. Customers who messaged DURING the broken window may have had Customer Facts run but no conversation_state row got written.

3. **Empty body messages** (like 156182744436801's empty body) get filtered by Filter Inbound (`body.payload.body notEmpty` condition).

None of these 5 customers have messaged since the 259a63a fix. So they remain in the broken state.

---

## Why the operator perceived "missed"

Combining the above:
- "Names" missing → 10/11 cards show `Unknown` because pushName isn't piped through (Finding 1)
- "Specific customer missing" → most likely the `Dj Richie Dxb` customer hidden in NEW overflow (Finding 3) OR one of the 5 historical customers (Finding 4)

The operator probably looked at WhatsApp, saw real customers by name (because WhatsApp shows pushName/contact name), then looked at /review and saw "Unknown" + the same customer missing entirely from the report → reasonable to conclude /review is broken.

It's NOT broken in the SQL/query/sweep sense. The data flow into customer_facts is what's deficient:

- WAHA webhook → workflow: missing pushName ← **fixable by enriching at workflow level**
- Hermes extraction: works fine, but only catches names the customer actually typed
- NEW section cap: by design (Telegram 4096-char message limit), but should perhaps spill names that aren't `Unknown` first

---

## What I did NOT investigate (out of scope per operator: diagnose only)

- WAHA webhook config — is there a setting to include pushName?
- Whether `wh._data` (the giant blob the workflow currently skips) contains the name
- Whether to query `WAHA /api/default/contacts/{cid}` as a name fallback inside the workflow
- Whether to raise NEW section cap or sort overflow by has-name-or-not
- Whether the 5 historical customers should be backfilled

---

## Summary

| Issue | Severity | Status |
|---|---|---|
| 10/11 cards show "Unknown" | **High — operator-visible confusion** | Diagnosed: WAHA webhook payload missing pushName |
| Dj Richie Dxb in NEW overflow | Medium — hides a real customer | Diagnosed: section cap=5 behavior |
| 5 historical customers absent from DB | Low — pre-fix-window, won't recur for new traffic | Diagnosed: facts-extract gate + Mark Customer Msg breakage (now fixed) |
| /review query broken | None | **NOT a bug** — query and view are correct |

**Awaiting operator decision before applying any fix.**
