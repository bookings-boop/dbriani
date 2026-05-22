# `pendingQueue` → Redis Migration — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL — `superpowers:executing-plans` to run task-by-task. Steps use `- [ ]`.

**Goal:** Move the in-flight `pendingQueue` (operator-approval drafts) out of n8n `staticData` into Redis, eliminating the staticData-isolation race that periodically loses drafts and produces "DRAFT EXPIRED" when the operator taps Send/Auto/Edit too soon after a card appears.

**Architecture:** One Redis STRING per draft (`draft:<id>` = JSON), plus a SET (`drafts:active`) and a per-customer sorted-set (`drafts:bycustomer:<id>`) for the few "find by customer / find active" lookups. All access via new bridge endpoints (`POST /queue` with `action: save | get | update | mark | drop | latest-for-customer`). Token-gated like the rest of the bridge. **Phased shadow-write rollout** — every step is independently reversible and the staticData copy stays as a fallback until the final phase removes it.

**Tech stack:** Hermes Bridge stdlib Python (`hermes-bridge/server.py`) using the existing `_redis()` helper, n8n workflow `azPIy9OcDwiPV5uY`, the `n8n_deploy.safe_put` deployer.

---

## §0 — Why migrate (the bug class, in one paragraph)

`pendingQueue` lives in `$getWorkflowStaticData('global')`. n8n loads staticData *per-execution at start*, and persists it *at execution end* (and at Wait-node pause). Concurrent executions are **isolated** — exec A's mid-flight push to `pendingQueue` is invisible to exec B until A persists; if A persists *after* B (because A had a longer Wait), A's snapshot overwrites B's writes. Empirically observed live many times today (see `docs/bug-diagnosis-2026-05-22.md` for prior instances; "DRAFT EXPIRED" on execs 754 / 756 / 758 is the most recent reproduction — the Send callback ran while the customer-msg exec's `Improve Wait` (20 s) + `Auto Gate` httpRequest was still in flight, so the persisted snapshot did not yet contain the draft id the callback was looking up).

Redis is the right primitive: shared, atomic, and we already have it wired (`n8n-redis-1`, used by `/autosend-state` and `/debounce`).

---

## §1 — Scope: every node that touches `pendingQueue`

Confirmed by `grep` against the live workflow (17 nodes, post-Nomod build):

| Node | Reads | Mutates | Role |
|---|---|---|---|
| `Queue & Format` | — | **push** | builds & enqueues a new draft |
| `Save Telegram MsgID` | find by id | `telegram_message_id` | binds the Telegram message_id back to the draft |
| `Parse Callback` | find by id | — | resolves the draft for every operator-button callback |
| `Mark Sent` | find by id | `status='sent'`, `messages_sent_count++` | after a Send completes |
| `Mark Skipped` | find by id | `status='skipped'` | after a Skip |
| `Set Awaiting Edit` | find by id | `status='awaiting_edit'` | after Edit Prompt is tapped |
| `Prep Regen` | find by id | — (read draft text) | builds the regen prompt |
| `Parse Regen` | find by id | `messages`, `draft_text` | applies the regen result |
| `Process Text Reply` | find by **customer** (status=awaiting_edit) | — | routes a free-text reply from the operator |
| `Prep Refine` | find by id | — | builds the refine prompt |
| `Parse Refine` | find by id | `messages`, `draft_text` | applies the refine result |
| `Check Pending` | find by id | — (read status) | gates the FR-5 improver |
| `Apply Improvement` | find by id | `messages`, `draft_text`, `improved_note` | applies the improver result (skipped on payment drafts) |
| `Prep Learn` | find by id | — | gathers feedback context for the learner |
| `Auto Prep` | find by id | — | preps the autonomous-send card |
| `Find Break` | find by id | — | reads `break_condition` from the draft |
| `Auto Decide` | already on Redis (`autosend:<id>`) | — | not part of this migration |

