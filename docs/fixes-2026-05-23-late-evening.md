# 2026-05-23 late evening — operator-reported bug fixes (round 2)

Four bugs reported in rapid succession after the earlier round of fixes. All four diagnosed from EC2 + n8n execution data, then fixed and deployed.

---

## 1. `/feedback` "classifier did not return valid JSON"

**Symptom (operator quote):** *"im giving prompt slash feedback and instructions to telegram bot but its saying: couldnt clasify that feedback:classified did not return valid JSON."*

**Root cause:** `classify_feedback()` called `run_hermes(...)` with `timeout=15`. Hermes under load takes 20–40 s; every recent classify attempt hit `subprocess.TimeoutExpired` (15 s) — five consecutive failures in the bridge log between 17:41:16 and 17:42:02. Function returned `None` → `_feedback` returned `"classifier did not return valid JSON"` → operator's feedback silently dropped.

**Fix (`hermes-bridge/server.py`):**
- Bump Hermes timeout: `timeout=15` → `timeout=60`.
- Add `_classify_feedback_fallback(text)` — deterministic pattern-based classifier (matches `customer <Name>`, scenario cues like `proposal`, `birthday`, `b2b`, `family group`).
- `classify_feedback` now **never returns None** for non-empty input. When Hermes fails/times out, the fallback kicks in and the operator's feedback is still captured.

**Verified:** Operator's exact failing text `"customer Saif paid, whenever a customer paid mark as payment done"` → classifier returns `CUSTOMER_NOTE`, `customer_name=Saif`, resolves to `253570691645643@lid`. `_fallback: true` in the response.

---

## 2. Edit button → "DRAFT EXPIRED" race

**Symptom (operator quote):** *"customer sends message, i see draft and press edit to give instructions to claud, immediatly draft disapears and we get message DRAFT EXPIRED."*

**Root cause:** In `phase-1b-telegram.json`, `Queue & Format` fanned out to **both** `Persist Draft` (Redis save) **and** `Send Draft to Telegram` (post the card) **in parallel**. The Telegram card landed in front of the operator before Persist Draft completed its `POST /queue {action: save}` to the bridge. Operator clicked Edit immediately → Telegram callback execution ran `Redis Draft Lookup` → bridge `_draft_get(...)` returned `null` → `Parse Callback` set `draft_found=false` → routed to `Notify Draft Expired`.

Confirmed by inspecting n8n execution `1046` (17:43:42, last node = `Notify Draft Expired`, callback data = `edit:1779558199933_pvywq`). Bridge GET for that same draft id NOW returns `found=true` (Persist Draft eventually completed after the click).

**Fix (`workflows/phase-1b-telegram.json`):**
- Connection rewire: `Queue & Format → Persist Draft → Send Draft to Telegram` (was: `Queue & Format → [Persist Draft, Send Draft to Telegram]` parallel).
- `Send Draft to Telegram` jsonBody now references `$('Queue & Format').item.json.telegram_payload` (was `$json.telegram_payload`) since `$json` is now `Persist Draft`'s response, not the card payload.

Result: Redis is guaranteed populated before the operator sees the buttons. Race window closed.

---

## 3. Pipeline review shows paid customer as WARM

**Symptom (operator quote):** *"pipelinereview should show when customer paid. for example its showing customer Saif, as WARM, not confirmed, and silent 40m, value add nudge could move it. its WRONG. customer already paid and booking is already confirmed."*

**Root cause:** No `CONFIRMED` label existed. Saif's `customer_facts.label` was `WARM`. /review had no way to express "this booking is won — stop suggesting nudges."

**Fix (`hermes-bridge/server.py`):**
- `LABELS` frozenset now includes `CONFIRMED`.
- `score_lead` short-circuits CONFIRMED with `return 5000` before any urgency/damping math (so they never fall into the paused tail).
- `render_review` adds a `✅ CONFIRMED — booked / paid` section. CONFIRMED rows get `ℹ️ Info` + `↩️ Unmark` buttons (no Draft-nudge button).
- `_label_eval` short-circuits when `previous_label == "CONFIRMED"` — terminal state, no auto-demotion on subsequent customer messages.
- Hourly sweep loop skips CONFIRMED entries (signal `confirmed_terminal`).
- `/label` allowed-labels error message lists `CONFIRMED`.

