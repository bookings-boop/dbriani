# Bug Diagnosis — 2026-05-22

> **READ-ONLY diagnosis.** No fixes applied. Evidence-based root-cause analysis
> of three reported bugs in the live "Dubriani Phase 1B" workflow
> (`azPIy9OcDwiPV5uY`, 98 nodes) and the Hermes Bridge.
> Method: `superpowers:systematic-debugging` — root cause before fixes.

**Scope investigated:** live workflow JSON, `hermes-bridge/server.py`, n8n
execution history (execs 634–683), bridge `journalctl` logs, Redis
(`n8n-redis-1`) and Postgres (`conversation_modes`, `autonomous_sends`).

---

## Summary

| Bug | Verdict | Root cause | Shares cause? |
|-----|---------|-----------|----------------|
| **1 — debounce produces multiple drafts** | **CONFIRMED** (reproduced in exec record) | `$getWorkflowStaticData` is execution-isolated — concurrent message executions never see each other's buffer/token writes | Same *class* as the already-fixed BUG-1 / Mark-Auto-Sent-residual (staticData misuse); the debounce is the **last un-migrated instance** |
| **2 — AUTO mode set, no auto-send** | **NOT REPRODUCED** — no defect found | None identified. The autonomous chain is intact; no customer message ever reached the workflow after AUTO was activated | n/a |
| **3 — improver skips autonomous drafts** | **CONFIRMED** (visible in node code) | `Check Pending` deliberately `return []`s when the draft is armed (added by the `1181c5b` "Mark Auto Sent residual" fix), **plus** `Arm Autosend` snapshots the draft text before the improver could run | No — independent of BUG 1 |

**Do the bugs share a root cause?** No single shared cause. BUG 1 and the
*previously fixed* autonomous bugs share a **theme** — `$getWorkflowStaticData`
cannot coordinate across concurrent executions or Wait-node resumes. BUG 3 is a
deliberate gate, not a staticData fault. BUG 2 has no identified cause.

---

## BUG 1 — FR-3 debounce produces multiple drafts

### Reported
Rapid-fire customer messages each produce their own draft; the FR-3 debounce
that should collapse them into one is not working.

### Reproduction — found in the live execution record
`Debounce Wait` is configured for **30 seconds**. Two real customer messages
arrived **7 seconds apart**, well inside that window:

| Exec | Started (UTC) | Message | Outcome |
|------|---------------|---------|---------|
| 672 | 11:21:29 | "where all cna we go" | ran full chain → `Queue & Format` → `Send Draft to Telegram` → **draft posted** |
| 673 | 11:21:36 | "is food included" | ran full chain → `Queue & Format` → `Send Draft to Telegram` → **draft posted** |

Two messages, 7 s apart, inside a 30 s debounce → **two separate drafts.** The
debounce collapsed nothing. (It also did not *combine* the texts — each draft
carried only its own message.) This is a direct reproduction.

### Root cause — evidence

The debounce is two code nodes around the `Debounce Wait` Wait node:

`Buffer Message`:
```js
const data = $getWorkflowStaticData('global');
if (!data.inboundBuffer) data.inboundBuffer = {};
if (!data.lastSeq) data.lastSeq = {};
data.inboundBuffer[phone].push({ id, text, ts });
const token = Date.now() + '_' + Math.random()...;
data.lastSeq[phone] = token;            // "I am the latest"
```
`Flush Check` (after the 30 s wait):
```js
const data = $getWorkflowStaticData('global');
if (!data.lastSeq || data.lastSeq[phone] !== token) return [];  // a newer msg won — abort
// else: flush data.inboundBuffer[phone], produce ONE draft
```

The design intends: the *last* message's token wins; earlier executions see a
newer token and abort. **This cannot work**, because every inbound message
starts a **separate n8n execution**, and `$getWorkflowStaticData('global')` is
**loaded per-execution and only persisted when that execution finishes** — it is
**not shared live across concurrent executions or across a Wait-node pause**
(confirmed root cause from yesterday's code review; the same defect was already
fixed for the autonomous-send path — git `5bc5c0a`, `5cb34e2`).

Trace for execs 672/673:
1. Exec 672 loads its own staticData snapshot, writes `inboundBuffer[phone]=["where…"]`, `lastSeq[phone]=tokenA`, enters the 30 s wait.
2. Exec 673 starts with its **own** snapshot — it never sees 672's write — writes `inboundBuffer[phone]=["is food…"]`, `lastSeq[phone]=tokenB`, enters the wait.
3. Exec 672 resumes `Flush Check`: in *its* snapshot `lastSeq[phone]` is still `tokenA` → `tokenA === tokenA` → **672 thinks it is the latest** → flushes → draft #1.
4. Exec 673 resumes `Flush Check`: in *its* snapshot `lastSeq[phone] === tokenB` → **673 also thinks it is the latest** → flushes → draft #2.