**Out of scope (intentional):**
- `staticData.autosend:*` — already on Redis (BUG-1 fix).
- `staticData.inboundBuffer` / `lastSeq` — already on Redis (FR-3 debounce / BUG-1 here).
- `behavior_rules`, `conversation_modes`, `autonomous_sends`, `customer_facts` — Postgres (correct, no change).

---

## §2 — Redis data model

```
draft:<id>                          STRING  — JSON-encoded draft object, TTL 24 h
drafts:active                       SET     — member = draft_id, currently pending
drafts:bycustomer:<customer_id>     ZSET    — score = timestamp_ms, member = draft_id
```

**Draft JSON** = same shape `Queue & Format` builds today:
```json
{
  "id": "1779488336136_vn7r5",
  "timestamp": "2026-05-22T22:19:23Z",
  "customer_phone": "274942918680787@lid",
  "customer_name": "Mark",
  "customer_message": "...",
  "conversation_history": "...",
  "history_count": 12,
  "messages": ["..."],
  "draft_text": "...",
  "messages_sent_count": 0,
  "notes": "...",
  "telegram_message_id": 12345,
  "telegram_chat_id": 5532831477,
  "status": "pending",
  "break_condition": {"hit": false},
  "is_lead": false,
  "is_payment": false
}
```

**TTL:** 24 h (env `BRIDGE_QUEUE_TTL=86400`). Forces stale drafts to expire automatically; we never need a sweeper. Operator can act on a draft anytime within 24 h; old/abandoned drafts vanish.

**Lifecycle:** `save` → SET key + EXPIRE + SADD `drafts:active` + ZADD `drafts:bycustomer:<cid>`. `mark` to `sent`/`skipped` → patch status + SREM `drafts:active` (kept in `bycustomer` for audit during TTL). `drop` (cleanup) → DEL key + SREM `drafts:active` + ZREM `drafts:bycustomer`.

---

## §3 — Bridge endpoint surface (`POST /queue`)

Single endpoint, dispatched by `action`:

| `action` | Body | Behaviour |
|---|---|---|
| `save` | `{draft: {...}}` | Validates `draft.id` + `draft.customer_phone`; writes `draft:<id>` (JSON), TTL, indexes. Returns `{ok, draft_id}`. |
| `get` | `{draft_id}` | Returns `{ok, draft \| null}`. |
| `update` | `{draft_id, fields: {...}}` | Read-modify-write: GET, merge `fields` into draft, SET. Returns `{ok, draft}`. Atomic per-key via single-threaded Redis (no MULTI needed at our load). |
| `mark` | `{draft_id, status}` | Convenience for status changes — same as `update` with `fields.status`; also adjusts `drafts:active` (SREM on non-`pending`). |
| `latest-for-customer` | `{customer_id, status?}` | Reads `drafts:bycustomer:<cid>` ZREVRANGEBYSCORE → for each id, GET draft, optionally filter by status, return the newest matching. Used by `Process Text Reply`. |
| `drop` | `{draft_id}` | DEL + SREM + ZREM. (Mostly unused — TTL handles cleanup.) |

**Fail-safe:** every response is 200 with `{ok: false, error: ...}` on bridge-side errors (same convention as `/payment-link` and `/customer-facts`). Bridge unreachable → workflow nodes use `onError: continueRegularOutput` and fall back to staticData (during the shadow-write phase, which we keep until Phase 4).

---

## §4 — Migration strategy: 4 phases, independently deployable

Each phase = one build script, one `safe_put`, one test-phone verification, one commit. **A failure at any phase rolls back via `PRE-<TAG>` backups; staticData remains intact through Phase 3 as the safety net.**

