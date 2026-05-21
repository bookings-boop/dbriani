# Break-Condition Detection — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make autonomous mode flip a conversation to `approval` (instead of auto-sending) when the customer's message hits a break condition — discount request, human request, or clear negative sentiment.

**Architecture:** Three changes. (1) The bridge `/set-mode` endpoint records an optional `break_reason`. (2) The initial-draft prompt emits a structured `break_condition` field, carried onto the queued draft. (3) A `Break Check` gate at the autonomous-branch entry routes a flagged draft to `Flip To Approval` + `Break Alert` instead of into the autonomous branch.

**Tech Stack:** Python stdlib bridge (`hermes-bridge/server.py`), n8n workflow surgery (`scripts/n8n_deploy.py` + `build_*.py` scripts), Postgres. Spec: `docs/break-condition-detection-design.md`.

**Conventions (read first):**
- Workflow deploys go through `scripts/n8n_deploy.py` (`N8N.safe_put`) — never a raw PUT. Model new surgery scripts on `scripts/build_draft_guard.py` (idempotency guard, precondition checks, dry-run default, `--deploy` flag).
- Bridge deploys go through `scripts/deploy_bridge.py` (backup → upload → compile-check → restart → verify).
- Secret-scan every `git diff --cached` before committing.

---

## Task 1 — Bridge: `/set-mode` records `break_reason`

**Files:**
- Modify: `hermes-bridge/server.py` (`set_mode`, `_set_mode`)
- Deploy: `scripts/deploy_bridge.py`

- [ ] **Step 1: Modify `set_mode()`** — add the `break_reason` parameter and column.

Replace:
```python
def set_mode(customer_id, mode, activated_by):
    """Record a conversation-mode change. Returns (mode, None) or (None, err)."""
    if mode not in ("approval", "autonomous", "paused"):
        return None, "invalid mode (use approval|autonomous|paused)"
    sql = (
        "INSERT INTO conversation_modes "
        "(customer_id, mode, activated_at, activated_by) VALUES ("
        + ", ".join([_lit(customer_id), _lit(mode)])
        + ", now(), " + _lit(activated_by) + ")"
    )
```
With:
```python
def set_mode(customer_id, mode, activated_by, break_reason=None):
    """Record a conversation-mode change. Returns (mode, None) or (None, err).
    break_reason is recorded when an automatic break-condition triggered the
    change; it is NULL for a normal operator-driven mode change."""
    if mode not in ("approval", "autonomous", "paused"):
        return None, "invalid mode (use approval|autonomous|paused)"
    sql = (
        "INSERT INTO conversation_modes "
        "(customer_id, mode, activated_at, activated_by, break_reason) VALUES ("
        + ", ".join([_lit(customer_id), _lit(mode)])
        + ", now(), " + _lit(activated_by) + ", " + _lit(break_reason) + ")"
    )
```
(`_lit(None)` renders `NULL`, so existing callers that omit `break_reason` are unaffected.)

- [ ] **Step 2: Modify the `_set_mode` handler** — read `break_reason` from the payload.

In `_set_mode`, after the line `by = (payload.get("activated_by") or "operator").strip()` add:
```python
        break_reason = (payload.get("break_reason") or "").strip() or None
```
And change the call `m, err = set_mode(cid, mode, by)` to:
```python
        m, err = set_mode(cid, mode, by, break_reason)
```

- [ ] **Step 3: Compile-check.**

Run: `python3 -m py_compile hermes-bridge/server.py`
Expected: no output (exit 0).

- [ ] **Step 4: Deploy.**

Run: `python3 scripts/deploy_bridge.py`
Expected: `VERIFY: service=active /health=HTTP 200 /improve=HTTP 401`.

- [ ] **Step 5: Verify the column is written.**

Run (on the box, with `BRIDGE_TOKEN` sourced from `~/hermes-bridge/.env`):
```bash
curl -s -X POST -H "X-Bridge-Token: $BRIDGE_TOKEN" -H 'Content-Type: application/json' \
  -d '{"customer_id":"__brk_t1__","mode":"approval","activated_by":"plan_test","break_reason":"discount_request: test"}' \
  http://localhost:8788/set-mode
docker exec n8n-postgres-1 psql -U hermes_rw -d n8n -tA \
  -c "SELECT mode||' | '||coalesce(break_reason,'NULL') FROM conversation_modes WHERE customer_id='__brk_t1__'"
```
Expected: `{"ok": true, ...}` then `approval | discount_request: test`.
Cleanup: `docker exec n8n-postgres-1 psql -U n8n -d n8n -c "DELETE FROM conversation_modes WHERE customer_id='__brk_t1__'; DELETE FROM autonomous_sends WHERE customer_id='__brk_t1__'"`

- [ ] **Step 6: Commit.**
```bash
git add hermes-bridge/server.py
git commit -m "Bridge: /set-mode records optional break_reason"
```

---

## Task 2 — Drafting: emit and carry `break_condition`

**Files:**
- Create: `scripts/build_break_detection_prompt.py` (workflow surgery — model on `scripts/build_draft_guard.py`)
- Modifies live workflow nodes: `Build Prompt` (Set), `Parse Response` (code), `Queue & Format` (code)

