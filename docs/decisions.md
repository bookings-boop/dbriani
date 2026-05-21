# Build Decisions Log

Judgment calls made during the build, especially the autonomous overnight
session of 2026-05-20/21. Reversible decisions were made and recorded here;
irreversible/destructive ones were skipped and flagged for review.

---

## D1 — Integration mechanism: a thin HTTP bridge
Hermes has no request/response HTTP API (its `proxy` is an OpenAI passthrough;
`webhook` is fire-and-forget delivery). Built `hermes-bridge` — a small stdlib
HTTP service on the EC2 box that wraps `hermes chat -q … -Q` headless and
exposes `POST /draft` and `POST /save-rule`. N8N calls it; the approval flow is
unchanged. **Approved by operator (chose "thin HTTP wrapper").**

## D2 — Approval bot swapped to "Dubriani Hermes"
The N8N workflow's `TELEGRAM_BOT_TOKEN` was repointed from the old `8569…` bot
to the new `8861…` "Dubriani Hermes" bot; webhook re-registered; old bot's
webhook deleted. The old bot is reserved as the future admin bot.
**Approved by operator.**

## D3 — Captured behavior_rules are saved INACTIVE
Per the overnight rule "all new behavior_rules default to active=false until I
approve them": the bridge `/save-rule` endpoint inserts rules with
`active=false`. Rule injection (Step 5.2) only injects `active=true` rules, so
a captured rule does nothing until the operator activates it.
**To activate a reviewed rule:** `UPDATE behavior_rules SET active=true WHERE id=<id>;`
(run via `docker exec n8n-postgres-1 psql -U n8n -d n8n`).

## D4 — Quiet hours for proactive Telegram messages
Overnight rule: "Telegram messages to me should be queued, not sent at night."
Interpreted as applying to **proactive/system** messages (R1 error alerts,
trigger reminders, daily summary) — NOT to customer-approval draft cards, which
are the live product and must keep reaching the operator whenever a customer
writes. Quiet hours: 22:00–08:00 Asia/Dubai. Proactive messages in that window
are held and flushed after 08:00.

## D5 — Overnight work committed to a branch
Autonomous overnight work is committed to branch `overnight-build`, one commit
per completed step, for operator review before merge to `main`.

## D6 — Bridge ↔ Postgres via `docker exec psql`
The bridge reads/writes `behavior_rules` by shelling `docker exec n8n-postgres-1
psql` (role `hermes_rw`, scoped to the 3 Hermes tables). Chosen to keep the
bridge stdlib-only (no `psycopg` pip dependency). Postgres is not exposed off
the docker network, so this is also the least-exposed path.

---

## Overnight session — step-by-step decisions

### Step 5.3 — rule capture (deployed)
- 46-node workflow deployed; bridge `/save-rule` live. Captured rules insert
  `active=false` (D3). `rule_id` parsing fixed (psql `-tA` also emits the
  command tag — take line 1).

### Step 6 — trigger detection + reminder cron (deployed)
- **Detection in the bridge:** on every non-refine `/draft`, Hermes is asked to
  return a `detected_trigger`; the bridge inserts it into `customer_triggers`
  (status `pending`). No workflow change needed. Reversible.
- **Reminder cron:** `~/hermes-bridge/cron-reminders.py`, system crontab every
  15 min, quiet-hours aware (22:00–08:00 Dubai skipped). Marks each trigger
  `reminded` so it fires once.
- **Reminders go to the "Dubriani Hermes" bot**, not a separate admin bot — the
  operator now lives in that one chat; the old `8569` bot stays retired. The
  spec's two-bot split was dropped as needless friction (one operator, one
  chat). Reversible — point `ADMIN_TG_TOKEN` elsewhere to change it.
- **DEFERRED — manual trigger resolution** (`/resolve`, "Mariam paid"): not
  built. Reminders fire once (pending→reminded), so there is no repeat spam;
  marking `resolved`/`dismissed` is a SQL update for now. Flagged for review —
  low risk, worth adding later as a Telegram command.
- **DEFERRED — R1 error-alert night-queuing:** R1 alerts are not quiet-hours
  gated. Judged low-value vs. effort: an R1 alert is informational ("nothing
  was force-sent"), night customer traffic is low, and bridge restarts during
  the build are the main (≈2s) risk window. Reminders and the daily summary
  ARE quiet-hours aware. Flagged for review.

### Step 7 — conversation health scoring (deployed)
- Hermes scores every initial draft's conversation health (good / warm /
  at_risk / cold + reason). The bridge prepends "⚕️ <score> — <reason>" to the
  operator notes (shows on the draft card with **no workflow change**) and
  UPSERTs a snapshot into a new `conversation_health` table for the daily
  summary.