| Phase | Effect | Reversible? |
|---|---|---|
| **1** | Add bridge endpoints + unit tests. **No workflow change.** | Yes — purely additive. |
| **2** | `Queue & Format` writes to **both** staticData (legacy) and Redis (new). All readers still use staticData. | Yes — Redis writes are extra; staticData behaviour unchanged. |
| **3** | Migrate **readers** to Redis-first, staticData-fallback. Mutating nodes still write to both. | Yes — fallback preserves the old behaviour if Redis is empty. |
| **4** | Make Redis the **only** source of truth. Remove staticData reads/writes from the 17 nodes. Optionally clear `staticData.pendingQueue`. | Yes — reverse the build script; staticData is empty but harmless. |

Each phase is a separate commit. Phase 1 is unit-tested in isolation. Phases 2–4 are verified live on the test phone before commit.

---

## §5 — Task breakdown (bite-sized, each verifies before progressing)

### Task 1 — Bridge endpoints + unit tests

**Files:**
- Modify: `hermes-bridge/server.py` (≈ +140 lines).
- Create: `hermes-bridge/test_queue.py`.

**Steps:**

- [ ] **1.1 Add `QUEUE_TTL` constant** (after `DEBOUNCE_TTL`):
```python
QUEUE_TTL = int(os.environ.get("BRIDGE_QUEUE_TTL", "86400"))
```

- [ ] **1.2 Add pure helpers** (module-level, near `_redis`):
```python
def _draft_key(did):   return "draft:" + did
def _byc_key(cid):     return "drafts:bycustomer:" + cid
DRAFTS_ACTIVE = "drafts:active"

def _draft_save(draft):
    """Write draft, set TTL, add to indexes. Returns (ok, err)."""
    did = (draft.get("id") or "").strip()
    cid = (draft.get("customer_phone") or "").strip()
    if not did or not cid: return False, "id + customer_phone required"
    ts = int(time.time() * 1000)
    _, err = _redis(["SET", _draft_key(did), json.dumps(draft),
                     "EX", str(QUEUE_TTL)])
    if err: return False, err
    if (draft.get("status") or "pending") == "pending":
        _redis(["SADD", DRAFTS_ACTIVE, did])
    _redis(["ZADD", _byc_key(cid), str(ts), did])
    _redis(["EXPIRE", _byc_key(cid), str(QUEUE_TTL)])
    return True, None

def _draft_get(did):
    out, err = _redis(["GET", _draft_key(did)])
    if err: return None, err
    raw = (out or "").strip()
    if not raw: return None, None
    try: return json.loads(raw), None
    except Exception as e: return None, repr(e)

def _draft_update(did, fields):
    """Read-modify-write. Returns (draft, err)."""
    d, err = _draft_get(did)
    if err: return None, err
    if not d: return None, "draft not found"
    d.update(fields or {})
    # status side-effect on the active set
    if "status" in (fields or {}):
        if (fields["status"] or "") == "pending":
            _redis(["SADD", DRAFTS_ACTIVE, did])
        else:
            _redis(["SREM", DRAFTS_ACTIVE, did])
    _, err = _redis(["SET", _draft_key(did), json.dumps(d),
                     "EX", str(QUEUE_TTL)])
    return (d, None) if not err else (None, err)
```

- [ ] **1.3 Register `/queue` in `do_POST`** (allowed-paths tuple + dispatch).