Every execution always sees **its own** token as the latest, because it never
observes another execution's write. The "latest-token-wins" comparison is
structurally inert. N messages → N drafts.

### Redis status
`n8n-redis-1` is **up** (19 h uptime), `PING` → `PONG`, `DBSIZE` → **0**
(idle, no keys). No `debounce:*` keys exist — the debounce has never used Redis.

### Proposed fix — scope & risk (DO NOT FIX — proposal only)
**A staticData-only patch is not possible** — staticData is the wrong
primitive; cross-execution coordination needs shared atomic state. The fix is a
**Redis-backed rebuild of the debounce**, mirroring the BUG-1 autosend fix
(`/autosend-state`):

- New bridge endpoint (e.g. `POST /debounce`, actions `buffer` / `flush-check`)
  using the existing `_redis()` helper:
  - `buffer`: `RPUSH debounce:buf:<phone> <msg>` + `SET debounce:seq:<phone> <token>` (atomic, shared).
  - `flush-check`: return whether the caller's token still equals `debounce:seq:<phone>`; if so, atomically return + `DEL` the buffer list.
- Rewrite `Buffer Message` and `Flush Check` to call that endpoint instead of `$getWorkflowStaticData`.
- **Scope:** moderate — 1 bridge endpoint + 2 node rewrites + deploy. Comparable to the autosend Redis migration.
- **Risk:** moderate — this is on the **inbound path of every customer message**; a defect here blocks *all* drafts. Mitigate by failing **open** (if the bridge call errors, process the message rather than drop it) and testing with rapid-fire messages before sign-off.

---

## BUG 2 — AUTO activates autonomous mode but no auto-send fires

### Reported
Clicking the 🤖 AUTO button activates autonomous mode, but a subsequent
customer message does not auto-send.

### Verdict: NOT REPRODUCED — no defect found in the workflow or bridge

The autonomous-send pipeline is **intact**. The execution record contains **no
case** of a customer message being processed in autonomous mode that failed to
auto-send. Evidence below.

### The autonomous send chain (mapped)
```
Save Telegram MsgID ─┬─> Improve Wait ... (improver branch — see BUG 3)
                     └─> Find Break -> Break Check ─#1(no break)─> Auto Prep
   -> Auto Gate (POST /autosend-check, commit:false)
   -> Auto Is Autonomous  (stop unless bridge says mode=autonomous)
   -> Arm Autosend (Redis SET) -> Render Auto Card -> Auto Wait (60–300 s)
   -> Get Autosend (Redis GET) -> Auto Decide (stop unless armed)
   -> Auto Commit (POST /autosend-check, commit:true)
   -> Auto Send Gate (stop unless caps cleared) -> Auto Send WAHA -> Mark Auto Sent
```

### Evidence

**1. The chain worked, end-to-end, at the H8 baseline.** Exec **646, 647, 648**
(H7-H9 testing, 08:20–08:28 UTC) each ran the full chain through `Auto Send
WAHA` → `Mark Auto Sent` → `Edit Auto-Sent Card`. Bridge log confirms:
`autosend-check COMMIT … auto_send=True reason='ok'` ×3. Those are the 3
`kind='auto'` rows in `autonomous_sends`.

**2. The chain's code and topology are UNCHANGED since exec 648.** The only
workflow changes after the H8 sign-off were: `/caps` wiring (`7a71511`), the
Mark-Auto-Sent residual (`1181c5b` — improver branch only), and the
customer-header feature (`9effd76` — `Parse Response → Customer Facts → Queue &
Format`). **None of them touch** `Find Break → Break Check → Auto Prep → Auto
Gate → Auto Is Autonomous → Arm Autosend → Render Auto Card → Auto Wait → Get
Autosend → Auto Decide → Auto Commit → Auto Send Gate → Auto Send WAHA`.

**3. The front half is verified intact TODAY (post-feature-header).** Execs
**672 / 673** (11:21 UTC) ran `Find Break → Break Check → Auto Prep → Auto
Gate → Auto Is Autonomous`. `Auto Prep` correctly resolved the draft and emitted
`customer_id: 274942918680787@lid`; `Auto Gate` called the bridge and got a
coherent response. They stopped at `Auto Is Autonomous` **correctly** — because
`Auto Gate` returned `mode: 'approval'`, and the conversation genuinely *was*
in approval mode at 11:21 (the 08:53 `manual_killswitch` set it to approval;
AUTO was not clicked until 11:23:55).