- [ ] **Step 1: Inspect the three target nodes.**

Run:
```bash
python3 - <<'PY'
import json
wf=json.load(open('workflows/phase-1b-telegram.json'))
n={x['name']:x for x in wf['nodes']}
print("--- Build Prompt systemPrompt OUTPUT FORMAT section ---")
sp=n['Build Prompt']['parameters']['assignments']['assignments'][0]['value']
i=sp.find('OUTPUT FORMAT'); print(sp[i:i+900])
print("\n--- Parse Response jsCode ---"); print(n['Parse Response']['parameters']['jsCode'])
print("\n--- Queue & Format jsCode ---"); print(n['Queue & Format']['parameters']['jsCode'])
PY
```
Note: how the draft JSON's fields are listed in the prompt's OUTPUT FORMAT section; how `Parse Response` turns Claude's JSON into its output object; how `Queue & Format` builds the `pendingQueue` draft entry (the object with `id`, `customer_phone`, `draft_text`, `messages`, …).

- [ ] **Step 2: Write `scripts/build_break_detection_prompt.py`.**

The script (idempotency guard keyed on the marker string `BREAK-CONDITION CHECK`; dry-run default; `--deploy` → `N8N.safe_put`, tag `BREAKPROMPT`) makes three edits:

**2a — `Build Prompt`:** append this block to the `systemPrompt` value, immediately after the existing OUTPUT FORMAT section:
```
BREAK-CONDITION CHECK — also include a "break_condition" field in your JSON.
Judge ONLY the customer's newest message. Set "hit": true only if it clearly
matches one of:
  - "discount_request": asks for a discount or lower price, says "best price",
    says it is too expensive, or is haggling on price.
  - "human_request": asks to speak to a person / human / manager / "real
    person", or to be transferred off the bot.
  - "negative_sentiment": the customer is clearly upset, angry, frustrated, or
    complaining — NOT mild hesitation or an ordinary sales objection.
A normal price question ("how much is the 80ft on Saturday?") is NOT a break.
Format when something matches:
  "break_condition": {"hit": true, "reason": "discount_request",
                      "detail": "<one short line>"}
Format when nothing matches:
  "break_condition": {"hit": false}
```

**2b — `Parse Response`:** in the object it returns for the draft, add `break_condition`, defaulting safely:
```javascript
break_condition: (parsed && parsed.break_condition && typeof parsed.break_condition.hit === 'boolean')
  ? parsed.break_condition : { hit: false },
```
(Use the name the node already uses for the parsed-JSON variable in place of `parsed`.)