- Kept lean: one latest-snapshot row per customer, no per-draft history, no
  separate health UI. Additive + reversible.

### Step 8 — autonomous mode (deployed, dormant)
- Originally planned to defer the live auto-send branch (unsupervised overnight
  = the irreversible risk to skip). The operator then chose to stay online, so
  it was **built and deployed in full** — but it is **dormant**: every
  conversation defaults to `approval`, the `IF Autonomous` node fail-closes to
  the approval branch, and nothing auto-activates.
- Bridge: `get_mode` / `set_mode` / `manual_killswitch`; `POST /set-mode`
  (`customer_id:"__ALL__"` = kill switch). `/draft` returns `conversation_mode`.
- Workflow: `IF Autonomous` → `Auto-Send to Customer` (WAHA) / approval branch;
  operator commands (`let it run`, `take back`, `pause`, `/manual`).
- **DEFERRED — safety caps & auto-break (spec §5.7):** daily/per-conversation
  caps and automatic break conditions are NOT built. A conversation set to
  `autonomous` auto-sends every reply until `take back` / `/manual`. The
  primary safety is per-conversation opt-in + the `/manual` kill switch.
  **Flagged for review — recommend adding caps before real use.** See
  `docs/autonomous-mode.md`.
- Autonomous mode is **deployed but not behaviourally tested** (testing needs a
  live autonomous conversation + a test number) — test steps in
  `docs/autonomous-mode.md`.

### Step 9 — daily summary digest (deployed)
- `cron-daily-summary.py`: assembles a digest (rules captured in 24h, pending
  follow-up triggers, at-risk conversations, conversations not in approval
  mode) from Postgres and sends it to the operator via Telegram. System
  crontab `0 4 * * *` UTC = 08:00 Asia/Dubai.
- **Script-assembled, not Hermes-narrated** — chosen for reliability and zero
  LLM cost. A Hermes-written narrative digest could be a later enhancement.
- Draft/conversation **count** omitted — it needs n8n's `execution_entity`,
  which `hermes_rw` is deliberately not granted. The lists cover the
  actionable items. Minor.

### §5.7 — autonomous-mode safety caps (deployed)
- Three caps, evaluated in the bridge: **daily** (≤20 auto-sends/day, resets
  00:00 Dubai), **per-conversation consecutive** (checkpoint every 5),
  **random QC sampling** (5% of auto-sends → approval). All default ON;
  configurable via `CAP_*` in `~/hermes-bridge/.env`.
- The bridge computes `auto_send` = (mode `autonomous` AND all caps pass).
  `IF Autonomous` now gates on `auto_send`, not the raw mode — fail-closed.
- New table `autonomous_sends` (event log: `auto` / `checkpoint` /
  `intervention`). `/caps` command + bridge endpoint; caps line in the digest.
- ✅ **Verified 2026-05-21** (after the auth blocker was resolved): all three
  caps tested bridge-side — per-conversation checkpoint blocks at 5 consecutive
  (`auto_send=false`), fresh autonomous passes (`auto_send=true`), daily cap
  blocks at 20 (`auto_send=false, "daily cap reached (26/20)"`), `/caps`
  reports correctly. The workflow's consumption of `auto_send` is structurally
  verified; the full WAHA auto-send path is exercised by H7–H9.

### 🚨 BLOCKER (2026-05-21, ~03:20 Dubai) — Anthropic API access failing
- Hermes → Anthropic returns `401 invalid x-api-key`, then `400` *"Third-party
  apps now draw from your extra usage, not your plan limits — add more at
  claude.ai/settings/usage"*.
- Cause: the `ANTHROPIC_API_KEY` in `~/.hermes/.env` is invalid (revoked?)
  and/or the Anthropic account has no usage credit for API/third-party use.
- **Effect: all drafting is down** — every customer message → bridge → Hermes
  → 401/400 → R1 alert. This is **not a code bug** — it is external (the
  Anthropic account/key).
- **Fix (operator):** put a valid key in `~/.hermes/.env` `ANTHROPIC_API_KEY`
  and/or add usage at claude.ai/settings/usage, then
  `systemctl --user restart hermes-bridge`. Drafting self-heals once the key
  works — no redeploy needed.
- **✅ RESOLVED (2026-05-21):** root cause was `~/.claude/.credentials.json`
  (a Claude Code OAuth token) overriding the `auth.json` credential pool — the
  operator renamed it to `.disabled-2026-05-21` and moved the anthropic pool to
  API-key credentials (`dubriani-prod` on the default profile). Drafting
  verified restored — a live `/draft` returned a clean draft in 7.4s with the
  full field contract (`health`, `auto_send`, etc.) intact.