- [ ] **1.4 Implement `_queue` handler** with `action` dispatch:
```python
def _queue(self, payload):
    """pendingQueue Redis backing — see docs/pendingqueue-redis-migration-plan.md.
    Fail-safe: always returns 200 with ok flag."""
    action = (payload.get("action") or "").strip().lower()
    if action == "save":
        d = payload.get("draft") or {}
        ok, err = _draft_save(d)
        self._send(200, {"ok": ok, "error": err, "draft_id": d.get("id")})
        return
    if action == "get":
        did = (payload.get("draft_id") or "").strip()
        if not did: self._send(400, {"ok": False, "error": "draft_id required"}); return
        d, err = _draft_get(did)
        self._send(200, {"ok": True, "draft": d, "found": d is not None,
                         "error": err})
        return
    if action == "update":
        did = (payload.get("draft_id") or "").strip()
        d, err = _draft_update(did, payload.get("fields") or {})
        self._send(200, {"ok": d is not None, "draft": d, "error": err})
        return
    if action == "mark":
        did = (payload.get("draft_id") or "").strip()
        status = (payload.get("status") or "").strip()
        d, err = _draft_update(did, {"status": status})
        self._send(200, {"ok": d is not None, "draft": d, "error": err})
        return
    if action == "latest-for-customer":
        cid = (payload.get("customer_id") or "").strip()
        want_status = (payload.get("status") or "").strip() or None
        if not cid: self._send(400, {"ok": False, "error": "customer_id required"}); return
        out, _ = _redis(["ZREVRANGE", _byc_key(cid), "0", "20"])
        for did in (out or "").splitlines():
            did = did.strip()
            if not did: continue
            d, _ = _draft_get(did)
            if not d: continue
            if want_status and d.get("status") != want_status: continue
            self._send(200, {"ok": True, "draft": d}); return
        self._send(200, {"ok": True, "draft": None})
        return
    if action == "drop":
        did = (payload.get("draft_id") or "").strip()
        _redis(["DEL", _draft_key(did)])
        _redis(["SREM", DRAFTS_ACTIVE, did])
        self._send(200, {"ok": True})
        return
    self._send(400, {"ok": False,
                     "error": "action must be save|get|update|mark|latest-for-customer|drop"})
```

- [ ] **1.5 Unit tests** (`hermes-bridge/test_queue.py`) — assert-based, run as `python3 hermes-bridge/test_queue.py`. Tests: `_draft_save` round-trips through `_draft_get`; `_draft_update` patches a field and preserves others; `mark sent` removes from `drafts:active`; `latest-for-customer` returns the newest by timestamp; missing-key returns `None`/`found:False`; bad-JSON in Redis returns an error tuple. Use a `__test__` prefix on draft ids to avoid colliding with live data.

- [ ] **1.6 Deploy bridge** (`scripts/deploy_bridge.py`). Smoke-test on the box:
```
. ~/hermes-bridge/.env
B() { curl -s -X POST localhost:8788/queue \
  -H "X-Bridge-Token: $BRIDGE_TOKEN" -H "Content-Type: application/json" -d "$1"; }
B '{"action":"save","draft":{"id":"__t1__","customer_phone":"x","status":"pending","messages":["hi"]}}'
B '{"action":"get","draft_id":"__t1__"}'
B '{"action":"mark","draft_id":"__t1__","status":"sent"}'
B '{"action":"drop","draft_id":"__t1__"}'
```

- [ ] **1.7 Commit** — `Migration: add Redis-backed /queue bridge endpoint`.

**Verification gate:** all unit tests pass + smoke test returns `ok:true` for each action. **No workflow change yet — operator-facing behaviour unchanged.**

---

### Task 2 — Shadow write at draft creation

**Files:**
- Create: `scripts/build_queue_migrate_2_shadow_write.py`.
- Modify: `Queue & Format` (jsCode) — append, after the staticData push, an `await this.helpers.httpRequest(...)`-style call **NO — code nodes can't carry the bridge token cleanly.** Instead, add a new httpRequest node.

**Topology change:**
```
Queue & Format ─┬─> Send Draft to Telegram   (existing)
                └─> Persist Draft (NEW http)  (parallel, fire-and-forget)
```

`Persist Draft` is a new httpRequest node, parallel branch off `Queue & Format` output[0]. `onError: continueRegularOutput` (a Redis failure must never block the draft posting). Body:
```
={ "action": "save", "draft": {{ JSON.stringify($json.draft) }} }
```

This requires `Queue & Format` to expose the full `pending` object on its output — add `draft: pending` to the return JSON. (`Send Draft to Telegram` is unaffected; it still reads `$json.telegram_payload`.)