**2c — `Queue & Format`:** the draft object pushed to `pendingQueue` must carry the field through — add to that object:
```javascript
break_condition: item.break_condition || { hit: false },
```
(Use the node's existing variable for the per-item input in place of `item`.)

- [ ] **Step 3: Dry-run.**

Run: `python3 scripts/build_break_detection_prompt.py`
Expected: reports the 3 node edits, `DRY RUN — nothing deployed`.

- [ ] **Step 4: Deploy.**

Run: `python3 scripts/build_break_detection_prompt.py --deploy`
Expected: `safe_put` succeeds, `pendingQueue` count preserved before==after.

- [ ] **Step 5: Verify the prompt change is live.**

Run:
```bash
python3 - <<'PY'
import sys; sys.path.insert(0,'scripts')
from n8n_deploy import N8N
wf=N8N().get_workflow()
sp=next(x for x in wf['nodes'] if x['name']=='Build Prompt')['parameters']['assignments']['assignments'][0]['value']
print("break-condition block present:", 'BREAK-CONDITION CHECK' in sp)
PY
```
Expected: `break-condition block present: True`. (Behavioural confirmation that the model actually emits the field is covered in Task 4.)

- [ ] **Step 6: Commit.**
```bash
git add scripts/build_break_detection_prompt.py
git commit -m "Drafting: emit and carry a break_condition field"
```

---

## Task 3 — Workflow: the break-gate sub-branch

**Files:**
- Create: `scripts/build_break_gate.py` (workflow surgery — model on `scripts/build_fr4_error_alerts.py`)
- Modifies live workflow: adds **4** nodes, rewires the `Save Telegram MsgID → Auto Prep` edge

> Deviation from the spec: the design estimated 3 new nodes. Planning showed an IF node cannot run the `pendingQueue.find()` lookup in its condition, so a `Find Break` **code** node precedes the `Break Check` IF — 4 nodes total (87 → 91).

**Current wiring:** `Save Telegram MsgID` out[0] → `[Improve Wait, Auto Prep]`.
**Target wiring:** `Save Telegram MsgID` out[0] → `[Improve Wait, Break Check]`; `Break Check` true→`Auto Prep`, false→`Flip To Approval`→`Break Alert`.
(Note the IF semantics: condition = "break hit". true output → break path, false output → `Auto Prep`. Wire accordingly.)

- [ ] **Step 1: Inspect the entry nodes.**

Run:
```bash
python3 - <<'PY'
import json
wf=json.load(open('workflows/phase-1b-telegram.json'))
n={x['name']:x for x in wf['nodes']}
for nm in ['Auto Prep','Save Telegram MsgID']:
    print(f"--- {nm} ---"); print(json.dumps(n[nm].get('parameters',{}))[:500])
PY
```
Note how `Auto Prep` reads the draft (`$('Save Telegram MsgID').item.json.draft_id`, then `pendingQueue.find`). `Break Check`'s preceding `Find Break` node uses the same lookup.

- [ ] **Step 2: Write `scripts/build_break_gate.py`** — adds 4 nodes (a `Find Break` code node + a `Break Check` IF node + `Flip To Approval` + `Break Alert` httpRequest nodes) and rewires.

`Find Break` (code) — reads the draft and surfaces the break fields onto the item:
```javascript
// break-gate: surface the queued draft's break_condition for the IF + alert
const sid = $('Save Telegram MsgID').item.json;
const data = $getWorkflowStaticData('global');
const d = (data.pendingQueue || []).find(x => x.id === sid.draft_id) || {};
const bc = d.break_condition || { hit: false };
return [{ json: {
  draft_id: sid.draft_id,
  customer_id: d.customer_phone || '',
  break_hit: bc.hit === true,
  break_reason: bc.reason || 'unknown',
  break_detail: bc.detail || ''
} }];
```

`Break Check` (IF, typeVersion 2.2) — condition: boolean `={{ $json.break_hit }}` is true. (Reuse the IF-node spec from `scripts/build_draft_guard.py`'s `Draft Exists?`.)

`Flip To Approval` (httpRequest, typeVersion 4.2) — POST `http://172.18.0.1:8788/set-mode`, `httpHeaderAuth` credential `"Hermes Bridge"` (id `IgIcvPoibuAayVDx`), `sendBody`/`specifyBody:json`, body:
```
={ "customer_id": {{ JSON.stringify($json.customer_id) }}, "mode": "approval",
   "activated_by": "break_detection",
   "break_reason": {{ JSON.stringify($json.break_reason + ': ' + $json.break_detail) }} }
```
`onError: continueRegularOutput`.

`Break Alert` (httpRequest, typeVersion 4.2) — POST `=https://api.telegram.org/bot{{ $env.TELEGRAM_BOT_TOKEN }}/sendMessage`, `sendBody`/`specifyBody:json`, body:
```
={ "chat_id": 5532831477, "text": {{ JSON.stringify("⏸️ AUTONOMOUS PAUSED — " +
   $('Find Break').item.json.customer_id + " — " + $('Find Break').item.json.break_reason +
   " — \"" + $('Find Break').item.json.break_detail + "\"\n\nConversation set back to approval; the draft is waiting for you.") }} }
```
`onError: continueRegularOutput`.

Connections: `Save Telegram MsgID` out[0] keeps `Improve Wait`, replaces `Auto Prep` with `Find Break`; `Find Break`→`Break Check`; `Break Check` out[0] (true)→`Flip To Approval`; `Break Check` out[1] (false)→`Auto Prep`; `Flip To Approval`→`Break Alert`.

- [ ] **Step 3: Dry-run.**

Run: `python3 scripts/build_break_gate.py`
Expected: reports +4 nodes and the rewire, `DRY RUN`.

- [ ] **Step 4: Deploy.**

Run: `python3 scripts/build_break_gate.py --deploy`
Expected: `safe_put` succeeds; node count +4; `pendingQueue` preserved.

- [ ] **Step 5: Commit.**
```bash
git add scripts/build_break_gate.py
git commit -m "Workflow: break-gate routes flagged drafts out of autonomous send"
```

---

## Task 4 — Verification

**Files:** Create `scripts/test_break_gate.py` (Node-harness test of the `Find Break` jsCode — model on the harness pattern used for the FR-4 alert builders).

- [ ] **Step 1: Unit-test `Find Break` logic.** Run the node's jsCode under Node.js with three mock drafts in `pendingQueue`: `break_condition.hit=true`, `hit=false`, and `break_condition` absent. Assert `break_hit` is `true`, `false`, `false` respectively.

- [ ] **Step 2: Behavioural test (folds into H7–H9).** With a test conversation in autonomous mode, the test phone sends a discount request. Expect: no auto-send; `conversation_modes` gains an `approval` row with `break_reason` like `discount_request: …`; a Telegram "⏸️ AUTONOMOUS PAUSED" alert. Repeat for a human request and an angry message.

- [ ] **Step 3: Commit** the test script.
```bash
git add scripts/test_break_gate.py
git commit -m "Add break-gate verification test"
```

---

## Notes for the implementer

- Tasks 2 and 3 each begin with a live inspection step because n8n node internals (the parsed-JSON variable name, the queue-entry object) must be read from the current workflow — they are stable but not reproduced here verbatim.
- Task ordering is safe in any order: until Task 2 ships, `break_condition` is absent and `Find Break` yields `break_hit:false` (autonomous proceeds unchanged) — the design's fail-open default.
- After all tasks: re-run `scripts`-based sync of `workflows/phase-1b-telegram.json` so the committed JSON matches the new node count.
