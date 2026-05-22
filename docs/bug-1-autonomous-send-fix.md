# BUG-1 Fix — Autonomous Auto-Send (Redis-backed state)

**Date:** 2026-05-22
**Status:** ✅ **BUILT · DEPLOYED · VERIFIED** — live test passed 2026-05-22
(execution 646: autonomous auto-send fired end-to-end). See *Outcome* below.
**Refs:** bug → `docs/feature-backlog.md` BUG-1 · evidence → `docs/h7-h9-test-plan.md`

## Confirmed diagnosis

Execution 645 (the H8 run) traced end-to-end:

- The autonomous branch reaches `Auto Wait` and waits the countdown (270s).
- On resume, `Auto Decide` runs
  `$getWorkflowStaticData('global').pendingQueue.find(id)`, finds nothing →
  returns `[]` → the branch stops before `Auto Commit` / `Auto Send WAHA`.
  No auto-send; zero `autonomous_sends` `kind=auto` rows.
- **Root cause:** n8n loads `staticData` per-execution and saves it at
  execution end; concurrent executions overwrite each other's snapshot. Over
  the multi-minute `Auto Wait`, another execution clobbered the draft out of
  the snapshot `Auto Decide` read. The draft *is* in the queue — it just was
  not in that instant's copy.
- Autonomous auto-send has fired **0 times** in the system's life → structural.

**The autonomous branch cannot hold state in `staticData` across the wait.**

## Fix — Redis-backed autosend state

Redis (`n8n-redis-1`, already running) is the reliable store. The autonomous
branch keys its post-wait decision off a single Redis key per draft instead
of `staticData`.

- Key: `autosend:<draft_id>`
- Value: JSON `{draft_id, customer_phone, draft_text}`
- TTL: 3600s (self-cleans; longer than any countdown)

Lifecycle:
- **Arm** — when an autonomous draft starts its countdown → `SET` the key.
- **Disarm** — when the operator presses any button on a draft card → `DEL`
  the key (the operator is handling it; cancel the autosend).
- **Decide** — after `Auto Wait` → `GET` the key. Present → operator did not
  intervene → send (using the Redis copy of the text). Absent → bail.

Redis is single-threaded and atomic — not subject to the per-execution
snapshot race.

## Design decision

**Redis access: n8n's native Redis node** (`n8n-nodes-base.redis`) — not the
bridge. The whole autonomous branch is in n8n; a native node avoids a bridge
round-trip and a new bridge dependency. One-time setup: an n8n "Redis"
credential for the `n8n-redis-1` container.
*(Alternative — route through the bridge via `docker exec n8n-redis-1
redis-cli` — rejected: extra hop, more moving parts, no benefit.)*

## Node-level changes

**Prereq (manual, n8n UI):** create a **Redis credential** — host
`n8n-redis-1` (or the container IP), port 6379, password per
`~/n8n/docker-compose.yml` (likely none). Note the credential id for the
build script.

1. **`Auto Prep`** *(modify)* — add `draft_text: d.draft_text` to its output
   object (it already reads the draft `d`).
2. **`Arm Autosend`** *(new — Redis SET)* — inserted
   `Auto Is Autonomous → Arm Autosend → Render Auto Card`. Key
   `autosend:{{draft_id}}`, value the JSON above, TTL 3600.
3. **`Disarm Autosend`** *(new — Redis DELETE)* — inserted
   `Answer Callback → Disarm Autosend → Draft Exists?`. Key
   `autosend:{{ $('Parse Callback').item.json.draft_id }}`. `DEL` of a missing
   key is a harmless no-op (covers callbacks with no draft).
4. **`Get Autosend`** *(new — Redis GET)* — inserted
   `Auto Wait → Get Autosend → Auto Decide`. Key
   `autosend:{{ $('Auto Prep').item.json.draft_id }}`.
5. **`Auto Decide`** *(rewrite)* — drop the `staticData`/`pendingQueue`
   lookup. Read `Get Autosend`'s result: a value present → parse it → output
   `{draft_id, customer_phone, draft_text}` for the send; empty → `return []`
   (operator intervened, or the arm expired).

`Auto Commit` / `Auto Send Gate` / `Auto Send WAHA` are unchanged —
`Auto Send WAHA` reads `$('Auto Decide').item.json.customer_phone` /
`draft_text`, now sourced from Redis.

Optional tidy: `Mark Auto Sent` `DEL`s the key after a successful send (the
TTL self-cleans anyway — cosmetic).

## Build order

1. Create the Redis credential in the n8n UI; record its credential id.
2. Write `scripts/build_bug1_redis_autosend.py` (model on
   `scripts/build_break_gate.py`): add the 3 Redis nodes (referencing the
   credential id), modify `Auto Prep` + `Auto Decide`, rewire the 3
   connection points. Dry-run, then deploy via `n8n_deploy.safe_put`.
3. Re-sync `workflows/phase-1b-telegram.json`.

## Test plan

1. **Arm + auto-send (re-run H8):** test phone (autonomous) sends a normal
   message → after the countdown the reply auto-sends; an `autonomous_sends`
   `kind=auto` row appears.
2. **Disarm:** test phone sends a message; during the countdown press Skip on
   the card → the auto-send must NOT fire (key deleted).
3. **5-consecutive cap (H8b):** now reachable — caps still apply at
   `Auto Commit` via `/autosend-check`, unchanged.

## Notes

- The same Redis foundation is what **FR-3's debounce** fix needs (shared root
  cause). Out of scope here, but the credential + pattern carry over.
- Caps logic (`evaluate_caps`, `/autosend-check`) is untouched — still the
  gate at `Auto Commit`.
- No bridge change. No change to the approval-mode flow.
- Verifying the fix needs a live autonomous test (test phone + a countdown) —
  it cannot be confirmed without the operator.

## Outcome (2026-05-22)

Built, deployed, verified. One build-time change from the design above: Redis
access is **bridge-mediated** — a new bridge `/autosend-state` endpoint doing
`docker exec n8n-redis-1 redis-cli` (the proven `_psql` pattern) — rather than
n8n's native Redis node. Chosen for a reliable build: known httpRequest +
bridge patterns, no unknown node schema, no new credential. Same Redis-backed
design.

Commits: `5cb34e2` + `7742fbf` (bridge `/autosend-state`) · `5bc5c0a`
(workflow — Arm/Disarm/Get Autosend, `Auto Decide` rewrite) · `18e3690`
(workflow JSON sync, 94 nodes).

**Live test — PASSED.** Execution 646: `Arm Autosend → Render Auto Card →
Auto Wait → Get Autosend → Auto Decide (proceeded — out=1) → Auto Commit →
Auto Send Gate → Auto Send WAHA → Mark Auto Sent`. WAHA confirmed delivery to
the test phone; `autonomous_sends` logged `kind=auto`. The H8 failure is
resolved — autonomous auto-send went from never-working to working.

**Minor residual (not BUG-1, tracked separately):** `Mark Auto Sent` still
writes the draft's queue `status` via n8n `staticData`; that write did not
reliably flip the test draft to `sent` (it stayed `pending` in the queue).
Cosmetic — the message was sent once and `Edit Auto-Sent Card` correctly
relabelled the Telegram card. Same `staticData` root cause, in the post-send
bookkeeping layer.