- [ ] **2.1 Build script** mirrors `build_nomod_payment.py`'s structure.
- [ ] **2.2 Anchors** in `Queue & Format`: append `,\n  draft: pending` inside the final `return { json: {...} }`.
- [ ] **2.3 New node** `Persist Draft` with `BRIDGE_CRED`, body as above.
- [ ] **2.4 Wire** `Queue & Format` output[0] to **both** `Send Draft to Telegram` and `Persist Draft`.
- [ ] **2.5 Dry-run** → `--deploy` → safe_put.
- [ ] **2.6 Verify on test phone:** send one customer message. Then on EC2: `docker exec n8n-redis-1 redis-cli GET draft:<the new draft id>` → confirm the JSON is there. `SMEMBERS drafts:active` includes the id.
- [ ] **2.7 Commit** — `Migration: shadow-write new drafts to Redis (staticData still primary)`.

**Verification gate:** new drafts appear in Redis. Approval flow unchanged (staticData still primary). 87+ pre-existing drafts in staticData are not migrated — they age out naturally.

---

### Task 3 — Read fallback at the callback

**Files:**
- Create: `scripts/build_queue_migrate_3_callback_fallback.py`.
- Modify: `Parse Callback` jsCode (small) + insert one new httpRequest node.

**Topology change:**
```
Route Update Type ─> Redis Draft Lookup (NEW http) ─> Parse Callback ─> (existing)
```

`Redis Draft Lookup` parses the draft_id out of the callback data and hits the bridge:
```
={ "action": "get", "draft_id": {{ JSON.stringify(($json.body.callback_query.data || '').split(':')[1] || '') }} }
```

`Parse Callback` (modified) — same logic, but resolves `draft` from BOTH sources:
```js
const sdDraft = queue.find(d => d.id === draftId);
let redisDraft = null;
try { const r = $('Redis Draft Lookup').item.json; redisDraft = (r && r.draft) || null; }
catch (e) { redisDraft = null; }
const draft = sdDraft || redisDraft;
```

`draft_found = !!draft`. All downstream nodes unchanged — they read `$('Parse Callback').item.json.draft` as before.

- [ ] **3.1–3.5** anchors, build, dry-run, deploy.
- [ ] **3.6 Verify on test phone:** *immediately* after a draft card appears, tap Send (within the previously-failing ~30 s window). Confirm `draft_found:true` in `Parse Callback`'s output and that `Send One to Customer` ran. Repeat 3× over different intervals.
- [ ] **3.7 Commit** — `Migration: callbacks fall back to Redis when staticData missed`.

**Verification gate:** **the "DRAFT EXPIRED" issue is gone for all five callback actions** (Send, Auto, Edit, Regen, Skip). This is the user-visible payoff — even though Phases 2-3 only.

---

### Task 4 — Migrate the remaining readers + mutators

This is the bulk of the migration. Each node listed in §1 is migrated to Redis-primary. Approach: insert a `Redis Get <Node>` httpRequest before the existing code node when it needs the draft, and a `Redis Update <Node>` httpRequest after when it mutates.

Where the existing code node both reads and mutates (e.g. `Mark Sent`, `Apply Improvement`, `Save Telegram MsgID`), keep the code node for the response shape and add a mutate-via-bridge call right after. The staticData mutation stays during this phase as a fallback.

Subtasks (one commit each):

- [ ] **4.1** `Save Telegram MsgID` — call `/queue update {telegram_message_id}` after the staticData write.
- [ ] **4.2** `Mark Sent` — call `/queue mark sent` after.
- [ ] **4.3** `Mark Skipped` — call `/queue mark skipped` after.
- [ ] **4.4** `Set Awaiting Edit` — call `/queue mark awaiting_edit` after.
- [ ] **4.5** `Parse Regen` + `Parse Refine` — call `/queue update {messages, draft_text}` after.
- [ ] **4.6** `Apply Improvement` — call `/queue update {messages, draft_text, improved_note}` (continue to skip for `is_payment` drafts).
- [ ] **4.7** `Process Text Reply` — when looking up the customer's awaiting-edit draft, call `/queue latest-for-customer {status:"awaiting_edit"}` first; fall back to staticData.