**4. After AUTO was clicked, NO customer message ever reached the workflow.**
AUTO was activated at **11:23:55** (exec 677) and again at **11:31:27** (exec
681); `conversation_modes` rows 44 & 46 confirm `mode=autonomous` persisted for
`274942918680787@lid`. But every inbound webhook *after* those clicks was
WhatsApp **status-broadcast noise**, not a customer message:

| Exec | Started | WAHA event | from | body |
|------|---------|-----------|------|------|
| 678 | 11:29:33 | message | `status@broadcast` | (empty, hasMedia) |
| 679 | 11:29:39 | message | `status@broadcast` | (empty, hasMedia) |
| 682 | 11:32:26 | message | `status@broadcast` | (empty, hasMedia) |
| 683 | 12:11:52 | message | `status@broadcast` | (empty, hasMedia) |

All four were correctly dropped by `Filter Inbound` (empty body; `from` is not
`@c.us`/`@lid`). **The last real customer messages were execs 672 & 673, sent
*before* AUTO was activated.** Exec 683 is the most recent execution overall —
there is nothing after it.

**5. Bridge logs corroborate:** no `autosend-check GATE`, no `autosend-state
ARM`, no `autosend-check COMMIT` after 08:53 — i.e. the autonomous branch was
never exercised with `mode=autonomous` after the H8 tests.

### Conclusion
There is **no execution on record where a customer message was processed while
the conversation was in autonomous mode** since the H8 sign-off. The chain that
*did* run (646–648) worked; the chain that *would* run is unchanged. The
operator's recent AUTO activations were simply never followed by a customer
message that reached the workflow — only status-broadcast noise arrived.

Most likely explanations (in order): the follow-up test message was not sent
from the test phone; or it was sent but **WAHA did not receive / forward it**
(WhatsApp session or WAHA-engine issue); or the operator expected the AUTO click
*itself* to send (it does not — AUTO only sets the mode; the *next inbound
customer message* triggers the auto-send).

### Proposed action — controlled re-test (no code fix; nothing to fix yet)
1. Click 🤖 AUTO on a draft; verify `conversation_modes` newest row for that
   customer is `autonomous`.
2. From the test phone, send a **real text message**.
3. Confirm a new n8n execution appears that reaches **`Buffer Message`** (i.e.
   it passed `Filter Inbound`). If no such execution appears → the problem is
   **WAHA inbound delivery**, not the workflow — check the `n8n-waha-1`
   container logs and the WhatsApp session state.
4. If the execution runs, trace where it stops. The four silent-stop gates, in
   order, are: `Auto Prep` (`if(!d) return []`), `Auto Is Autonomous`
   (`mode !== 'autonomous'`), `Auto Decide` (`armed !== true`), `Auto Send Gate`
   (`auto_send !== true`). Capture **that** execution and re-diagnose against it.

Note one genuine edge case to watch in the re-test: `Arm Autosend` has
`onError: continueRegularOutput` — if the bridge is unreachable at arm time, the
branch continues but nothing is stored in Redis, so `Auto Decide` later finds
`armed=false` and stops **silently**. Confirm the bridge is up during the test.

---

## BUG 3 — improver does not run on autonomous-mode drafts

### Reported
The FR-5 improver does not improve autonomous-mode drafts. The operator notes
autonomous mode has a 1–5 min countdown (`Auto Wait` = `auto_delay_s`,
60–300 s), so a window for the improver clearly exists.

### Root cause — CONFIRMED, visible in node code

The improver branch is structurally **reachable** on autonomous drafts:
`Save Telegram MsgID → Improve Wait (20 s) → Check Autosend Key → Check Pending
→ Hermes Improve → Apply Improvement → Edit Improved Card`. Autonomous drafts
*do* post a Telegram card and *do* run this branch.

But `Check Pending` deliberately aborts it. Its current code (added by the
`1181c5b` "Mark Auto Sent residual" fix):
```js
const __as = $('Check Autosend Key').item.json || {};
if (__as.armed === true) { return []; }   // <-- skips the improver
```
with the in-code comment:
> *"BUG-2 residual fix: skip autonomous drafts — they auto-send the copy armed
> at countdown start, so improving them is moot and would edit a card whose
> text never went out."*

So: an autonomous draft is armed by `Arm Autosend` (Redis `SET autosend:<id>`).
20 s later `Improve Wait` ends, `Check Autosend Key` does `GET autosend:<id>` →
`armed=true` → `Check Pending` returns `[]` → **the improver is skipped, by
design.** This is the proximate root cause and it is a **deliberate gate**, not
a wiring accident.