### 🔴 INCIDENT + ROLLBACK (2026-05-21) — Hermes drafting unstable; reverted
- Under real traffic the Hermes drafting path degraded: `/draft` latency rose
  from ~10s to 20–50s+, several calls exceeded the bridge's 120s timeout →
  `502` → R1 "a step failed". Refinement (same path) failed with it.
- **DECISION:** rolled the LIVE workflow back to the pre-Hermes Phase 1B
  (40-node, direct Claude — T1–T6-proven) via `scripts/rollback_workflow.py`,
  preserving the 60-entry draft queue. Hermes **PAUSED** (bridge stopped +
  disabled, Dubriani crons removed) — code retained on branch `overnight-build`.
- 53-node Hermes snapshot: `workflows/phase-1b-telegram.PRE-ROLLBACK-*.json`.
- **Before re-deploying Hermes:** diagnose the latency regression OFF the live
  path. Hypotheses: `hermes chat -t memory` multi-turn loops slowing as the
  profile's memory/session store grew; `default` profile state bloat from
  build-time drafts. Likely fix: drop `-t memory`; prune profile state;
  re-measure before any re-deploy.

### 🔴 BUG + FIX (2026-05-21) — reply-to-draft routed to the WRONG customer
- **Symptom:** after Send, replying to a draft card with a follow-up (e.g. a
  payment link) delivered it to a *different* customer.
- **Root cause:** Telegram message IDs are per-chat counters that reset on bot
  swaps/restarts. The `pendingQueue` never prunes (60 entries) and held old +
  new drafts sharing IDs — **11 collisions confirmed**, each mapping one ID to
  two different customers. `Process Text Reply` did `queue.find(x =>
  x.telegram_message_id === r.message_id)` → *first* (oldest) match → a stale,
  unrelated customer.
- **FIX** (`scripts/fix_reply_routing.py`, deployed to live): `Process Text
  Reply` now scans the queue **newest-first**, matching the current card.
  Pre-Hermes Phase 1B bug — **the same fix must be folded into the Hermes
  (`overnight-build`) `Process Text Reply` before Hermes is re-deployed.**
- **FOLLOW-UP (flagged):** the `pendingQueue` grows unbounded — `Mark Sent` /
  `Mark Skipped` never remove entries. Prune it / remove actioned entries; the
  deferred Redis-queue migration (R10) is the proper structural fix.

### 🧹 QUEUE PRUNE (2026-05-21) — one-time de-bloat of the live draft queue
- Ran `scripts/prune_queue.py` against the live workflow: queue **61 → 34**.
- Rule: keep **every `pending` entry** (22 — the operator's actionable queue,
  never dropped) + the **12 most-recent done** (`sent`/`skipped`) entries (so
  reply-to-a-recent-draft still resolves); dropped 27 stale done entries.
- Backup: `workflows/phase-1b-telegram.PRE-PRUNE-*.json` (gitignored — holds
  customer data in staticData). Workflow re-activated + verified (34 entries).
- This is a **one-time** cleanup. The unbounded-growth design flaw remains —
  the queue will re-bloat. The structural fix (self-prune in `Queue & Format`,
  or the R10 Redis migration) is still pending. Re-run `prune_queue.py` as
  interim hygiene whenever the queue gets large.

### 🛠️ FR-1 + FR-2 DEPLOYED (2026-05-21) — Edit feedback loop + `/lead` intake
- `scripts/build_fr1_fr2.py` applied FR-1 (Edit button → Claude feedback loop)
  and FR-2 (`/lead` outbound new-lead intake) to the live workflow: **40 → 49
  nodes**. 9 nodes added, 4 modified (Process Text Reply, Route Text Action,
  Edit Telegram (Edit Prompt), Queue & Format). Backups: `PRE-FR12-*.json`.
- **PUT "unauthorized" — transient.** Two `build_fr1_fr2.py --deploy` runs
  failed with the n8n API returning `{"message":"unauthorized"}` on the PUT
  while every GET in the same run worked. Diagnostics proved the API key is
  valid for writes — an empty-body PUT/POST returned a clean `400` schema
  error (auth passed), and a no-op PUT succeeded. The FR-12 PUT then went
  through. Cause: a transient on the n8n side, not the key and not the body.