**Fix (`workflows/phase-1b-telegram.json`):**
- New node `Promote To Confirmed` — `Mark Sent` now fans to both `Edit Telegram (Send)` and `Promote To Confirmed`. The latter POSTs to `/label` with `label=CONFIRMED` when `draft.is_payment === true` (i.e., the operator just sent a Nomod payment-link draft). When `is_payment=false`, customer_id is empty and the bridge no-ops (with `onError: continueRegularOutput` so the workflow keeps going).

**Operator command path:** `/label saif CONFIRMED` (already wired; CONFIRMED is now accepted).

**Verified:** Saif → CONFIRMED via `/label`; `/review` now renders:
```
*✅ CONFIRMED — booked / paid* (1)
1. *Saif* — Sunseeker Satoshi 70 · Sun May 24 · msg #13
   ⏱ silent 23m  ·  new conversation
```
totals: `{CONFIRMED: 1, WARM: 3, ...}` (was WARM: 4 before).

---

## 4. Reply to nudge draft with text → "Nothing sent"

**Symptom (operator quote):** *"reply on nudge draft with edit also is not working, responding with text results in NOTHING sent."*

**Root cause:** Nudge cards (both from `/review nudge:` button → `Build Nudge Card` and from the separate `pipeline-hourly-sweep` workflow → `Build Followup Card`) were persisted to **Redis only** — never pushed into `phase-1b`'s `staticData.pendingQueue`. So:
- `Set Awaiting Edit` (Code node) called `data.pendingQueue.find(d => d.id === draftId)` → `undefined` → never set `status: 'awaiting_edit'`.
- `Route Text Action` (subsequent text-reply execution) called `queue.find(x => x.status === 'awaiting_edit')` → no match → fell through to the `'⚠️ Nothing sent. Commands: ...'` ack.

Confirmed by n8n execution `1050` (last node = `Ack No Pending`).

**Fix (`workflows/phase-1b-telegram.json`):**
- `Build Nudge Card`: now pushes `draftObj` into `data.pendingQueue` via `$getWorkflowStaticData('global')` before returning. Same pattern `Queue & Format` already uses for customer-message drafts.
- `Parse Callback`: when the draft is found via Redis fallback (`Redis Draft Lookup`) but NOT in `staticData`, **write through** — push it into `data.pendingQueue` so `Set Awaiting Edit`, `Mark Sent`, `Mark Skipped`, and the next-execution `Route Text Action` all see it. This also covers `pipeline-hourly-sweep` follow-up cards whose drafts live in a different workflow's staticData.

---

## Files touched
- `hermes-bridge/server.py` (+95 / −15)
- `workflows/phase-1b-telegram.json` (+38 / −10)
- `docs/fixes-2026-05-23-late-evening.md` (this file)

## Deploy
- `server.py` → EC2 `~/hermes-bridge/server.py`, `systemctl --user restart hermes-bridge`.
- Workflow → `docker exec n8n-n8n-1 n8n import:workflow --input=/tmp/wf.json`, then `docker restart n8n-n8n-1` (mandatory per runbook to refresh in-memory webhook registration).
- Telegram `getWebhookInfo`: `pending_update_count: 0`, `last_error_message: -`.

## Smoke tests passed
- `/feedback classify` for operator's exact failing text → CUSTOMER_NOTE Saif (`_fallback: true`).
- `/label saif CONFIRMED` → previous_label=WARM, new=CONFIRMED.
- `/review` → renders `✅ CONFIRMED — booked / paid` section with Saif.
- Bridge `/label BANANA` → rejected (`label must be one of ...CONFIRMED...`).
- Deployed workflow connections verified: `Queue & Format → Persist Draft → Send Draft to Telegram` (serial) and `Mark Sent → [Edit Telegram (Send), Promote To Confirmed]` (parallel).

## Not pushed to origin
Default-branch push remains blocked by the auto-mode classifier. Local commits ship; the EC2 deploys (bridge + n8n workflow) are independent and live.