**Timing confirms it triggers every time:** branch B reaches `Arm Autosend`
within ~1–3 s of `Save Telegram MsgID` (all instant nodes in between); `Improve
Wait` is 20 s. So the key is always armed before `Check Autosend Key` reads it.

**There is a second, deeper reason behind the gate** (the architectural issue):
`Arm Autosend` snapshots the draft **text** into Redis at countdown start, and
`Auto Decide` later sends `r.data.draft_text` — that **arm-time snapshot**. The
improver runs in a *parallel* branch and only edits the Telegram *card*. So even
if the gate were removed, the improver's improvement would **not** reach the
auto-send — `Auto Send WAHA` would still send the pre-improvement snapshot. The
gate's authors removed a no-op, not a feature.

### Evidence the improver itself works (in approval mode)
Execs 672 & 673 ran `Improve Wait → Check Autosend Key → Check Pending → Hermes
Improve → Apply Improvement → Edit Improved Card` to completion — because they
were *not* armed (approval mode → autonomous branch stopped at `Auto Is
Autonomous` → `Arm Autosend` never ran → `armed=false` → `Check Pending`
proceeded). So the improver is healthy; it is gated **only** by the `armed`
check.

### Proposed fix — scope & risk (DO NOT FIX — proposal only)
Removing the one-line `armed` skip is **not sufficient** — it would let the
improver edit the card while `Auto Send WAHA` still sends the stale arm-time
snapshot (the exact no-op the gate was added to prevent). A real fix must make
the improvement actually reach the send. Two options:

- **Option A — re-sequence: improve, *then* arm.** Move the FR-5 improve step so
  it completes *before* `Arm Autosend` snapshots the text — arm with the
  improved text. Scope: moderate (re-order the autonomous branch). Risk:
  moderate — restructures the autonomous send path.
- **Option B — send the live text, not the snapshot.** Keep arming early, but
  have `Auto Decide` / `Auto Send WAHA` re-read the *current* draft text
  (post-improvement) at send time instead of `r.data.draft_text`. Scope:
  moderate. Risk: moderate — must guarantee the read is consistent with what the
  operator saw on the card.

Either way this is an **architectural change to the autonomous-send ordering**,
touching a path that sends real WhatsApp messages — it warrants its own plan and
careful testing, not a quick patch. The original gate rationale (never send a
text the operator never saw improved) remains valid and must be preserved by the
re-sequencing.

---

## Cross-cutting observations

- **`$getWorkflowStaticData` is a recurring hazard.** It has now caused BUG-1
  (autonomous send, fixed), the Mark-Auto-Sent residual (fixed), and BUG 1 here
  (debounce, open). 20 nodes still call it (`pendingQueue`, `inboundBuffer`,
  `lastSeq`, break state). `pendingQueue` works *only* because it is written and
  read within a single execution; any feature that needs cross-execution or
  post-Wait visibility of staticData will break the same way. Worth a deliberate
  audit + migration plan, separate from these three bugs.
- **`Filter Inbound` is working correctly** — it cleanly drops `status@broadcast`
  and empty-body events; it is not implicated in any of the three bugs.
- **Redis is healthy and idle** (`n8n-redis-1` up, `DBSIZE 0`) — ready to back a
  debounce rebuild.

## Recommended priority
1. **BUG 1** — confirmed, user-visible (duplicate drafts on every multi-message
   burst), and the fix path is well-understood (Redis, mirrors a prior fix).
2. **BUG 3** — confirmed, but the fix is an architectural change; needs a plan.
3. **BUG 2** — not reproduced; run the controlled re-test first. Do **not**
   write a fix until a real failing execution exists to diagnose.

---

## Appendix — evidence sources
- Workflow: `workflows/phase-1b-telegram.json` (live `azPIy9OcDwiPV5uY`, 98 nodes).
- Bridge: `hermes-bridge/server.py` — `_autosend_check`, `_autosend_state`,
  `_set_mode`, `get_mode`, `evaluate_caps`.
- n8n executions analysed: 634–683 (`includeData=true`, node-level run data).
- Bridge logs: `journalctl --user -u hermes-bridge`.
- Postgres: `conversation_modes` (rows 33–46), `autonomous_sends` (today:
  intervention ×16, auto ×3).
- Redis: `n8n-redis-1` — `PING`/`DBSIZE`/`SCAN`.
- Key execs: 646–648 (autonomous send success), 672/673 (debounce reproduction +
  front-half-of-autonomous-chain verification), 678/679/682/683
  (post-AUTO inbound = status-broadcast noise).