Each step: dry-run, deploy, test-phone verify (send/skip/edit/regen/refine/improver/auto/lead — see §9), commit.

---

### Task 5 — Redis-primary cleanup

After §4 has soaked for a session and all flows pass:

- [ ] **5.1** Remove the staticData *reads* from all 17 nodes (they now go to Redis first; the staticData path is dead code).
- [ ] **5.2** Remove the staticData *writes*. `Queue & Format` no longer pushes to `data.pendingQueue`; mutating nodes no longer touch `d`.
- [ ] **5.3** Optionally clear `staticData.pendingQueue` once via a one-time script (or just leave it — the operator-side flow no longer reads it; it'll go stale).
- [ ] **5.4** Commit — `Migration: Redis-primary pendingQueue (staticData retired)`.

**Verification gate:** end-to-end test matrix (§9) all-green on the test phone.

---

## §6 — Risks

| # | Risk | Mitigation |
|---|---|---|
| R1 | A bridge / Redis outage takes the operator-approval flow offline. | Through Phases 2-4 the staticData copy remains as a fallback. Phase 5 increases dependency on bridge uptime; bridge is already required for other endpoints (`/improve`, `/autosend-state`, `/payment-link`). |
| R2 | A Redis update races a staticData update; the two diverge. | Acceptable through Phases 2-4 (operator's view comes from Phase-3 fallback logic, so Redis wins when present). Phase 5 eliminates the divergence by retiring staticData. |
| R3 | The 87+ existing staticData drafts disappear. | Not migrated by design. They age out via TTL once the workflow stops touching them. Operator can still act on them through Phases 2-4 (staticData path still works). |
| R4 | A mutating node fails to update Redis (bridge error). | `onError: continueRegularOutput` + staticData stays primary in Phases 2-4. Phase 5 — add explicit alerts on bridge-side failures. |
| R5 | `Parse Callback` Redis fallback misreads a stale draft. | Bridge returns `null` when the key has expired (TTL 24 h); fallback then yields `draft_found:false` correctly. |
| R6 | The Nomod `is_payment` flag must round-trip through Redis. | Already in the draft object; `_draft_save` serialises the whole draft, so `is_payment` survives. |

---

## §7 — Rollback (per phase)

- **Phase 1:** revert `server.py`, `systemctl --user restart hermes-bridge`. Endpoint goes away; no consumer yet, no impact.
- **Phase 2:** `scripts/rollback_workflow.py` to the `PRE-` backup. `Persist Draft` disappears; staticData behaviour intact.
- **Phase 3:** rollback. The `Redis Draft Lookup` node + the `Parse Callback` 3-line addition disappear. Callbacks read staticData only (back to the timing-race issue, but functional).
- **Phase 4:** per-subtask rollback. Each subtask is independent.
- **Phase 5:** safest reversal — restore the staticData reads/writes (the Phase-2/3/4 staticData fallback was preserved, so the code paths still exist in git history; restoring them is a revert).

---

## §8 — `PAYMENTS_ENABLED` & ops safety

This migration does **not** touch payments or autonomous-mode caps. `PAYMENTS_ENABLED`, the autonomous caps, and `/customer-facts` are unaffected. The kill-switches remain effective.

During Phase 2-3 deploys (the period when the test phone is needed), recommend keeping the test conversation in **approval** mode (`/manual`) — autonomous send + an in-flight migration is too many moving parts.

---

## §9 — End-to-end test matrix (run after Phase 3 *and* Phase 5)

| # | Trigger | Expected nodes that ran | Pass criterion |
|---|---|---|---|
| 1 | Customer message → operator taps **Send** | `Send One to Customer` ran | customer receives the draft text |
| 2 | Customer message → operator taps **Skip** | `Mark Skipped` ran, status='skipped' | no WAHA send |
| 3 | Customer message → operator taps **Edit** → reply with corrected text | `Set Awaiting Edit`, `Prep Refine`, `Claude AI (Refine)`, `Parse Refine`, `Edit Telegram (Refine)` ran | card edited to the refined text |
| 4 | Customer message → operator taps **Regen** | `Prep Regen`, `Claude AI (Regen)`, `Parse Regen`, `Edit Telegram (Regen)` ran | card edited to the regen text |
| 5 | Approval-mode draft → operator taps **Auto** then sends another message | `Set Auto Mode` ran, mode flipped autonomous; next message: `Arm Autosend` → `Auto Send WAHA` → `Mark Auto Sent` (do not touch the card) | autonomous send fires |
| 6 | Improver on an approval draft | `Check Pending` returned an item, `Hermes Improve` ran, `Apply Improvement` ran, `Edit Improved Card` ran | card edited to the improved text |
| 7 | Payment draft (`should_send_payment` true) | improver SKIPPED for payment draft, link survives to `Prepare Send` | sent message ends in `pay.nomodapp.com/en/l/<id>` |
| 8 | `/lead` outbound command | `Parse Lead Response` → `Queue & Format` → card | card posts |
| 9 | Send within 5 s of card appearing (the previously-failing window) | `Parse Callback.draft_found=true` (via Redis fallback) | customer receives; **no "DRAFT EXPIRED"** |
| 10 | TTL — let a draft sit 25 h then tap | `draft_found=false` | "DRAFT EXPIRED" message (this is correct — drafts genuinely do expire after 24 h) |

---

## §10 — Time estimate

| Phase | Build + dry-run | Deploy + verify | Total |
|---|---|---|---|
| 1 — bridge endpoints + unit tests | 50 min | 20 min | 70 min |
| 2 — shadow write at `Queue & Format` | 30 min | 15 min | 45 min |
| 3 — callback fallback | 35 min | 25 min (full button matrix) | 60 min |
| 4 — migrate 7 remaining nodes | 90 min | 60 min (matrix per subtask) | 150 min |
| 5 — Redis-primary cleanup | 40 min | 45 min (full matrix) | 85 min |
| | | **Total** | **≈ 6.8 h** |

Realistically a half-day of focused work + a second sitting for Task 4 (long tail of small subtasks).

---

## §11 — Operator decisions to confirm before Task 1

1. **TTL = 24 h** — accept? (Operator-side, a draft >24 h old just gives "DRAFT EXPIRED" — the same as today.)
2. **Phased rollout (Phases 1–5)** vs big-bang? *Recommend phased* — each phase is its own commit and is independently reversible.
3. **`Process Text Reply` text-reply path** — does it need migrating in Phase 4, or defer? It's lower-traffic than the callback path; the customer reply still works via the awaiting-edit flag. *Recommend migrating in Phase 4.*
4. **Cleanup of the existing 87+ staticData drafts** — let them age out naturally (recommended) vs migrate them to Redis (extra script, niceness only).
5. **Bridge availability assumption** — Phase 5 makes the operator-approval path dependent on the bridge. Already true for `/customer-facts` and `/payment-link`; consider it confirmed unless operator objects.

---

## §12 — Not in scope (explicitly)

- The wider question of moving *other* staticData state (e.g., autonomous mode shadow state) to Redis — already done where needed.
- Restructuring the autonomous-mode + payment-link interaction (open follow-up from `docs/nomod-minimal-plan.md`).
- The cosmetic `customer_name` fallback to Customer Facts in `Build Payment Message` (separate one-line fix).
- Migration of `customer_facts` / `behavior_rules` / `conversation_modes` — those are Postgres, not staticData; correctly so.