- **staticData snapshot caveat:** the successful PUT carried a draft-queue
  snapshot ~6 min old (from the build's GET). Any draft created or actioned
  in that ~15:09–15:15 window is not reflected in the queue — its Telegram
  card would be orphaned ("draft not found" on a button tap). Low-traffic
  afternoon window; flagged to the operator.
- FR-1 + FR-2 are deployed but **not yet behaviourally tested** — operator to
  test the Edit feedback loop and a `/lead` message.

### 🛠️ FR-3 DEPLOYED (2026-05-21) — message debounce (30s quiet-window)
- `scripts/build_fr3.py` inserted a 30s quiet-window into the inbound chain:
  Filter Inbound → **Buffer Message → Debounce Wait (30s) → Flush Check** → Get
  Chat History. **49 → 52 nodes.** Format Context modified to draft against the
  combined buffered messages. Backup: `PRE-FR3-*.json`.
- **Intermittent SSH connectivity:** the deploy hit repeated `ssh exit 255`
  (connect timeout to the box) — roughly 1 in 3 connections failed. The box
  itself is healthy (load 0.06, n8n `/healthz` 200 in ~1ms); the flakiness is
  the network path to it, not the box or n8n. `build_fr3.py`'s `ssh_run` /
  `ssh_upload` retry idempotent SSH calls up to 5×, which pushed the deploy
  through (resolve-ip, list, upload, PUT, activate, verify all needed retries).
  Verified: 52 nodes, active, all 3 new nodes present.
- FR-3 deployed but **not yet behaviourally tested** — operator to send two
  quick messages and confirm one combined draft card appears after ~30s.

### 🔬 HERMES REVIVAL — Phase 1 diagnosis (2026-05-21)
- `scripts/diagnose_hermes.py` ran off the live path: located the CLI (Hermes
  Agent v0.14.0), inspected `~/.hermes`, ran 6 timed `hermes chat` test drafts.
- **Finding — Hermes drafting is healthy.** Sequential drafts averaged **~13s**
  (12.8–14.9s with `-t memory`, 10.9–13.6s without), all `rc=0` with real draft
  output. The memory tool adds only **+1.5s** — negligible. No 20–50s slowness
  and no 120s timeouts were reproduced in isolation.
- `~/.hermes` is 1.5 GB, but that is the Hermes install itself (bundled node,
  claude-code, the agent repo + a 229 MB git pack). The actual `profiles` dir
  is 44 MB and `sessions` 1.4 MB — moderate, **not** the cause. The
  "profile-state bloat" hypothesis is ruled out.
- **Conclusion: there is no Hermes latency *bug*.** The rollback slowdown was a
  **critical-path / concurrency** effect — Hermes sat on the live path, so
  under concurrent customer traffic the executions piled up. The FR-5 refined
  design (Hermes OFF the critical path, as a background improver) sidesteps
  this entirely. **Phase 2 (latency fix) is largely moot;** the revival
  proceeds to Phase 3 (re-integration design).
- Side notes: the `hermes-bridge` service is actually `active` (the handoff
  recorded it stopped — it is running, just `disabled` from boot autostart;
  harmless, nothing is wired to it). `~/.hermes/skills` (8.4 MB) exists —
  relevant to FR-5's "check all skills". A few `config.yaml.bak.*` remain from
  the build (minor hygiene).

### 🛠️ HERMES REVIVAL — Phase 4 (2026-05-21) — background improver built
- **4.1 — bridge `/improve`** (`hermes-bridge/server.py`, deployed via
  `scripts/deploy_bridge.py`): given a draft + conversation, Hermes reviews it
  and returns a better version only if it can meaningfully improve it. Tested
  end-to-end — a stiff/corporate draft was rewritten into Maria's voice
  (lowercase, warm, didn't re-ask a detail already given) in ~18s,
  `improved:true`.
- **4.2 — the n8n improver branch** (`scripts/build_fr5_improver.py`, 52 → 57
  nodes): off `Save Telegram MsgID` — Improve Wait (20s) → Check Pending →
  Hermes Improve → Apply Improvement → Edit Improved Card. ~20s after a draft
  card posts, if it is still pending, Hermes improves it and the card is
  re-rendered in place with a "✨ improved by Hermes" marker. One pass per
  draft (a multi-pass loop is a later enhancement).
- **Off the critical path:** the fast direct-Claude draft posts first and is
  unaffected; the improver runs after. `Hermes Improve`'s error output is
  unwired — a slow/failed/down bridge leaves the draft as-is and breaks
  nothing. This is the structural fix for the rollback incident.
- The flaky SSH path to the box persists — the deploy scripts' 5× connection
  retry carried each deploy through.
- 4.2 deployed but **not yet behaviourally tested** on a real draft. Remaining:
  **Phase 4.3 — the learning loop** (Send/Edit/Skip → `behavior_rules`).
