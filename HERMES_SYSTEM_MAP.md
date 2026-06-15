# Hermes System Map

Hermes is the internal codename for an AI-assisted WhatsApp booking concierge that runs the customer-conversation pipeline for **Dubriani**, a Dubai yacht-charter business. It reads inbound WhatsApp messages, drafts replies with Anthropic's Claude, and posts every draft to a human operator's Telegram for one-tap approval before anything is sent — the system is **approval-first**: almost nothing auto-sends. Around that core sit lead-scoring, payment detection, identity reconciliation, a CRM read-side, and a fleet of scheduled jobs. This document maps the whole system end to end for someone who has never seen it: where it runs, how a message flows through it, every workflow and module, every flag, every scheduled job, the data stores, the known landmines, the Dubriani-specific values that would have to become per-tenant config, and the live credential status.

Last mapped: 2026-06-15 (read-only)

## Table of Contents

1. [System overview & topology](#1-system-overview--topology)
2. [n8n workflows](#2-n8n-workflows)
3. [hermes-bridge — HTTP API, pipeline & feature modules](#3-hermes-bridge--http-api-pipeline--feature-modules)
4. [Flags & environment variables](#4-flags--environment-variables)
5. [Scheduled jobs](#5-scheduled-jobs)
6. [Data stores](#6-data-stores)
7. [Known fragilities & gotchas](#7-known-fragilities--gotchas)
8. [Tenant seams (Dubriani-specific → future config)](#8-tenant-seams-dubriani-specific--future-config)
9. [Credential / OAuth-token revocation status](#9-credential--oauth-token-revocation-status)
10. [Appendix: version snapshots & repo layout](#10-appendix-version-snapshots--repo-layout)

---

## 1. System overview & topology

Hermes runs on a single AWS EC2 box and is built from a handful of cooperating services. Nothing in the stack is a managed cloud service except Anthropic, HubSpot, Nomod (payments), and Google Drive (backup); everything else is a process or Docker container on the one box.

**The box.** `ubuntu@13.63.82.112` (internal IP `ip-172-31-31-194`), reachable over SSH as the alias `dubriani-ec2`. It is an EC2 `t3.xlarge` (4 vCPU).

**The components and how they talk to each other:**

| Component | What it is | Where it listens / how it's reached |
|---|---|---|
| **hermes-bridge** ("the bridge") | A Python HTTP service (Python standard library only — no web framework). The business-logic brain: all DB reads/writes, draft storage, dedupe, quality checks, customer context, payments, lead-lifecycle. | `systemd --user` unit (`hermes-bridge.service`, linger enabled), binds `0.0.0.0:BRIDGE_PORT` (**8788**). **NOT internet-reachable** — the AWS security group blocks 8788 from the internet. n8n reaches it via the Docker bridge gateway **`172.18.0.1:8788`**. |
| **n8n** | A low-code workflow-automation tool. The live AI drafting/approval orchestrator and the timer for scheduled workflows. | Docker container; public at `https://n8n.13-63-82-112.sslip.io` (behind caddy/edge TLS). |
| **WAHA** | An unofficial WhatsApp HTTP API (WEBJS engine) that drives a real WhatsApp Web session. How the system reads/sends WhatsApp. | Docker container; `https://waha.13-63-82-112.sslip.io`. Internal port 3000 is **not** published to the host. |
| **Postgres** | Primary operational database (shared with the n8n stack). | Docker container `BRIDGE_PG_CONTAINER` (default `n8n-postgres-1`). The bridge talks to it via `docker exec ... psql` (no DB driver). |
| **Redis** | Ephemeral cache: debounce, draft queue, locks, dedup keys. | Docker container `BRIDGE_REDIS_CONTAINER` (default `n8n-redis-1`). Reached via `docker exec ... redis-cli`. |
| **caddy / edge** | Reverse proxy giving TLS to n8n + WAHA over `sslip.io`. | Fronts the public HTTPS endpoints. |

**Source of truth vs deployed copy.** The repository is the source of truth and lives on the operator's Mac at `/Users/macbook/projects/dubriani-ai-agent` (branch `catalog-consolidation-2026-06-02`). The deployed copy on the box is `/home/ubuntu/hermes-bridge/`. **The box is not a git repo** — it can drift from the repo (see Fragilities).

**The one-line data path.** Customer WhatsApp message → WAHA → webhook into n8n → n8n drafts a reply with Claude (calling the bridge for context/history/state) → n8n posts an approval card to the operator's Telegram → operator taps Send → n8n tells WAHA to send the reply to the customer. Payments, lead-scoring, and identity reconciliation all run through the bridge and its scheduled jobs.

---

## 2. n8n workflows

n8n hosts both the **live event-driven orchestrator** (`phase-1b-telegram`) and five **schedule-triggered** workflows (analyze / review / hourly-sweep / payment-poll / reanalyze). All of them offload real work to the bridge over `http://172.18.0.1:8788`; n8n is the traffic cop, the human approval gate, and the timer.

> **Jargon, defined once for this section:**
> - **WAHA** = unofficial WhatsApp HTTP API (WEBJS engine) at `https://waha.13-63-82-112.sslip.io`.
> - **hermes-bridge** ("the bridge") = the stdlib-only Python HTTP service at `http://172.18.0.1:8788`; n8n offloads all DB reads/writes, draft storage, dedupe, quality checks and context to it.
> - **Operator / Zayn** = the human who approves drafts, via a Telegram bot called "Dubriani Hermes".
> - **`@c.us` / `@lid`** = WhatsApp chat-id suffixes; `@c.us` embeds the phone number, `@lid` is a privacy-masked hash.
> - **label** = a lead's pipeline stage (`NEW`, `WARM`, `HOT`, `NEEDS_ATTENTION`, `COLD`, `WAITING_FOR_PAYMENT`, `CONFIRMED`) or a terminal stage (`LOST`, `DISREGARDED`, `SCAM`, `PAUSED_*`). **Terminal** = closed/dead; the operator must manually reopen. **cid** = customer id. **Re-touch / resurrect** = an automated job pulls a closed/cold lead back into the active pipeline or drafts fresh outbound to it.

### 2.1 Main workflow — `phase-1b-telegram`

**File (source of truth):** `workflows/phase-1b-telegram.json`. **Workflow name inside n8n:** `Dubriani Phase 1B — WhatsApp + Telegram Approval` (id `azPIy9OcDwiPV5uY`). **Size:** 202 nodes (123 HTTP Request, 46 Code, 16 IF, 6 Wait, 4 Set, 3 Switch, 2 Webhook, 1 SplitOut, 1 SplitInBatches).

This is the LIVE orchestrator. It does **not** write the reply text itself — it calls Claude to draft and calls the bridge for every piece of state, history, and business logic. It has **no direct Postgres, Redis, or supermemory calls** (a grep for "supermemory" returns 0 hits); the bridge is the only thing that touches the database.

#### Triggers — what fires this workflow

Two entry points, both `n8n-nodes-base.webhook` (POST). It is **event-driven**, not scheduled.

| Trigger node | Path | Fired by | Purpose |
|---|---|---|---|
| **`WAHA Webhook`** | `POST /webhook/whatsapp` | WAHA pushes every WhatsApp event | A WhatsApp message was sent/received → start (or interrupt) the draft pipeline |
| **`Telegram Webhook`** | `POST /webhook/telegram` | The "Dubriani Hermes" Telegram bot | The operator tapped an approval button or typed a command/reply |

#### The pipeline, in order

**Inbound WhatsApp → draft → approval card (main path).** `WAHA Webhook` fans out to three IF filters:

1. **`Filter Inbound`** — keeps genuine customer messages (`fromMe=false`, `event="message"`, non-empty text or media, `from` ends `@c.us`/`@lid`). This branch produces a draft.
2. **`Filter Op Reply`** / **`Filter Op Reply fromMe`** — the human answered the customer directly on their own WhatsApp (`fromMe=true`) → `Record Op Reply` / `Record Op Reply fromMe` (`POST /conversation-state`) so Hermes backs off.

For a real inbound, `Filter Inbound` runs this chain:

1. **Debounce** (collapses rapid-fire messages into one draft): `Debounce Buffer` (`POST /debounce action=buffer`, keyed by phone) → `Buffer Message` → `Debounce Wait` → `Debounce Flush` (`POST /debounce action=flush`) → `Flush Check` (only the winning flush proceeds; three quick WhatsApps → one Claude call).
2. **`Get Chat History`** — last 15 messages from WAHA (`GET .../chats/{from}/messages?limit=15&downloadMedia=false`).
3. **`Format Context`** builds the conversation text.
4. **`Fetch Behavioral Context`** (`POST /feedback action=behavioral-context`) — pulls returning-customer / HubSpot / behavior-rule context. In parallel, **`Cancel Prior Autosend`** clears any armed auto-send.
5. **`Build Prompt`** (Set) assembles `systemPrompt`, `userMessage`, `conversationHistory`, `customerName`, `customerPhone`, `regenHint`.
6. **`Claude AI`** (`POST api.anthropic.com/v1/messages`) — **the drafting brain.** Model `claude-sonnet-4-6`, `max_tokens:1024`. The `system` block is `systemPrompt + Fetch Behavioral Context.formatted` with `cache_control: ephemeral`. It must return only a JSON object.
7. **`Parse Response`** parses that JSON.
8. Side enrichers: **`Customer Facts`** (`/customer-facts`), **`Label Eval`** (`/label-eval`), **`Mark Customer Msg`** (`/conversation-state`).
9. **`Interrupt?`** — suppress the draft if the human took over. **`Send Sameday Alert`** can fire for urgent same-day requests.
10. **`Check Payment Trigger`** — if a "send payment link" turn, go `Generate Payment Link` (`/payment-link`) → `Build Payment Message`; else `Queue & Format`.
11. **`Queue & Format`** builds the Telegram card (text + Send/Edit/Regen/Skip buttons). **`Persist Draft`** (`POST /queue action=save`) stores the draft.
12. **`Send Draft to Telegram`** (`POST .../sendMessage`) posts the card. **`Save Telegram MsgID`** + **`Mark Draft Posted`** record the message id.
13. **Post-post quality loop:** `Improve Wait` → `Check Pending` → **`Hermes Improve`** (`POST /quality-check`) → on success `Recheck Draft Status` → `Apply Improvement` → `Improve Commit`. (`Find Break`/`Break Check` detect a conversation break; `Sync Customer MsgID To Redis` keeps Redis in step.)

**Operator decides on Telegram.** `Telegram Webhook` → **`Verify Admin`** (the security gate, see below) → **`Route Update Type`** (Switch: `callback_query` vs `message`).

- **Button taps** (`callback_query`): `Redis Draft Lookup` → `Parse Callback` → **`Route Action`** with outputs `send, skip, edit, regen, auto, takeover, feedback_yes/modify/no, nudge, snz, inf, paylink, disregard, disregard_force, file, editfb, editrule`. Key branches: **send** → `Check Freshness` (`/draft-freshness`) → `Send One to Customer` (WAHA `sendText`) → `Mark Sent` + `Edit Telegram (Send)`; **regen** → `Build Regen Prompt` → `Claude AI (Regen)` → `Parse Regen` → re-post; **edit** → `Set Awaiting Edit` → `Edit Telegram (Edit Prompt)`; **skip** → `Mark Skipped`; **paylink** → `Prep Paylink` → `Call Nomod` (`/payment-link`) → `Nomod OK?` → `Send Link to Customer`.
- **Typed text** (`message`): `Process Text Reply` → **`Route Text Action`**: `send_to_customer` (verbatim send), `ack`, `refine` (→ `Claude AI (Refine)`), `new_lead` (→ `Claude AI (Lead)`), `rules_cmd` (`/rules`), `mode_cmd` (`/set-mode` — let-it-run / take-back / pause / `/manual`), `caps_cmd`, `feedback_cmd`, `review_cmd` (`/review`), `info_cmd`, `label_cmd`, `snooze_cmd`, `refresh_cmd` (`/refresh-facts`), `paylink_amount`, `assist_query` (`/assist`).

**Optional autonomous auto-send (gated, usually off).** If the conversation's mode is "autonomous": `Auto Prep` → `Auto Gate` (`/autosend-check commit:false`, a dry-run cap check) → `Auto Is Autonomous` → `Arm Autosend` → `Render Auto Card` → `Auto Wait` (veto delay) → `Get Autosend` → `Auto Decide` → `Auto Commit` (`/autosend-check commit:true`) → `Auto Send Gate` (sends only if `auto_send===true`) → `Auto Send WAHA`. Errors → `Auto Send Failed` → `Edit Auto-Sent Card`.

#### What it READS

| Source | How | Examples |
|---|---|---|
| WhatsApp history | WAHA `GET .../chats/{id}/messages?limit=15` | `Get Chat History` |
| Anthropic Claude | `POST api.anthropic.com/v1/messages` (`claude-sonnet-4-6`) | `Claude AI`, `Claude AI (Refine/Regen/Lead)` |
| hermes-bridge (read-side) `http://172.18.0.1:8788` | POST endpoints | `/feedback` (behavioral-context), `/customer-facts`, `/draft-freshness`, `/queue get`, `/quality-check`, `/autosend-check` (commit:false), `/label-eval`, `/caps`, `/review`, `/info`, `/conversation-state` (read) |

#### What it WRITES

| Target | How | Examples |
|---|---|---|
| Draft store / dedupe | bridge `/queue action=save`, `/debounce` | `Persist Draft`, `Debounce Buffer/Flush`, Redis sync |
| Telegram (operator) | `sendMessage` (27 nodes) / `editMessageText` (17) / `editMessageReplyMarkup` / `answerCallbackQuery` to chat `5532831477` | `Send Draft to Telegram`, all `Edit Telegram (*)`, `Alert Zayn` |
| WhatsApp (customer) | WAHA `POST /api/sendText` (session `default`) | `Send One to Customer`, `Send to Customer (Manual)`, `Auto Send WAHA`, `Send Link to Customer` |
| Conversation / business state | bridge POSTs | `/conversation-state`, `/set-mode`, `/label`, `/snooze`, `/learn`, `/edit-rule`, `/edit-feedback`, `/edit-capture`, `/autonomous-log`, `/followup-action`, `/record-message`, `/payment-link` |

#### Where it can FAIL (node-level)

- **Anthropic outage / bad JSON.** `Claude AI` (and Refine/Regen/Lead) use `onError: continueErrorOutput` → error output goes to `Build Alert → Alert Zayn`. But `max_tokens` is only 1024; a long reply can be truncated and `Parse Response` (on the *success* path) may choke on incomplete JSON.
- **Telegram down.** `Send Draft to Telegram` error → `Build Alert` (which itself can't deliver if Telegram is down).
- **WAHA send fails.** `Auto Send WAHA` → `Auto Send Failed` → `Edit Auto-Sent Card`. Manual `Send One to Customer` has `continueErrorOutput`.
- **Quality loop swallows its own failure.** `Hermes Improve` has `onError: continueErrorOutput` but its error output is **wired to nothing** — a quality-check failure silently stops the improvement loop and the original draft stands (see Fragilities).
- **Bridge unreachable.** Everything except Claude/WAHA/Telegram depends on `172.18.0.1:8788` (intentionally not internet-reachable; no failover). Many bridge nodes use `onError: continueRegularOutput`, so a failed call flows down the **success** path carrying an error object instead of stopping (see Fragilities).
- **Admin gate degradation.** `Verify Admin` enforces the Telegram `x-telegram-bot-api-secret-token` header **only if** `TELEGRAM_WEBHOOK_SECRET` is set; if lost, it silently falls back to checking `from.id` against the hardcoded admin id `5532831477`.
- **Debounce coupling.** If the bridge's buffer/flush bookkeeping and n8n's `Debounce Wait` drift, messages can be merged wrongly or a flush lost.

> The committed `workflows/*.PRE-*.json` files (e.g. `PRE-DEBOUNCE_WINDOW`, `PRE-PAYLINK-SOURCE`, `PRE-TGSECRET`, dated `PRE-DEPLOY-*`) are timestamped pre-deploy backups, not live workflows. Only `phase-1b-telegram.json` (no `PRE-` suffix) is live. `docs/hermes-architecture.md` still describes a stale 51-node version (see Fragilities).

### 2.2 Scheduled workflows (analyze / review / hourly-sweep / payment-poll / reanalyze)

Each has an n8n **Schedule Trigger** that fires on a timer, then POSTs to the bridge and/or Telegram. Shared facts across all five:

| Thing | Value (hard-coded in every file) | Meaning |
|---|---|---|
| Bridge base URL | `http://172.18.0.1:8788` | Docker bridge-gateway IP for the bridge |
| Bridge auth | n8n credential `IgIcvPoibuAayVDx` ("Hermes Bridge"), HTTP header auth | Shared secret header on every bridge call |
| Telegram target | `chat_id: 5532831477` | The single operator's Telegram chat |
| Telegram auth | `{{ $env.TELEGRAM_BOT_TOKEN }}` | Bot token from the n8n environment |
| Owner project | "Zayn Kazemi `<z@ynkazemi.nl>`" (projectId `cW9yfaMW5jqMcI5f`) | n8n personal project owning these workflows |

#### Payment Poll Cron (`payment-poll-cron.json`) — every 2 min

- **Purpose:** Nomod issues no webhooks, so this pulls recent charges and reconciles them. Matched real deposits flip the lead to `CONFIRMED`; everything is announced to the operator.
- **Flow:** `Schedule (2 min)` → `POST /poll-payments {window_hours:24, page_size:50}` (timeout 30 s) → `Anything New?` → `Split Matched`/`Notify Matched` + `Split Unmatched`/`Notify Unmatched` (both `onError: continueRegularOutput`).
- **Reads:** recent Nomod charges; `autonomous_sends` (match by `link_id`, then amount+time); `customer_facts`/label row; WAHA chat cache for payer-vs-customer mismatch.
- **Writes:** `payment_received` / `payment_received_unmatched` rows; Redis dedup `nomod_seen:<charge_id>` (TTL 30 d); `apply_label_transition(... → CONFIRMED)` for matched real deposits; `_reconcile_paid_unconfirmed()` every poll.
- **Guards:** only `status==paid` in window; Redis dedup per charge; below `CONFIRM_PROMOTION_MIN_AED` → logged only; payer-mismatch → logged not confirmed; prefix-anchored phone resolver returns "unmatched" on ambiguity.
- **Failure / re-touch:** Promotion fires for **any** `prev_label != "CONFIRMED"` — including `LOST`/`DISREGARDED` (payment-gated, intended, but bypasses terminal state). Telegram failures are swallowed (a payment can be confirmed in DB while the alert is lost). Unset `TELEGRAM_BOT_TOKEN` → both notify branches no-op.

#### Pipeline Analyze Cron (`pipeline-analyze-cron.json`) — every 1 h

- **Purpose:** hourly importance ranker; runs the Hermes LLM (`hermes_analyze_lead`) per active lead, stores score + reasoning + suggested action. Only pings Telegram for not-convertible leads.
- **Flow:** `Schedule (hourly)` → `POST /pipeline-analyze {cap:20}` (timeout **600 s**) → `Has Summary?` → `Notify Operator`.
- **Reads:** `customer_facts` where `label IN (NEW,WARM,HOT,NEEDS_ATTENTION,COLD,WAITING_FOR_PAYMENT,CONFIRMED) AND merged_into IS NULL`, stalest-first (by `importance_analyzed_at`, or `last_analyze_attempt_at` when `SWEEP_ATTEMPT_ROTATION_ENABLED=1`), limit cap (node passes 20; `PIPELINE_ANALYZE_CAP` default 24).
- **Writes:** `customer_facts.importance_score/reasoning/suggested_action/analysis_unreliable/importance_analyzed_at` (+`last_analyze_attempt_at`); on `close` verdict or score 0 may `apply_label_transition(... → COLD)` (`auto:analyzer_close`/`auto:analyzer_score0`); per-lead `🛑 Disregard` cards (deduped via `closecard:<cid>`, 20 h).
- **Guards:** skipped outside UAE working hours unless `force=true`; Redis NX lock `lock:pipeline_analyze` (EX 2400 s = 40 min); auto-COLD blocked for won/in-flight/paid, unreliable analyses, recent manual overrides, unverifiable passed-date, label locks.
- **Failure / re-touch:** the n8n 10-min timeout is **shorter than the 40-min bridge lock** → silent gaps. `CONFIRMED` is in the population (re-scored hourly, won/paid guard blocks demotion). Auto-COLD can demote a live lead on a wrong analysis (mitigated, not eliminated).

#### Pipeline Hourly Sweep (`pipeline-hourly-sweep.json`) — `0 * * * *`

- **Purpose:** deep label re-evaluation with skip-if-unchanged, plus cold-decay. Contains a legacy follow-up branch that is **self-disabled** by default.
- **Flow:** `Schedule (Hourly)` → `POST /hourly-sweep {}` (timeout 60 s) → branch A `Transitions > 10?` → `Send Sweep Alert`; branch B `Split Followups` → `Hermes Draft Followup` (`/draft-followup`) → `Build Followup Card` → `Persist Followup` (`/queue save`) → `Send Followup Card` → `Save Followup MsgID` (`/queue update`) → `Log Followup Drafted` (`/followup-action drafted`).
- **Reads:** `conversation_state` rows newer than `last_analyzed_at`, stalest-first, limit `HOURLY_SWEEP_BATCH_LIMIT` (default 200).
- **Writes:** `conversation_state.last_analyzed_at/last_analysis_signal/confidence`; `apply_label_transition` for cold-decay (`→ COLD`, `hourly_sweep:cold_decay`) and re-eval; a disk-space alert check.
- **Guards:** terminal labels + `label_locked_until` skipped; `_should_cold_decay` refuses to decay never-messaged leads (NULL `last_customer_message_at`) and in-flight stages; sticky-upward guard stops weak re-eval demoting a HOT lead.
- **Follow-up branch is OFF by default:** bridge returns `eligible_followups: []` unless `HOURLY_SWEEP_FOLLOWUPS=1`. The canonical re-engage owner is the separate `*/30` `/followup-sweep` cron. The two collided (one customer got two cards), which is why this branch was disabled.

#### Pipeline Review Cron (`pipeline-review-cron.json`) — `0 5 * * *` and `0 13 * * *`

- **Purpose:** twice-daily pipeline digest = the operator's `/review` report (header + one card per lead with inline buttons). The only file with `"active": true` and two triggers into one chain.
- **Flow:** either schedule → `POST /review {mode:"scheduled"}` (timeout 90 s, `onError: continueRegularOutput`) → `Send Review` (header) + `Split Lead Cards` → `Send Lead Card`.
- **Reads:** `v_lead_summary`/`customer_facts` (excludes `merged_into`), scored/sorted.
- **Writes (NOT purely read-only):** an **auto-heal pass** calls `refresh_customer_facts_from_waha(...)` for broken-intake and stale high-value leads (capped at `REVIEW_INLINE_REFRESH_CAP`) — writes `customer_facts`; `mark_review_seen(...)` writes `last_review_seen_at`; on the 1st of the month prepends a monthly edit-learning digest.
- **Failure:** the "09:00/17:00 Dubai" node labels are correct **only if the n8n instance TZ is UTC** (Dubai = UTC+4); if TZ is `Asia/Dubai` they fire at 05:00/13:00 Dubai (4-hour drift, no other signal). The WAHA auto-heal adds latency + write side-effects to a "reporting" job.

#### Reanalyze Queue Cron (`reanalyze-cron-...PRE-RENAME-20260610_033130.json`) — every 5 min

> Pre-rename export. Its name says **"Dubriani Reanalyze Queue Cron (DISABLED — enable while watching box load)"** yet the JSON shows `"active": true` and an `"event": "activated"` publish entry — name and flag disagree (see Fragilities).

- **Purpose:** on-demand re-analysis. Every fresh inbound runs `_enqueue_reanalyze(cid)` onto a Redis queue (`hermes:reanalyze:queue`, deduped per-cid for `REANALYZE_DEDUP_TTL`, default 30 min); this cron drains it so a lead refreshes within minutes.
- **Flow:** `Schedule (5 min)` → `POST /pipeline-analyze {source:"reanalyze"}` (timeout 600 s). No Telegram node in the workflow itself.
- **Reads/writes:** same `_analyze_one` pipeline as Pipeline Analyze, but drains the Redis queue (not stalest-first), smaller cap (`REANALYZE_CAP`, default 12), runs **anytime** (no working-hours gate), shares `lock:pipeline_analyze`.
- **Queue guards:** `_filter_reanalyze_cids` canonicalizes merges and keeps only `merged_into IS NULL AND label IN (NEW,WARM,HOT,NEEDS_ATTENTION,COLD,WAITING_FOR_PAYMENT,CONFIRMED)`; fails closed on DB error. With `REANALYZE_DEFER_ENABLED=1`, `_promote_deferred_reanalyze` re-surfaces cooldown-suppressed cids.
- **⚠️ Re-touch / resurrect — the main seam:** the queue is fed by **every** inbound, and the filter drops only *terminal* labels (`LOST`,`DISREGARDED`,`SCAM`,`PAUSED_*`) and merged rows — **`COLD` is not terminal.** A lead the analyzer previously demoted to `COLD` (signal `auto:analyzer_close`/`auto:analyzer_score0` — effectively *declined*) can be re-queued, re-scored, re-promoted to `WARM`/`HOT`, and re-surfaced in `/review` on a single new inbound, with no operator gate. It can also fire `🛑 not-convertible` disregard cards **at any hour** (only `closecard:<cid>` 20-h dedup), the same quiet-hours exposure as past 4am-Dubai mis-sends.

#### Cross-workflow resurrection / re-touch summary

| Workflow | Can it re-touch / resurrect a closed/declined/lost lead? |
|---|---|
| Payment Poll (2 min) | **Yes** — a matched real deposit promotes any non-`CONFIRMED` lead (incl. `LOST`/`DISREGARDED`) to `CONFIRMED`. Payment-gated, intended. |
| Pipeline Analyze (hourly) | Closes-direction only (auto-COLD on LLM close/score-0); re-scores `CONFIRMED` (writes, no demote). Doesn't pull terminal leads back. |
| Hourly Sweep | Follow-up branch **off by default**; if `HOURLY_SWEEP_FOLLOWUPS=1` drafts nudges to silence-`COLD` leads (excludes terminal + analyzer-closed). Cold-decay closes-toward-cold. |
| Pipeline Review (2×/day) | No label changes; writes facts via auto-heal + `last_review_seen_at`. |
| **Reanalyze (5 min)** | **Yes (main seam)** — fed by every inbound; filter drops terminal but **not `COLD`**, so an analyzer-declined-then-`COLD` lead can be re-promoted to active on a single inbound. |

---

## 3. hermes-bridge — HTTP API, pipeline & feature modules

The bridge is the always-on Python service that holds all business logic. On 2026-06-15 it was confirmed running as a `systemd --user` service (`active` + `enabled`), **PID 1341215**, listening on `BRIDGE_PORT`. This section covers its HTTP/data layer (3a), the message-intake → draft → payment → lead-lifecycle pipeline (3b), and the optional feature modules (3c).

### 3a. HTTP API & data layer

A small Python HTTP service (Python standard library only — no web framework) sitting between n8n, the Hermes-LLM CLI, Postgres, Redis, WAHA, Telegram and Nomod. It is a `systemd --user` service (`hermes-bridge.service`), `ExecStart=/usr/bin/python3 ~/hermes-bridge/server.py`, config from `~/hermes-bridge/.env` (`EnvironmentFile`). It binds `0.0.0.0:8788`; the AWS security group blocks 8788 from the internet, so it is reachable only from inside the box (n8n via `172.18.0.1:8788`). The web server is `http.server.ThreadingHTTPServer` with a custom `Handler(BaseHTTPRequestHandler)` (one thread per request, HTTP/1.1 keep-alive).

**Code layout (four files in scope):**

| File | Lines | Role |
|---|---|---|
| `server.py` | 5,219 | Process entry point, the `Handler` class (routing/auth), Hermes-LLM call helpers, ~half the business logic. Route → function table lives in `Handler.do_POST`. |
| `routes.py` | 8,347 | The bulk of the HTTP handlers (`handle_*`), imported into `server.py`. |
| `db.py` | 64 | Postgres + Redis primitives (`_psql`, `_redis`, `_lit`). |
| `util.py` | 43 | Leaf helpers: `log`, `_envflag`, `_md_escape`. |

Import direction is one-way (`util` ← `db` ← `server`/`routes`). Handlers are wired in two hand-maintained places that must stay in sync: an **allow-list tuple** in `do_POST` (404s unknown paths) and the long `if/elif` **dispatch chain**.

#### HTTP endpoints

One GET route and a fixed set of POST routes. Any allow-listed-but-unmatched path falls through to `handle_draft` (the `else` branch).

| Method | Path | Purpose | Reads | Writes |
|---|---|---|---|---|
| GET | `/health` | Liveness — `{status: ok}` | — | — |
| POST | `/draft` | Build context, ask Hermes LLM for a WhatsApp draft (default/fallback route) | `customer_facts`, `behavior_rules`, durable conversation, HubSpot | `draft_log`, `conversation_health`, `customer_triggers`, Redis `draft:`/`score:` |
| POST | `/draft-gated` | Draft via Anthropic, then SCORE against a quality gate; fail-open to n8n's draft | `behavior_rules` | Redis draft cache |
| POST | `/draft-followup` | Proactive follow-up draft via Hermes | lead context | Redis draft cache |
| POST | `/draft-freshness` | Has a stored follow-up draft gone stale? | Redis `draft:`, `conversation_state` | — |
| POST | `/improve` | Review + rewrite a draft (background) | `behavior_rules` | — |
| POST | `/quality-check` | Fast quality SCORE (no rewrite) | `behavior_rules` | Redis `score:` |
| POST | `/learn` | Judge operator feedback, capture an (inactive) rule | LLM judge | `behavior_rules` |
| POST | `/rules` | List/activate/discard pending rules | `behavior_rules` | `behavior_rules` |
| POST | `/save-rule` | Persist a rule directly | — | `behavior_rules` |
| POST | `/edit-rule` | Operator decision on a pattern-detected rule | Redis `rulesuggest:` | `behavior_rules` |
| POST | `/feedback` | `/feedback` command — classify, save, return behavioural context | `behavior_rules` | `behavior_rules` |
| POST | `/edit-capture` | Store an operator edit-delta | — | `edit_corrections` |
| POST | `/edit-feedback` | Feedback-prompt interactions (P2) | — | `edit_corrections` |
| POST | `/autosend-check` | Mode + caps decision for an autonomous draft (FR-4); quality floor | `conversation_modes`, `autonomous_sends`, Redis | — |
| POST | `/autosend-state` | Redis autonomous-send arm/disarm/countdown | Redis `autosend:` | Redis `autosend:` |
| POST | `/set-mode` | Set conversation mode / manual kill-switch | — | `conversation_modes`, Redis `manual:` |
| POST | `/caps` | Autonomous-mode cap status text | `autonomous_sends` | — |
| POST | `/autonomous-log` | Generic event row (always 200) | — | `autonomous_sends` |
| POST | `/followup-action` | Log a follow-up event (always 200) | — | `autonomous_sends` |
| POST | `/customer-facts` | Maintain `customer_facts`, return context header | `customer_facts` | `customer_facts` (UPSERT) |
| POST | `/refresh-facts` | Pull WAHA history, re-run fact extraction (on-demand) | WAHA history | `customer_facts` |
| POST | `/label` | Manual label override + correction record | `customer_facts` | `customer_facts`, `label_corrections`, `customer_label_history` |
| POST | `/label-eval` | Silent per-message label updater + same-day interrupt (fail-open) | `customer_facts` | `customer_facts`, `customer_label_history` |
| POST | `/snooze` | Set `label_locked_until` without changing the label | — | `customer_facts` |
| POST | `/name` | Operator rename — sets and LOCKS the name | — | `customer_facts` (name + `name_locked`) |
| POST | `/conversation-state` | Silent timestamp updater | — | `conversation_state` |
| POST | `/record-message` | Fail-safe writer for the durable conversation store | — | `conversation_messages` |
| POST | `/ask-operator` | Store awaiting-info state, ask the operator (fail-open) | — | `conversation_state` |
| POST | `/answer-info` | Operator answered an `/ask-operator` question (fail-open) | `conversation_state` | `conversation_state` |
| POST | `/debounce` | Redis inbound debounce (FR-3) | Redis | Redis debounce key |
| POST | `/queue` | Redis pending-draft queue (push/list/clear) | Redis `drafts:active` | Redis `draft:`/`drafts:active` |
| POST | `/review` | Read `v_lead_summary`, score, render the Telegram review | `v_lead_summary` | Redis card state |
| POST | `/info` | Single-lead dossier (read-only) | `customer_facts`, history | — |
| POST | `/assist` | Free-text operator assistant (LLM) | various | — |
| POST | `/hourly-sweep` | Deep re-analysis with skip-if-unchanged | active leads | `customer_facts`, Redis `lock:`/`hourly_sweep:` |
| POST | `/pipeline-analyze` | Hourly importance ranker (stalest first) | active leads | `customer_facts.importance_*`, Redis `lock:pipeline_analyze` |
| POST | `/dormancy-sweep` | Mark dormant leads + graceful close | leads | `customer_facts`, Redis `lock:dormancy_sweep` |
| POST | `/daily-feedback-sweep` | Confirmed bookings → queue feedback draft | leads/bookings | Redis `lock:feedback_sweep` |
| POST | `/followup-sweep` | Proactive ghost-recovery follow-up | leads | `autonomous_sends`, Redis `lock:followup_sweep` |
| POST | `/owe-reply-sweep` | Proactive reminder for customers we owe a reply | leads | Redis `lock:owe_reply_sweep`, `owe:hourcap` |
| POST | `/lead-analyze-disregard` | Operator pressed [Disregard] | — | `customer_facts`, Redis `disregard:` |
| POST | `/reconcile-identities` | Merge `@lid`/`@c.us` duplicates | `customer_facts` | `customer_facts` (merge), Redis |
| POST | `/payment-link` | Create a Nomod link for a confirmed booking | `customer_facts` | external Nomod API |
| POST | `/poll-payments` | Fetch Nomod charges, match, notify (cron fallback) | external Nomod API | Redis `nomod_seen:`, Telegram/WAHA notify |
| POST | `/nomod-webhook` | Real-time Nomod webhook (svix HMAC; the only token-free endpoint) | external | Redis `nomod_webhook_seen:`, `customer_facts`, notify |
| POST | `/send-file` | Send a registered Google Drive file via WAHA | file registry | Redis `filesend:` |
| POST | `/list-files` | Return the file registry (read-only) | file registry | — |

#### How it connects to Postgres + Redis

Both go through **`docker exec`** — no Postgres driver, no Redis client library. `db.py`:

- **Postgres** (`_psql(sql, timeout=12)`): `docker exec <PG_CONTAINER> psql -U <PG_USER> -d <PG_DB> -tA -c "<sql>"`, returns `(stdout, err_or_None)`. `-tA` = tuples-only, unaligned (raw `\n`-separated, `|`-delimited rows split by hand). Inside the container, `psql` uses Postgres **trust auth** over the local socket (no password). Defaults: `n8n-postgres-1`, user `hermes_rw`, db `n8n`.
- **Redis** (`_redis(args, timeout=8)`): `docker exec <REDIS_CONTAINER> redis-cli <args...>`. Default `n8n-redis-1`. Used for debounce, draft cache/queue (`draft:`, `drafts:active`), autosend arming (`autosend:`), cron mutexes (`lock:*`), webhook/payment dedup (`nomod_seen:`, `nomod_webhook_seen:`).

Rationale (per `db.py`): a sudo-free user service with no native deps, "zero credential plumbing." Trade-off: **~50–150 ms per call** (every query forks a `docker exec`).

**SQL escaping.** Queries are **string-interpolated**, not parameterised. The only defence is `_lit(v)` (single-quote wrap + double embedded quotes; `None`/`""` → `NULL`). The caller is responsible for wrapping every customer-derived string — forgetting it anywhere is a latent SQL-injection / query-break risk.

**The "read-only SQL guard."** There is **none at the transaction level** (no `SET TRANSACTION READ ONLY`, no allow-list/parser). Write safety relies on **Postgres role GRANTs outside this repo**: `hermes_rw` is "scoped to the 3 Hermes tables," has **no DELETE grant**, and is **not the table owner** (can't `ALTER`/`ADD COLUMN`). The bridge writes ~a dozen tables (`customer_facts`, `conversation_state`, `conversation_messages`, `conversation_health`, `autonomous_sends`, `behavior_rules`, `conversation_modes`, `customer_label_history`, `label_corrections`, `edit_corrections`, `customer_notes`, `customer_triggers`, `draft_log`). `_ensure_schema` is a best-effort startup probe that only **logs a warning** if migration columns are missing.

#### Authentication (`BRIDGE_TOKEN`)

- A single static shared secret, `BRIDGE_TOKEN`. If empty, `main()` logs `FATAL` and refuses to start (`sys.exit(1)`).
- `do_GET` is unauthenticated — only `/health`; everything else GETs `404`.
- `do_POST` requires header **`X-Bridge-Token`** == `TOKEN` for every endpoint except `/nomod-webhook`; mismatch → `401`. The comparison is plain `!=` (not constant-time).
- **`/nomod-webhook`** authenticates with **svix HMAC-SHA256**: required `svix-id`/`svix-timestamp`/`svix-signature`, ±5-min freshness window, `base64(HMAC-SHA256(key, "<id>.<ts>.<raw_body>"))` compared with `hmac.compare_digest` (constant-time; multiple sigs for rotation). Key = `base64decode(NOMOD_WEBHOOK_SECRET stripped of "whsec_")`. Body dispatched from **raw bytes before `json.loads`**. Missing/bad sig → `400`; unconfigured/malformed secret → `500`.

The token gates *who can call*; there is no per-endpoint authorisation or per-tenant scoping.

#### Failure points

- **Per-call `docker exec` latency / process pressure** (~50–150 ms; loop-heavy endpoints + one-thread-per-request can multiply this).
- **No SQL parameterisation** — safety hinges on every caller remembering `_lit()`.
- **Write safety is external** — blast radius = whatever `hermes_rw` GRANTs are on the live DB.
- **Trust auth** — password-less; anyone who can `docker exec` into PG is effectively `hermes_rw`.
- **Dual route lists can drift** — a path in the tuple but not the chain silently routes to `handle_draft`; in the chain but not the tuple is unreachable.
- **Background work can be lost** — `/nomod-webhook` ACKs 200 then processes in a daemon thread; a restart between ACK and completion drops it (idempotency depends on Redis dedup).
- **Schema check only warns** — an un-migrated/DR-rebuilt env starts anyway; fact-UPSERTs then fail per-inbound.
- **Single shared token, non-constant-time compare** — no rotation story, no per-caller identity (lower risk because the port is internal).
- **`_send` swallows write errors** — broken-pipe/connection errors during the response are ignored.

### 3b. Message intake, drafting, payments & the lead-lifecycle state machine

This traces a WhatsApp message from arrival through draft, payment detection, and the lead-lifecycle state machine. Key modules:

| File | One-line purpose |
|---|---|
| `waha.py` | Talks to WhatsApp via WAHA: chat history, sends, identity resolution. |
| `intake.py` | Safety net detecting inbound messages the system *missed* ingesting. |
| `hermes_calls.py` | Helpers for shelling into the `hermes` CLI (a drafting/analysis engine). |
| `payments.py` | Talks to Nomod: create links, poll paid charges, flag wrong-payer. |
| `labels.py` | The lead-lifecycle state machine vocabulary + pure transition helpers. |

#### Inbound message intake

**Main path (WAHA → n8n → bridge).** WAHA fires a webhook into n8n, which drafts the reply and calls back into the bridge to record/update state. Three bridge functions matter:

- **`record_message(customer_id, direction, body, ...)`** — appends every message to a durable store (so analysis/drafting no longer depend on a live WAHA fetch, measured 8% empty / 17% degraded). Deliberately *fail-safe* (swallows errors, returns `False`). Dedup in SQL (`ON CONFLICT DO NOTHING` on the provider message id).
- **`_persist_inbound_durable(...)`** — "Root B Step 3": makes a live inbound visible instantly. LIVE (`ROOT_B_PERSIST_ENABLED=1`).
- **`behavioral_context(customer_id)`** — assembles the context blob the n8n drafter reads (history, prior facts, capacity, returning-customer signal from HubSpot). The seam where the bridge influences the n8n draft *without editing n8n*.

**`waha.py` — the WhatsApp client.** Reads chat lists/history/push-names/the `@lid`→phone map; writes outbound text/images/files. Notable:

- **Self-healing container IP (`waha_base`).** WAHA's port 3000 isn't published, so the bridge reaches it by internal Docker IP, which Docker reassigns on recreation (a 2026-05-30 incident drifted `.4`→`.5` while the bridge silently drafted with no history). Fix: resolve via `docker inspect`, cache 60 s, re-resolve on failure. An explicit `WAHA_BASE` is honored verbatim.
- **Send-boundary scrub (`scrub_outbound`).** Strips internal operator notes and the operator name ("Zayn") from every customer-facing send (root-caused to a 2026-06-06 caption leak). Also rewrites any non-canonical Google Maps pin to the canonical Dubai Harbour drop-off pin (`_canonicalize_pins`).
- **Identity resolution.** `phone_for_cid` / `lid_to_cus` / `_get_lid_phone_map` / `lids_for_phone` map `@lid` hash ↔ `@c.us` phone via WAHA's LID endpoint — the only reliable path (the `@lid` local part is a hash, not a number).
- **History shaping (`waha_fetch_history`).** Last ~20 non-empty messages as `Dubriani (3h ago): "..."` / `Customer (5m ago): "..."`. A forwarded message (`_waha_is_fwd`) is tagged "Dubriani FORWARDED 3rd-party content" (a 2026-06-12 bug scored a forwarded captain 95/100).
- **Failure:** every call degrades to `(None, err)` rather than raising — a WAHA outage produces a blind draft, not a crash. Self-heal needs `docker inspect` access; without Docker it falls back to a possibly-stale env value.

**`intake.py` — the silent-drop detector (safety net).** Pure logic (no DB/network; I/O lives in `cron-intake-gap.py`). Catches leads that reached WAHA but never landed in customer records (webhook miss, or a dropped n8n pipeline — e.g. a 2026-06-07 incident where the Claude node 400'd on a credit-balance error and skipped the record upsert). `intake_gaps(chats, cf_ids, now_ts, ...)` cross-references the WAHA overview against ingested ids. Hardening: **identity-aware membership** (`canon`/`canon_phone`) and **no age cap by default** (`max_age_days=None`). The companion cron applies phantom-card suppression (`INTAKE_PHANTOM_SUPPRESS_ENABLED` ON, `INTAKE_PHANTOM_STALE_DAYS`=7) — but those flags are read by the *separate* cron process, **not** the bridge, so they fall to code defaults there.

#### Draft production (three ways)

| Path | Engine / model | When it runs | Defined in |
|---|---|---|---|
| **n8n "Claude AI" node** (LIVE primary drafter) | Anthropic via "Dubriani Phase 1B" | Every normal inbound | Outside this repo; reads `behavioral_context()` |
| **Bridge direct Anthropic** | `claude-sonnet-4-6` over raw HTTPS | The ≥8 quality gate + fast "Draft message" button + regen loops | `routes.py` (`_anthropic_draft`, `_anthropic_score`, `_gate_loop`) |
| **Hermes CLI** | local `hermes` (`~/.local/bin/hermes`) | Background analysis, fact extraction, feedback classification, scoring | `hermes_calls.py` |

- **`hermes_calls.py`.** `run_hermes(query, ..., priority=...)` runs `hermes chat -q <query>` in non-interactive "YOLO" mode, returns `(rc, stdout, stderr, elapsed_ms)`. `extract_json` pulls the outermost `{...}`; `extract_session` recovers `session_id` so `/improve` can resume. **Two-lane concurrency:** global semaphore `_HERMES_TOTAL` (default 3, `BRIDGE_HERMES_CONCURRENCY`) + a background semaphore `_HERMES_BG` one slot below total (lock order BG→TOTAL). Failure: 120 s timeout, unparseable JSON, queue wait bounded only by the caller's n8n timeout.
- **Bridge direct Anthropic + caching (`routes.py`).** `_anthropic_draft(...)` shapes one Messages-API call like the n8n node. **Prompt-caching ("Lever 1a"):** the large static system prompt is a `cache_control: ephemeral` block (byte-identical prefix → attempts 2+ read it at ~0.1× price); volatile content (history, newest message, regen hint) goes *after*. `_log_anthropic_usage` logs `cache_creation_input_tokens` vs `cache_read_input_tokens`. `_anthropic_score` mirrors this (~19K static scorer prefix cached). `_gate_loop` = draft → score → feed flags back → regenerate until ≥ threshold (rule: "every draft 8+") or `max_attempts` (3). `DRAFTER_PRICE_STRICT_ENABLED=1` enforces the corrected price-opener (Satoshi "2,000 opener" fix). `_join_draft_parts` tolerates bare-string and `{"role","text"}` bubbles; a missing `text` → empty → triggers `_fact_anchored_fallback`.
- **`labels.py` shapes the draft** via `behavioral_context()` with no n8n change: `_party_size_fit_line` (capacity constraint; for parties <10 the capacity number is kept silent), `_clean_message_bubbles` (drops `False`/`None`/numbers and literal `false/true/none/null` — a 2026-06-02 incident sent a literal "false" bubble), `_fact_anchored_fallback` (warm placeholder when the drafter returns nothing).
- **Failure across drafting:** the model id `claude-sonnet-4-6` is hardcoded in both `_anthropic_draft` and `_anthropic_score`; a WAHA outage starves the prompt of history; the n8n path and bridge-direct path are separate code (a guard added to one is not in the other).

#### Payments (`payments.py` + Nomod) → promotion to CONFIRMED

Payments are LIVE (`PAYMENTS_ENABLED=true`, `PAYLINK_PRICE_GATE_ENABLED=1`).

- **Creating a link.** `nomod_create_link(amount, summary, customer_name)` POSTs `/v1/links`. Currency hard-coded **AED** (never from the model), title `Dubriani Yachts — <name>`, custom User-Agent `DubrianiHermesBridge/1.0` (Nomod's Cloudflare WAF 403s the default Python UA).
- **Two detectors, one dedup** (`nomod_seen:<charge_id>`, TTL 30 d): (1) **Poll** (`handle_poll_payments`) every ~2 min via `nomod_list_recent_charges`; (2) **Real-time webhook** (`handle_nomod_webhook`) svix HMAC then ACK 200 + background thread. If `NOMOD_WEBHOOK_SECRET` (must start `whsec_`) is unset, the endpoint returns **500 on every event** and the system relies on the poll.
- **3-layer matching:** (1) `link_id` → the `payment_link_sent` row; (2) phone (`resolve_customer_by_phone`, prefix-anchored, ambiguity-safe); (3) amount+time (an autonomous-send row within ±1 AED and −30min/+5min, exactly one hit).
- **Two guards before CONFIRMED:** **Deposit floor** (`is_real_deposit`, `CONFIRM_PROMOTION_MIN_AED=500` default — a 2026-05-26 1-AED test wrongly auto-promoted Qurbani); **payer mismatch** (`_payer_mismatch` compares payer digits vs `@c.us` digits / stored phone-name / WAHA push-name; returns "no mismatch" when nothing to compare). Promotion via `apply_label_transition` only when real AND not mismatched AND not already CONFIRMED; on promotion `booked_yacht` is filled from the unambiguous discussed-yachts accumulator, and `PAYMENT_TRIGGERS_REANALYSIS_ENABLED` (**LIVE=1**, even though code default off) fires `_reanalyze_on_payment` bypassing the 30-min cooldown.
- **Self-heal:** after every poll, `_reconcile_paid_unconfirmed()` promotes any canonical customer with a real deposit but non-CONFIRMED label (excludes refund/chargeback, **respects `label_locked_until`** and payer-mismatch — both added after it re-promoted a force-disregarded customer 3×).
- **Failure:** unconfigured webhook secret silently disables real-time detection; the payer-mismatch guard is weakest when the matched customer has a real (non-phone) display name; Nomod errors degrade to `(None, err)`, logged not retried.

#### The lead-lifecycle / label state machine (`labels.py`)

**States:**

| Label | Meaning | Terminal? |
|---|---|---|
| `NEW` | Just created; pre-qualification. Never proactively nudged. | No |
| `COLD` | Gone quiet / decayed; re-warmable. | No |
| `WARM` | Engaged (asked pricing/dates, or 5+ messages). | No |
| `HOT` | Strong buying signal. | No |
| `NEEDS_ATTENTION` | Flagged for operator. | No |
| `WAITING_FOR_PAYMENT` | Link sent, awaiting payment. | In-flight |
| `CONFIRMED` | Paid (real deposit). The "won" state. | Terminal (won) |
| `DISREGARDED` | Genuine non-customer (vendor/seller/spam/wrong-number) or completed booking. | Terminal |
| `LOST` | A real prospect who didn't convert. Kept for win-back. | Terminal |
| `SCAM` | Crypto/fraud/phishing. The stickiest. | Terminal (never reopens) |
| `PAUSED_SPAM`/`PAUSED_B2B`/`PAUSED_PERSONAL` | Held / suppressed. | Held |

`_LABEL_RANK`: `NEW(0) < COLD(1) < WARM(2) < HOT(3) < NEEDS_ATTENTION(4) < WAITING_FOR_PAYMENT(5) < CONFIRMED(6) < DISREGARDED(7) < LOST(8) < SCAM(9)` (drives the sticky-upward guard).

**`compute_label(latest_message, facts)`** matches signal heuristics in strict priority: `payment_confirmed_chat → CONFIRMED` (only if confirm phrase AND a link logged ≤48 h); `date_passed → COLD`; `service_mismatch`/`declined → LOST`; `payment_intent → HOT`; `quote_accepted → HOT`; `money_mentioned`/`lets_do_it`/`same_day_booking`/`multi_yacht_engaged → HOT`; `pricing_inquired`/`date_asked_no_commit`/`engaged_5plus → WARM`; else `new_window → NEW`.

**`handle_label_eval`** (per-message updater) order: (1) terminal short-circuits (`CONFIRMED` never auto-demotes, `SCAM` never auto-reopens); (2) lock check (`label_locked_until`); (3) compute + dampen (`compute_confidence`; <0.4 → demote one tier via `_TIER_BELOW`); (4) terminal-reopen check; (5) sticky-upward guard; (6) apply (`apply_label_transition` writes `customer_facts.label` + a `customer_label_history` row; CONFIRMED also persists `booked_yacht`). Hard-demote signals bypassing dampening: `date_passed`, `cold_decay`, `confirmed_terminal`, `locked`.

**What can move a lead OUT of a closed/terminal state (highest-risk):**
- **Returning-customer reopen (LOST/DISREGARDED only)** — only if all of: close was not operator-set (`_is_operator_close`), the message isn't a decline, the computed target ranks below the terminal label, and `_is_reengage_enquiry(msg)` is true. `reengage_reopen_target` caps the new label at WARM unless the signal is hard/high-confidence. Fail-open: unknown close source treated as system-set.
- **SCAM never reopens automatically**; **CONFIRMED never auto-demotes** — operator `/label` only.
- **Operator `/label`** — the universal override; records into `label_corrections`, `created_by="operator"`, `PAUSED_*` sets a lock (SPAM permanent; others 90 d).
- **Merge re-activation (`_merged_label`)** — a stale canonical re-activates only if the duplicate is genuinely active.
- **Payment reconcile** — `_reconcile_paid_unconfirmed` can move even a DISREGARDED lead to CONFIRMED on a real, non-mismatched deposit with no operator lock.

**Cadence:** **cold-decay** (`_should_cold_decay`) demotes a silent lead to COLD after >7 days but never on NULL customer-timestamp or in-flight/terminal stages; **passed-date dormancy** (`_is_dormancy_eligible`) auto-closes only after ≥2 re-engage drafts AND ≥7 days silence; **re-engage cadence** (`_silence_window_for`) measured from *our* last reply (24–72 h soft, 3–14 d last-shot), bounded by `FOLLOWUP_CAP`, ≥48 h cooldown, terminal/paused suppression, `REENGAGE_MODE=operator`/`REENGAGE_DAILY_CAP=5`; **manual-override protection** (`_manual_override_protects`) blocks auto-demote for 14 days after a `manual:*` override.

**Merge / canonicalization (`@lid` ↔ `@c.us`).** Every read/write goes through `canonicalize_cid` (follows `merged_into`, cycle-guarded ≤8 hops, fail-open). Merge guards: `_merge_blocked`/`_names_likely_same_person` (block distinct real names — the 2026-06-02 Qurbani/Antonio recycled-LID false merge; allow first-vs-full name + transliterations), `_phone_disagreement_block` (block on different known phones), `_do_not_merge_pinned` (durable un-merge pin), `_merge_canonical_pick` (booked/CONFIRMED side survives; else richer history). Reconcile re-pointing is LIVE (`RECONCILE_REPOINT_ENABLED=1`).

**`booked_yacht`** is set only when the discussed-yachts list is unambiguous and not already set. Per the booking-verification guardrail, `booked_yacht` (or the audited index) is the **only** trustworthy "did they book" signal — `dates`/`booking_time`/`yachts` are inquiry-populated traps.

**Send-time guards:** `_send_blocked_by_status`, `_sibling_send_blocked` (`@lid`/`@c.us` sibling-card case), `_waha_send_blocked` (block only on STOPPED/SCAN_QR_CODE/FAILED), `_quality_floor_ok` (fail-closed). All fail toward not-sending.

#### One inbound, end to end

1. Customer → WAHA → n8n webhook. 2. n8n drafts (Claude AI node), calling `behavioral_context()`. 3. Bridge records the message, runs `/label-eval`, scores/regenerates through the quality gate. 4. Bridge posts an approval card to Telegram; almost nothing auto-sends. 5. On approval, the send goes out via `waha.py`, scrubbed + pin-corrected. 6. On payment, the webhook/poll matches, applies deposit-floor + payer-mismatch, promotes to CONFIRMED (fills `booked_yacht`, re-runs analysis). 7. Hourly sweeps cold-decay, reconcile merges duplicates, the safety-net reconcile rescues stuck-paid leads.

### 3c. Feature modules (HubSpot lookup, profile lookup, known-customers, re-engage, /review, exclusion guards, analysis guard)

Optional feature layers bolted onto the bridge's two core jobs (write an AI draft; render `/review`). Terms: **env flag/gate** = an environment variable toggling a code path (`1`/`true`/`yes`/`on` = on); **fail-open** = on error return nothing, normal flow continues; **fail-closed** = on error block the action; **`behavioral_context().formatted`** = the single text blob the live n8n drafter reads; **`build_query`** = an in-bridge fallback drafter path.

**LIVE / DORMANT summary (against 2026-06-15 production flags):**

| Module | What it does | Gating flag(s) | Live value | Status |
|---|---|---|---|---|
| `hubspot_lookup.py` | Live-reads HubSpot at draft time, injects a "RETURNING CUSTOMER" note into the live draft | `HUBSPOT_LOOKUP_ENABLED` | `1` | **LIVE** (token caveat) |
| `profile_lookup.py` | Returning-customer name/country/prefs from a static on-disk index | `PROFILE_LOOKUP_ENABLED` | `0` | **DORMANT** |
| `known_customers.py` | One-line "returning paid customer" cue from a flat phone→revenue file | `KNOWN_CUSTOMER_PROFILE_ENABLED` | `0` | **DORMANT** |
| `reengage_quote.py` | Pure builder of the one-tap follow-up Telegram card | (sweep `MODE`/`CONFIRM`) | owe-reply=operator+yes; reengage=operator, no confirm | **Module LIVE; owe-reply posts, reengage dry-run** |
| `review.py` | `/review` render, scorer, working-hours gate | five `REVIEW_*` sub-flags | all `1` | **LIVE (all sub-features on)** |
| `hermes_exclusion_guards.py` | Layer-3 block-list suppressing proactive outreach to staff/crew/agents | none (always on; fail-closed) | n/a | **LIVE (unconditional)** |
| `analysis_guard.py` | Deterministic guardrail correcting the LLM analyzer | none (always on) | n/a | **LIVE (unconditional)** |

- **`hubspot_lookup.py` — LIVE.** On every message, looks up the contact by phone (live HTTP to `api.hubapi.com`) and renders a compact "RETURNING CUSTOMER — internal CRM context" block appended to `behavioral_context().formatted`. Gated by `HUBSPOT_LOOKUP_ENABLED` (off → `""`, prompt byte-for-byte unchanged). Safety: fail-open everywhere, 1.0 s timeout, honors the Layer-3 exclusion guard *before* enrichment, a money-scrubber strips AED/`k`/comma figures, 300 s TTL cache (caches "no match"), token never logged, `hermes_data_quality_flag` contacts skipped. **Caveat:** needs `HUBSPOT_TOKEN` in the environment (not in the supplied flag list; if unset, every lookup silently no-ops despite the flag being on).
- **`profile_lookup.py` — DORMANT.** Same shape but reads a static ~4 MB `customer_profile_index.json`. Off (`0`); even if flipped on it's wired only into the `build_query` fallback, not `behavioral_context().formatted`.
- **`known_customers.py` — DORMANT.** Flat phone→`{name, revenue_aed, n_bookings}` lookup; emits e.g. "🏆 RETURNING PAID CUSTOMER — AED 48,000 lifetime across 3 booking(s)"; returns `None` for `@lid`. Off (`0`); also only in the `build_query` fallback.
- **`reengage_quote.py` — module LIVE; sweeps split.** Pure `build_followup_card(...)` (text + ✅/✏️/🔁/❌ buttons), used by two sweep handlers. Outreach gating: `dry_run = not (MODE=="operator" and CONFIRM=="yes")`. Re-engage: `REENGAGE_MODE=operator` but `REENGAGE_CONFIRM` unset → **dry-run** (previews, posts nothing; quiet hours 22:00–08:00 Dubai). Owe-reply: `OWE_REPLY_MODE=operator` AND `OWE_REPLY_CONFIRM=yes` → **posts real cards** (still approval-first to the customer).
- **`review.py` — LIVE (all sub-features on).** Builds `/review`: scores leads (`score_lead`), routes to sections (AWAITING REPLY, HOT, WARM, CONFIRMED, 💔 LOST, 🚫 SCAM, 💤 NO ACTIVE SALE…), renders per-lead cards. Holds `_is_uae_working_hours()` (the gate the analyze cron uses). Five optional sub-features, all `1`: `REVIEW_OWED_DIGEST_ENABLED` ("🔴 NEEDS YOUR REPLY" index), `REVIEW_DATES_DIGEST_ENABLED` ("📅 DATE THIS WEEK"), `REVIEW_COMPLETED_SECTION_ENABLED` (virtual "🏁 COMPLETED", render-only), `REVIEW_POSTPAY_OVERRIDE_ENABLED` (post-payment logistics + agreed-vs-gross price), `REVIEW_UNRELIABLE_FROM_STORE_ENABLED` (read stored `analysis_unreliable` instead of recomputing). Caps run at code defaults (10/10/8/5, owed-cap 20, dates-cap 25, completed-cap 10, refresh-cap 2, hours 9–21).
- **`hermes_exclusion_guards.py` — LIVE (unconditional, fail-closed).** A pure block-list that can only *suppress* proactive/marketing outreach to staff, crew/vendors, and B2B agents (it cannot select/mix up a customer and does not block inbound handling or approval-mode replies). Loads `exclusion_phones.json` once, normalizes via `normalize_phone`, handles `+digits`/`@c.us`/`@lid`. No flag — always on; **fail-closed** (any error, missing file, or even a zero-phone file → `is_excluded()` returns `True`).
- **`analysis_guard.py` — LIVE (unconditional).** Pure (no-I/O) corrections of the analyzer's failure modes: `reclassify_close_label()` (SCAM > DISREGARDED > LOST), `analysis_unreliable_verdict()`/`is_analysis_unreliable()` (analysis on empty history), `is_stale_relative_date()` ("tomorrow"/"this weekend" frozen days ago). No flag; `REVIEW_UNRELIABLE_FROM_STORE_ENABLED=1` only tells `/review` to prefer the stored verdict.

> **Naming caution.** `ANALYZER_JSON_GUARD_ENABLED=1` is a *different* guard (gates JSON-parsing of the analyzer's output in `server.py:3293`). It is **not** the gate for `analysis_guard.py`, which has no flag and is always on.

---

## 4. Flags & environment variables

This is the complete inventory of every environment variable read by `hermes-bridge/*.py` and `scripts/*.py`. "Code default" is the fallback baked into `os.environ.get(...)`. "Live prod value" is from the running production bridge (PID 1341215, systemd `--user` service `hermes-bridge`, read 2026-06-15). Anything not in that snapshot is `(unset → code default)`.

**How to read the toggle columns.** Most toggles are `os.environ.get("X","0").strip()=="1"` or via `_envflag()` in `util.py` (`true`/`1`/`yes`, case-insensitive). "Live (ON)" = on in production; "Dormant (OFF)" = code deployed but off; "Active (default)" = a numeric/timeout/infra value at its code default; "Set" = configured.

**Two caveats.** (1) The snapshot omits **secrets**, yet several live features prove their secrets are set (e.g. `HUBSPOT_LOOKUP_ENABLED=1` requires `HUBSPOT_TOKEN`; `PAYMENTS_ENABLED=true` requires `NOMOD_API_KEY`; serving at all requires `BRIDGE_TOKEN`) — so for secrets, `(unset → code default)` cannot be taken literally. (2) The cron drivers (`cron-intake-gap.py`, `cron-owe-reply-sweep.py`, `cron-reengage-quote.py`) read env via a custom `getenv()` that also falls back to a `~/hermes-bridge/.env` *file*; the snapshot is the *bridge* process env only, so cron-effective values (e.g. `OWE_REPLY_MODE=operator`, `REENGAGE_MODE=operator`) may originate from that file.

### Feature toggles

| Variable | Code default | Live prod value | Live or dormant | What it controls | Read by |
|---|---|---|---|---|---|
| `DRAFTER_PRICE_STRICT_ENABLED` | **no reader in repo** | `1` | **Live (ON) — reader NOT in this repo** | Per memory: rejects the Satoshi "2,000" opener defect. No reader exists in any file/commit. | (none found — see fragility) |
| `GIVENUP_DECLINE_ENABLED` | **no reader in repo** | `1` | **Live (ON) — reader NOT in this repo** | Per memory: kill-switch for the "given up?" decline fix. No reader exists. | (none found — see fragility) |
| `ANALYZER_JSON_GUARD_ENABLED` | `0` | `1` | Live (ON) | Enforces JSON-only analyzer output + no-JSON retry. | `server.py:3293` |
| `FWD_GATE_ENABLED` | (none → `None`) | `1` | Live (ON) | Forwarded-message gate in the draft pipeline. | `routes.py:374` |
| `HUBSPOT_LOOKUP_ENABLED` | `""` | `1` | Live (ON) | Injects returning-customer HubSpot context (requires `HUBSPOT_TOKEN`). | `hubspot_lookup.py:63` |
| `PROFILE_LOOKUP_ENABLED` | `""` | `0` | Dormant (OFF) | Layer-1 profile lookup gating. | `profile_lookup.py:69` |
| `KNOWN_CUSTOMER_PROFILE_ENABLED` | `0` | `0` | Dormant (OFF) | Known-customer profile injection/block. | `server.py:2629` |
| `PAYLINK_PRICE_GATE_ENABLED` | `false` | `1` | Live (ON) | Blocks payment-link creation unless an agreed price exists. | `routes.py:1172` |
| `PAYMENT_TRIGGERS_REANALYSIS_ENABLED` | `0` | `1` | Live (ON) | A payment triggers re-analysis. **Compared `=="1"` in server.py but `!="1"` in routes.py** (fragility). | `server.py:3139`, `routes.py:137` |
| `PAYMENTS_ENABLED` | `true` | `true` | Live (ON) | Master switch for Nomod payment processing. | `payments.py:50` |
| `REANALYZE_DEFER_ENABLED` | `0` | `1` | Live (ON) | Defers cooldown-suppressed re-analysis. | `routes.py:88,103` |
| `RECONCILE_REPOINT_ENABLED` | (none → `None`) | `1` | Live (ON) | Reconcile re-points identity/labels (Root B). | `routes.py:4278` |
| `RECORD_OUT_OPREPLY_ENABLED` | `0` | `1` | Live (ON) | Records operator outbound replies into the store. | `routes.py:2084` |
| `ROOT_B_PERSIST_ENABLED` | (none → `None`) | `1` | Live (ON) | Persists Root-B @lid↔phone resolution. | `routes.py:328` |
| `SWEEP_ATTEMPT_ROTATION_ENABLED` | `0` | `1` | Live (ON) | Rotates the hourly sweep on last ATTEMPT. | `routes.py:4430` |
| `REVIEW_OWED_DIGEST_ENABLED` | `0` | `1` | Live (ON) | "needs your reply" owed-digest in `/review`. | `review.py:67` |
| `REVIEW_DATES_DIGEST_ENABLED` | `0` | `1` | Live (ON) | "DATE THIS WEEK" digest. | `review.py:76` |
| `REVIEW_COMPLETED_SECTION_ENABLED` | `0` | `1` | Live (ON) | Virtual COMPLETED section (render-only). | `review.py:87` |
| `REVIEW_POSTPAY_OVERRIDE_ENABLED` | `0` | `1` | Live (ON) | Post-payment action-line override. | `review.py:99` |
| `REVIEW_UNRELIABLE_FROM_STORE_ENABLED` | `0` | `1` | Live (ON) | Reads stored `analysis_unreliable`. | `review.py:108` |
| `FOLLOWUP_ENGINE_ENABLED` | `true` | `(unset → code default)` | Active (default ON) | Master switch for the proactive follow-up engine. | `server.py:4020` |
| `HOURLY_SWEEP_FOLLOWUPS` | `0` | `(unset → code default)` | Dormant (OFF) | Whether the hourly sweep also fires follow-ups. | `routes.py:5595` |
| `CAP_DAILY_ACTIVE` | `true` | `true` | Active | Enables the daily autosend cap. | `server.py:163` |
| `CAP_CONSECUTIVE_ACTIVE` | `true` | `true` | Active | Enables the consecutive-autosend cap. | `server.py:165` |
| `CAP_SAMPLE_ACTIVE` | `true` | `true` | Active | Enables sampling (% held for human review). | `server.py:167` |
| `OWE_REPLY_MODE` | `shadow` | `operator` | Live (operator) | owe-reply sweep mode. | `cron-owe-reply-sweep.py:51` |
| `OWE_REPLY_CONFIRM` | `""` (→ no) | `yes` | Live (confirmed) | Required `=yes` for operator mode to act. | `cron-owe-reply-sweep.py:52` |
| `OWE_REPLY_IGNORE_QUIET` | `""` (→ false) | `(unset → code default)` | Dormant (respects quiet hours) | Bypass 22:00–08:00 Dubai quiet hours. | `cron-owe-reply-sweep.py:53` |
| `REENGAGE_MODE` | `shadow` | `operator` | Live (operator) | reengage sweep mode. | `cron-reengage-quote.py:45` |
| `REENGAGE_CONFIRM` | `""` (→ no) | `(unset → code default)` | Dormant (dry unless `=yes`) | Required `=yes` for reengage to act. | `cron-reengage-quote.py:46` |
| `REENGAGE_IGNORE_QUIET` | `""` (→ false) | `(unset → code default)` | Dormant (respects quiet hours) | Bypass Dubai quiet hours for reengage. | `cron-reengage-quote.py:47` |
| `INTAKE_PHANTOM_SUPPRESS_ENABLED` | `1` (ON) | not in bridge env → code default | Live (ON, via cron default) | Suppresses hollow content-less @lid "missed lead" cards. | `cron-intake-gap.py:83` |

### Caps & limits

| Variable | Code default | Live prod value | Live or dormant | What it controls | Read by |
|---|---|---|---|---|---|
| `CAP_DAILY_LIMIT` | `20` | `20` | Active (default) | Max autonomous sends per Dubai day. | `server.py:164` |
| `CAP_CONSECUTIVE_LIMIT` | `5` | `5` | Active (default) | Max consecutive autonomous sends. | `server.py:166` |
| `CAP_SAMPLE_PCT` | `5` | `5` | Active (default) | % of autosends to human review. | `server.py:168` |
| `AUTOSEND_MIN_SCORE` | `8` | `(unset → code default)` | Active (default) | Min quality score for autosend (fail-closed). | `server.py:173` |
| `FEEDBACK_MAX_GLOBAL` | `60` | `(unset → code default)` | Active (default) | Max global feedback rules. | `server.py:154` |
| `FEEDBACK_MAX_SCENARIO` | `5` | `(unset → code default)` | Active (default) | Max scenario-scoped feedback rules. | `server.py:155` |
| `FEEDBACK_MAX_PER_CUSTOMER` | `5` | `(unset → code default)` | Active (default) | Max per-customer feedback rules. | `server.py:156` |
| `FEEDBACK_WINDOW_DAYS` | `3` | `(unset → code default)` | Active (default) | Window for `/feedback` applicability. | `routes.py:3368` |
| `HOURLY_SWEEP_BATCH_LIMIT` | `200` | `(unset → code default)` | Active (default) | Rows scanned per hourly sweep. | `server.py:3994` |
| `HOURLY_SWEEP_HERMES_CAP` | `30` | `(unset → code default)` | Active (default) | Max Hermes calls per hourly sweep. | `server.py:3995` |
| `PIPELINE_ANALYZE_CAP` | `24` | `(unset → code default)` | Active (default) | Max leads analyzed per run. | `server.py:4009` |
| `PIPELINE_ANALYZE_WORKERS` | `5` | `(unset → code default)` | Active (default) | Parallel workers for pipeline analyze. | `server.py:4010` |
| `FOLLOWUP_BATCH_LIMIT` | `10` | `(unset → code default)` | Active (default) | Leads per follow-up batch. | `server.py:4021` |
| `FOLLOWUP_CAP` | `2` | `(unset → code default)` | Active (default) | Max follow-ups per lead. | `server.py:4026` |
| `REANALYZE_CAP` | `12` | `(unset → code default)` | Active (default) | Max re-analyses per run. | `routes.py:4371` |
| `DORMANCY_MIN_ATTEMPTS` | `2` | `(unset → code default)` | Active (default) | Min outreach attempts before "dormant". | `routes.py:3263` |
| `DORMANCY_MIN_SILENT_DAYS` | `7` | `(unset → code default)` | Active (default) | Min silent days before dormancy. | `routes.py:3264` |
| `PAYLINK_MAX_AED` | `500000` | `(unset → code default)` | Active (default) | Upper bound on a payment link (AED). | `routes.py:1160` |
| `CONFIRM_PROMOTION_MIN_AED` | `500` | `(unset → code default)` | Active (default) | Min deposit (AED) to auto-promote to CONFIRMED. | `payments.py:36` |
| `OWE_SWEEP_CAP` | `8` | `(unset → code default)` | Active (default) | Per-run owe-reply sweep cap. | `routes.py:6861`, `cron-owe-reply-sweep.py:56` |
| `OWE_HOURLY_CAP` | `20` | `(unset → code default)` | Active (default) | Hourly owe-reply cap. | `routes.py:6869` |
| `OWE_MAX_AGE_H` | `336` | `(unset → code default)` | Active (default) | Max age (14 d) for owe-reply eligibility. | `routes.py:6888` |
| `REENGAGE_DAILY_CAP` | `5` | `5` | Active (default) | Per-run reengage cap. | `cron-reengage-quote.py:50` |
| `REVIEW_CAP_HOT` | `10` | `(unset → code default)` | Active (default) | `/review` HOT tier cap. | `review.py:58` |
| `REVIEW_CAP_NEEDS_ATTENTION` | `10` | `(unset → code default)` | Active (default) | `/review` needs-attention cap. | `review.py:59` |
| `REVIEW_CAP_WARM` | `8` | `(unset → code default)` | Active (default) | `/review` WARM tier cap. | `review.py:61` |
| `REVIEW_CAP_COLD` | `5` | `(unset → code default)` | Active (default) | `/review` COLD tier cap. | `review.py:62` |
| `REVIEW_OWED_DIGEST_CAP` | `20` | `(unset → code default)` | Active (default) | Rows in the owed digest. | `review.py:69` |
| `REVIEW_DATES_DIGEST_CAP` | `25` | `(unset → code default)` | Active (default) | Rows in the dates digest. | `review.py:78` |
| `REVIEW_CAP_COMPLETED` | `10` | `(unset → code default)` | Active (default) | Rows in the COMPLETED section. | `review.py:89` |
| `REVIEW_INLINE_REFRESH_CAP` | `2` | `(unset → code default)` | Active (default) | Max inline WAHA fact-refreshes per `/review`. | `review.py:120` |
| `REVIEW_NEAR_READY_MSGS` | `20` | `(unset → code default)` | Active (default) | Msg-count threshold for "near ready". | `review.py:946` |
| `REVIEW_NEAR_READY_IMP` | `40` | `(unset → code default)` | Active (default) | Importance threshold for "near ready". | `review.py:947` |
| `BRIDGE_HERMES_CONCURRENCY` | `3` | `(unset → code default)` | Active (default) | Global cap on concurrent Hermes CLI subprocesses. | `hermes_calls.py:35` |
| `BRIDGE_HERMES_BG_CONCURRENCY` | `max(1, CONCURRENCY-2)` (=`1`) | `(unset → code default)` | Active (default) | Background Hermes concurrency. | `hermes_calls.py:56` |
| `BRIDGE_ANALYZE_RETRIES` | `1` | `(unset → code default)` | Active (default) | Retries for the analyze call. | `server.py:3042` |
| `INTAKE_PAGE_SIZE` | `200` | not in bridge env → code default | Active (default, cron) | WAHA chat-overview page size. | `cron-intake-gap.py:68` |
| `INTAKE_MAX_PAGES` | `60` | not in bridge env → code default | Active (default, cron) | Max WAHA pages per sweep (60×200=12k). | `cron-intake-gap.py:70` |
| `INTAKE_RECOVER_CAP` | `8` | not in bridge env → code default | Active (default, cron) | Max auto-recoveries per intake run. | `cron-intake-gap.py:73` |
| `INTAKE_PHANTOM_STALE_DAYS` | `7` | not in bridge env → code default | Active (default, cron) | Age threshold for suppressing content-less gaps. | `cron-intake-gap.py:89` |
| `AUDIT_CAP` | `50` | `(unset → code default)` | Active (default, script) | Cap for the recycled-LID audit script. | `scripts/recycled_lid_audit.py:20` |

### Timeouts & TTLs

| Variable | Code default | Live prod value | Live or dormant | What it controls | Read by |
|---|---|---|---|---|---|
| `BRIDGE_AUTOSEND_TTL` | `3600` | `(unset → code default)` | Active (default) | Redis TTL for autosend keys (s). | `server.py:144` |
| `BRIDGE_DEBOUNCE_TTL` | `120` | `(unset → code default)` | Active (default) | Inbound debounce window (s). | `server.py:145` |
| `BRIDGE_QUEUE_TTL` | `86400` | `(unset → code default)` | Active (default) | Draft-queue expiry (24 h). | `server.py:146` |
| `BRIDGE_FEEDBACK_TTL` | `600` | `(unset → code default)` | Active (default) | `/feedback` capture window (s). | `server.py:149` |
| `BRIDGE_HERMES_TIMEOUT` | `120` | `(unset → code default)` | Active (default) | Hermes CLI subprocess timeout (s). | `hermes_calls.py:24` |
| `BRIDGE_FACTS_TIMEOUT` | `15` | `(unset → code default)` | Active (default) | Customer-facts extraction timeout (s). | `server.py:298` |
| `BRIDGE_ANALYZE_TIMEOUT` | `45` | `(unset → code default)` | Active (default) | Lead-analyze timeout (s). | `server.py:3036` |
| `BRIDGE_ANALYZE_RETRY_BACKOFF` | `2.0` | `(unset → code default)` | Active (default) | Backoff multiplier between analyze retries. | `server.py:3044` |
| `REANALYZE_DEDUP_TTL` | `1800` | `(unset → code default)` | Active (default) | Dedup window for re-analysis (s). | `routes.py:72` |
| `PAYLINK_DEDUP_TTL` | `1800` | `(unset → code default)` | Active (default) | Dedup window for paylink creation (s). | `routes.py:1209` |
| `REVIEW_STALE_AFTER_SECS` | `1800` | `(unset → code default)` | Active (default) | When a `/review` snapshot is stale. | `routes.py:2145` |
| `REENGAGE_CLAIM_TTL` | `900` | `(unset → code default)` | Active (default) | Claim lock TTL for reengage (s). | `routes.py:6646` |
| `OWE_RECARD_TTL` | `14400` | `(unset → code default)` | Active (default) | Cooldown before re-carding an owed lead (4 h). | `routes.py:6865` |
| `OWE_SWEEP_FRESH_SKIP` | `1800` | `(unset → code default)` | Active (default) | Skip owe-reply if reply fresher than this (s). | `routes.py:6877` |
| `SAMEDAY_INTERRUPT_TTL` | `14400` | `(unset → code default)` | Active (default) | Same-day interrupt suppression window (4 h). | `server.py:3991` |
| `UAE_WORK_HOURS_START` | `9` | `(unset → code default)` | Active (default) | Start of UAE business hours (Asia/Dubai). | `review.py:126` |
| `UAE_WORK_HOURS_END` | `21` | `(unset → code default)` | Active (default) | End of UAE business hours (Asia/Dubai). | `review.py:127` |
| `INTAKE_PAGE_PAUSE` | `0.3` | not in bridge env → code default | Active (default, cron) | Pause between WAHA pages (s). | `cron-intake-gap.py:69` |
| `INTAKE_REALERT_COOLDOWN_H` | `6` | not in bridge env → code default | Active (default, cron) | Re-alert cooldown for an unrecovered gap (h). | `cron-intake-gap.py:75` |

### Infra / connection

| Variable | Code default | Live prod value | Live or dormant | What it controls | Read by |
|---|---|---|---|---|---|
| `BRIDGE_PORT` | `8788` | `8788` | Set | Bridge HTTP listen port (also used by crons). | `server.py:140` (+ all 3 crons) |
| `ADMIN_CHAT_ID` | **inconsistent: `5532831477` / `""` / `0`** | `5532831477` | Set | Operator Telegram chat. **Default differs by file** (fragility). | `server.py:369`, `routes.py:7220,7279,7505`, `cron-intake-gap.py:59` |
| `GOOGLE_REVIEW_URL` | `""` | `https://g.page/r/Cd9gFEpKn3w9EBM/review` | Set | Google review link in post-event messages. | `routes.py:3790` |
| `NOMOD_API_BASE` | `https://api.nomod.com/v1` | `(unset → code default)` | Active (default) | Nomod payment API base. | `payments.py:48` |
| `FILE_REGISTRY_PATH` | `~/hermes-bridge/file-registry.md` | `(unset → code default)` | Active (default) | Path to the file registry markdown. | `routes.py:7991` |
| `BRIDGE_PG_CONTAINER` | `n8n-postgres-1` | `(unset → code default)` | Active (default) | Postgres docker container. | `db.py:23` (+ cron-intake-gap.py:60) |
| `BRIDGE_PG_USER` | `hermes_rw` | `(unset → code default)` | Active (default) | Postgres user. | `db.py:24` |
| `BRIDGE_PG_DB` | `n8n` | `(unset → code default)` | Active (default) | Postgres database name. | `db.py:25` |
| `BRIDGE_REDIS_CONTAINER` | `n8n-redis-1` | `(unset → code default)` | Active (default) | Redis docker container. | `db.py:26` |
| `WAHA_BASE` | `""` | `(unset → code default)` | Active (default; resolved dynamically) | Override for WAHA base URL. | `waha.py:44` |
| `WAHA_CONTAINER` | `n8n-waha-1` | `(unset → code default)` | Active (default) | WAHA docker container. | `waha.py:45` |
| `WAHA_PORT` | `3000` | `(unset → code default)` | Active (default) | WAHA port. | `waha.py:46` |

### Secrets (name only — values never shown)

| Variable | Code default | Live prod value | Live or dormant | What it controls | Read by |
|---|---|---|---|---|---|
| `BRIDGE_TOKEN` | `""` | (secret; must be set — bridge is serving) | Set (inferred) | Shared auth token (`X-Bridge-Token`). | `server.py:139` (+ all 3 crons) |
| `ANTHROPIC_API_KEY` | `""` | (secret) | Set (inferred) | Anthropic API key for analyze/draft. | `routes.py:6346,6396` |
| `TELEGRAM_BOT_TOKEN` | `""` | (secret) | Set (inferred — card edits work) | Telegram Bot API token. | `server.py:194` |
| `ADMIN_TG_TOKEN` | `""` | (secret) | Set (inferred) | Telegram token for admin alerts + intake cron. | `routes.py:7504`, `cron-intake-gap.py:58` |
| `HUBSPOT_TOKEN` | (none → `None`) | (secret) | Set (inferred — `HUBSPOT_LOOKUP_ENABLED=1`) | HubSpot private-app token. | `hubspot_lookup.py:68` |
| `NOMOD_API_KEY` | `""` | (secret) | Set (inferred — `PAYMENTS_ENABLED=true`) | Nomod payment API key. | `payments.py:21` |
| `NOMOD_WEBHOOK_SECRET` | `""` | (secret) | Unknown (endpoint 500s if empty) | svix HMAC secret for Nomod webhooks. | `payments.py:27` |
| `WAHA_API_KEY` | `""` | (secret) | Set (inferred — WAHA calls work) | WAHA API key (`X-Api-Key`). | `waha.py:25`, `cron-intake-gap.py:61` |
| `SUPERMEMORY_API_KEY` | (none → `None`) | (secret) | Script-only | Supermemory API key for the verify script. | `scripts/verify-supermemory.py:24` |

**Scope notes.** *bridge*: `server.py`, `routes.py`, `review.py`, `payments.py`, `hubspot_lookup.py`, `profile_lookup.py`, `hermes_calls.py`, `db.py`, `waha.py`, `util.py`. *cron-intake*: `cron-intake-gap.py` (the `INTAKE_PHANTOM_*` flags live only here, absent from the bridge env → code defaults). *script*: the `cron-owe-reply-sweep.py` / `cron-reengage-quote.py` drivers + one-offs; all three cron drivers use a custom `getenv()` with a `~/hermes-bridge/.env` fallback.

---

## 5. Scheduled jobs

There are **two** schedulers: the operating-system cron on the box (jobs below), and the n8n Schedule-trigger workflows that run *inside* the n8n container (see [Section 2.2](#22-scheduled-workflows-analyze--review--hourly-sweep--payment-poll--reanalyze): `pipeline-analyze-cron`, `pipeline-review-cron`, `pipeline-hourly-sweep`, `payment-poll-cron`, `reanalyze-cron`). The n8n ones are **not** OS cron.

### Authoritative box crontab (user `ubuntu`; times UTC; Dubai = UTC+4)

| Schedule (UTC) | Command | Dubai time | Purpose |
|---|---|---|---|
| `0 2 * * *` | `/home/ubuntu/backup.sh` | 06:00 daily | nightly Postgres dump |
| `0 4 * * 0` | `/home/ubuntu/cleanup.sh` | 08:00 Sun | weekly cleanup |
| `0 4 * * *` | `cron-daily-summary.py` | 08:00 daily | operator digest (= 08:00 Dubai) |
| `*/3 * * * *` | `cron-waha-watchdog.sh` | every 3 min | WAHA session reboot-resilience (infra watchdog) |
| `*/3 * * * *` | `cron-edge-watchdog.sh` | every 3 min | caddy/edge inbound-webhook watchdog (infra) |
| `0 6,12 * * *` | `cron-recycled-lid-audit.sh` | 10:00, 16:00 | recycled-LID monitor |
| `30 6,10,14,18 * * *` | `cron-intake-gap.py` | 10:30, 14:30, 18:30, 22:30 | intake-gap / missed-lead alerts (daytime Dubai) |
| `*/30 * * * *` | `cron-reengage-quote.py` | every 30 min | re-engage quoted-silent (REENGAGE_MODE=operator, cap 5/24h) |
| `*/15 * * * *` | `cron-owe-reply-sweep.py` | every 15 min | owe-reply sweep (OWE_REPLY_MODE=operator + CONFIRM=yes — LIVE, not shadow) |
| `30 3 * * *` | `offsite-backup.sh` | 07:30 daily | ENCRYPTED offsite backup to Google Drive via rclone |
| `30 4 * * *` | `backfill_lcm.py --tranche-size 15 --execute` | 08:30 daily | lcm backfill ~15/day, self-draining/idempotent |

> **Not in the crontab:** `cron-reminders.py` **EXISTS on the box but is NOT scheduled** → currently dormant (does not run). Operational consequence: any `customer_triggers` reminders silently never fire and pending triggers accumulate indefinitely.

Reading aids: **fail-closed** = on error raise a loud alarm; **fail-open** = on error go quietly silent (dangerous for a safety net); **dry-run/shadow** = computes candidates but posts nothing; **approval-first** = the job never sends to a customer, it posts a card the operator must tap ✅ Send.

### OS-cron jobs in detail

- **`backup.sh` (06:00 daily).** Nightly full `pg_dump -U n8n n8n` + a tar of the n8n config (`.env`, `docker-compose.yml`, `Caddyfile`) into `/home/ubuntu/backups`, pruned to `KEEP=3`. Pre-dump disk guard prunes early if ≥92% full. Never touches lead data. **No resurrection risk.**
- **`offsite-backup.sh` (07:30 daily).** Bundles the rebuild-critical set (whole `hermes-bridge` minus caches/`.bak`, n8n config, systemd units, crontab snapshot) + newest pg_dump, **encrypts with a public key the box cannot itself decrypt**, uploads via `rclone` to `dubriani-drive:dubriani-offsite-backups`, prunes remote (7 data / 30 config). Backup transport only. **No resurrection risk.**
- **`cron-daily-summary.py` (08:00 daily).** Telegram morning digest: who is awaiting your reply, 24 h behavior rules, pending follow-ups, leads needing attention, non-`approval` conversations, today's autonomous-send counts (SELECT-only; excludes `DISREGARDED`/`PAUSED_%`; risk list only `HOT`/`NEEDS_ATTENTION`/`WARM` with `merged_into IS NULL`). Near the end it fires two best-effort writes: `POST /dormancy-sweep` (closes passed-date leads after 2 attempts + 7 days — moves *toward* closed) and `POST /daily-feedback-sweep` (post-trip feedback cards to CONFIRMED). Both "never raise." **No resurrection risk** (operator-facing; moves toward dormant).
- **`cleanup.sh` (08:00 Sun, weekly).** Deletes bridge `.bak.*` older than 7 days (keeps newest 3) + `docker builder/image prune` (dangling only; no `-a`, no `--volumes`). **No resurrection risk.**
- **`backfill_lcm.py` (08:30 daily, `--tranche-size 15 --execute`).** Fixes ~half of active leads having NULL `last_customer_message_at` (rows created by `/review`/analysis, not real inbound), which blinds owe-reply/staleness. Backfills from WAHA ~15 leads/day, idempotent; creates a one-time rollback snapshot `conversation_state_bak_backfill_20260611` before the first write; **restricted to active labels** (`NEW,WARM,HOT,NEEDS_ATTENTION,COLD,WAITING_FOR_PAYMENT,CONFIRMED`) — never touches `LOST/DISREGARDED/SCAM/COMPLETED`. **No resurrection of closed leads**, but **indirect caution:** for an active **COLD** lead with an inbound but no recorded outbound, it sets `last_customer_message_at` while leaving `last_operator_reply_at` NULL → flips the lead to "owed" → can surface a long-silent COLD lead into the **LIVE** owe-reply sweep (a fragility, not a closed-lead resurrection).
- **`cron-recycled-lid-audit.sh` (10:00, 16:00).** Read-only audit detecting recycled-`@lid` suspects (the "Qurbani-class" bug); runs `recycled_lid_audit.py` (`AUDIT_CAP=60`); Telegram alert only if `SUSPECTS > 0`. No DB writes, no customer contact (can over-alert — the 2026-06-14 "Erika" false positive). **No resurrection risk.**
- **`cron-intake-gap.py` (10:30, 14:30, 18:30, 22:30).** Catches leads that reached WAHA but never landed in `customer_facts` (webhook outage, dropped n8n pipeline). Pages all WAHA chats, folds `@lid`↔`@c.us`, finds gaps, **auto-ingests** each via `POST /refresh-facts` (creates `customer_facts`+`conversation_state`), then posts a one-tap recovery card. **Fail-closed** (if WAHA paging or the read fails it alerts that "the net is blind"). Phantom suppression default ON (`INTAKE_PHANTOM_SUPPRESS_ENABLED`/`INTAKE_PHANTOM_STALE_DAYS=7` from code defaults — those flags are **not** in the bridge env). The file's docstring proposes `*/10 * * * *` but the authoritative crontab keeps 4×/day. **No resurrection, with caveat:** it only ingests chats *absent* from `customer_facts` (a closed lead is already present, so not a gap), and cards are approval-first — **but** auto-ingest does **not** consult the Layer-3 exclusion list before creating a row, so it can re-create a row for a never-seen contact that *should* be blocked (staff/crew) and post a Draft-reply card for them (fragility). If a closed lead's row were ever deleted it could re-appear as a gap.
- **`cron-owe-reply-sweep.py` (every 15 min — LIVE).** Thin trigger firing `POST /owe-reply-sweep`; the bridge finds leads where *we* owe a reply (customer messaged after our last outbound), drafts a reply, resolves the phone, runs the exclusion guard, self-posts a ✅ Send card. **Approval-first.** With `OWE_REPLY_MODE=operator` AND `OWE_REPLY_CONFIRM=yes`, `dry_run=False` → **posts real cards.** Quiet hours 22:00–08:00 Dubai skipped. Per-run cap `OWE_SWEEP_CAP` (8). **No resurrection** (customer-initiated; exclusion-guarded; approval-gated).
- **`cron-reengage-quote.py` (every 30 min — currently dry-run).** Thin trigger firing `POST /followup-sweep`; the bridge scans **quoted-but-silent** leads, drafts each with "Voss ghost-recovery" phrasing, runs the exclusion guard, self-posts a ✅ Send card (`REENGAGE_DAILY_CAP=5`). **The canonical resurrection vector.** `dry = not (MODE=="operator" and CONFIRM)`; `REENGAGE_MODE=operator` is set but `REENGAGE_CONFIRM` is **NOT** in the env → `dry=True` → **previews candidates, posts NOTHING.** (Deliberate contrast with owe-reply, which has `CONFIRM=yes`.) If `REENGAGE_CONFIRM=yes` were set it would post approval cards (capped 5/run, 48 h per-lead cooldown, exclusion guard, quiet hours). **Resurrection risk: YES by design — currently neutered.** Whether a *closed/disregarded* lead can be a candidate depends on the bridge's `/followup-sweep` filter — re-verify before flipping the flag.
- **`cron-reminders.py` (DORMANT — not scheduled).** Would, every 15 min, find `customer_triggers` rows `status='pending'` past `reminder_date`, Telegram a reminder, mark `status='reminded'`; suppresses `LOST/DISREGARDED/CONFIRMED`/paused/merged. **No resurrection** even if enabled (operator-only). Currently silent (see note above).

**Cross-cutting:** two-tier "live" gating (owe-reply LIVE vs reengage effectively shadow, hinging entirely on `*_CONFIRM`); approval-first holds everywhere a customer could be reached; the `INTAKE_PHANTOM_*` identity-flag split (cron process space, not bridge env). Also in the crontab but infra-only: `cron-waha-watchdog.sh` and `cron-edge-watchdog.sh` (both `*/3`).

### ⚠️ Jobs that can re-touch / resurrect a lead

Compiled across OS-cron and n8n (resurrection = an automated job pulls a closed/lost/disregarded/declined or long-silent lead back into active outreach or in front of a customer):

| Job | Scheduler | Risk | Notes |
|---|---|---|---|
| **`cron-reengage-quote.py`** (`/followup-sweep`) | OS cron, `*/30` | **YES by design — currently neutered (dry-run)** | The canonical resurrection vector; targets quoted-silent leads. Mitigations: approval-first, exclusion guard, 48 h cooldown, 5/run cap, quiet hours. Flip risk: `REENGAGE_CONFIRM=yes`. |
| **Reanalyze Queue Cron** (`/pipeline-analyze source=reanalyze`) | n8n, every 5 min | **YES (main automated seam)** | Fed by every inbound; filter drops terminal but NOT `COLD`, so an analyzer-declined-then-`COLD` lead can be re-promoted to active and re-surfaced in `/review` with no operator gate; can also page not-convertible cards at any hour. |
| **Payment Poll Cron** (`/poll-payments`) | n8n, every 2 min | **YES (payment-gated, intended)** | A matched real deposit promotes any non-`CONFIRMED` lead — incl. `LOST`/`DISREGARDED` — straight to `CONFIRMED`, bypassing terminal state. `_reconcile_paid_unconfirmed()` can do the same each tick. |
| **Pipeline Hourly Sweep** follow-up branch | n8n, hourly | **Conditional** | Off by default (`eligible_followups: []`); if `HOURLY_SWEEP_FOLLOWUPS=1` it drafts proactive nudges to silence-`COLD` leads (excludes terminal + analyzer-closed) and double-fires against `/followup-sweep`. |
| **`backfill_lcm.py`** | OS cron, 08:30 | **Indirect** | Not a closed-lead resurrection, but can flip a long-silent active **COLD** lead to "owed" → into the LIVE owe-reply sweep. |
| **`cron-intake-gap.py`** | OS cron, 4×/day | **No, with caveat** | Only ingests chats absent from `customer_facts`; but auto-ingest skips the Layer-3 exclusion guard, so a should-be-blocked never-seen contact can be created + carded. |

(For completeness: Pipeline Analyze closes-direction only and re-scores `CONFIRMED` without demotion; Pipeline Review changes no labels; owe-reply only fires for customer-initiated leads.)

---

## 6. Data stores

The system spreads one customer's data across **four data stores plus a Redis cache**. "The bridge" = the Python service; "cid" = a WhatsApp id (`<digits>@c.us` or `<hash>@lid`); "the box" = the production EC2 server.

### PostgreSQL (primary operational store)

Postgres runs as a **Docker container**, not a managed service. The bridge uses no DB driver — `db.py` shells in with `docker exec ... psql` (trust auth on the in-container socket), same for Redis (`docker exec ... redis-cli`), ~50–150 ms per call.

| Setting | Default | Meaning |
|---|---|---|
| `BRIDGE_PG_CONTAINER` | `n8n-postgres-1` | The Postgres container — **shared with n8n** |
| `BRIDGE_PG_DB` | `n8n` | Database name — the bridge's tables live **inside n8n's own database** |
| `BRIDGE_PG_USER` | `hermes_rw` | The bridge's limited role (SELECT/INSERT/UPDATE/DELETE per grants) |
| (table owner) | `n8n` | Owner role that runs migrations (CREATE/ALTER); `hermes_rw` cannot |

The 14 migrations in `db/migrations/` are *additive* (mostly `ALTER TABLE`s); several core tables have **no `CREATE TABLE` in this repo** (created out-of-band on the box, marked "base").

| Table / view | Created in | What lives there | Written by | Read by |
|---|---|---|---|---|
| `customer_facts` | **base** + 9 migrations | One row per customer (PK `customer_id`). Core: `name`, `dates`, `yachts`, `party_size`, `message_count`, `updated_at`. Migrations add `label`+locks (001), `importance_*`+`disregard_*` (004), `merged_into` (006), `booked_yacht` (008), `name_locked`/`name_lock_reason` (009), `booking_date_abs`/`booking_time`/`addons` (011), `analysis_unreliable` (013), `last_analyze_attempt_at` (014) | Bridge `_upsert_facts_sql` on every inbound; hourly `/pipeline-analyze`; analyzer (`booked_yacht`); operators | `/review` (via `v_lead_summary`), `behavioral_context()`, `handle_info` |
| `customer_label_history` | 001 | Audit log of every label transition | Bridge, reconcile guards | `/review`, audits |
| `conversation_state` | 001 (+`followup_count` 005) | Per-customer timings only | Bridge on message/reply/nudge | `/review` scorer, follow-up engine |
| `label_corrections` | 001 | Operator manual overrides → dampening | Bridge on operator override | Self-improvement tuning |
| `autonomous_sends` | **base** (+`notes` JSONB 003) | Audit of proactive follow-ups | Bridge follow-up engine | Audits, dormancy checks |
| `draft_log` | 007 | One row per draft, `created→scored→outcome` (incl. shadow "would_send"); 90-day retention | Bridge `_draft_log_write` | `/log <day>` |
| `conversation_messages` | 010 | **Durable append-only transcript**; idempotent via partial unique `(customer_id, msg_id)` | Bridge `record_message` (012 lets it move/delete during re-point) | Analyzer, backfill, drafter |
| `lid_phone_map` | **none in repo** (box only) | Durable `lid`↔`phone` map (+`conflict_phone`), seeded from WAHA `/lids` | Box seed job | reconcile recycled-LID guard |
| `edit_corrections` | **`scripts/sql/`, not `db/migrations/`** | Operator draft edits → feeds `behavior_rules` | Bridge on operator edit | Edit-learning loop |
| `behavior_rules` | **base** (+`source`) | Learned drafting rules | Edit-learning loop | Drafter |
| `customer_triggers` | **base** | Per-customer signals | Bridge analyzer | `v_lead_summary` |
| `customer_notes` | **base** | Free-text operator notes | Operators | `v_lead_summary` |
| `conversation_modes` | **base** | Per-customer mode (latest wins) | Bridge | `v_lead_summary` |
| `v_lead_summary` (view) | 002, recreated 004 | Read-only join for `/review`'s scorer | — | `/review` only |

### Redis (ephemeral draft queue — Docker container)

`n8n-redis-1` holds the short-lived draft approval queue (24 h, `BRIDGE_QUEUE_TTL=86400`); it replaced an older n8n `staticData.pendingQueue`.

| Key pattern | Type | Purpose |
|---|---|---|
| `drafts:active` | set | All currently-pending draft ids |
| `draft:<id>` | string (JSON) | The draft payload |
| `drafts:bycustomer:<cid>` | sorted set | Drafts per customer, score = timestamp |
| `tgmsg:<message_id>` | string | Reverse index: Telegram card → draft id |
| `draft:<status>:<chat_id>` | string | Singleton for the one draft awaiting operator text (`awaiting_edit`/`awaiting_amount`) |

### Supermemory (currently inert)

Per `docs/supermemory-status.md`, Supermemory (a vector KB) was **removed from the live Phase-1B workflow on 2026-05-20**. The retrieval node was deleted; `Build Prompt`'s `memoryContext` is a hardcoded empty string. Drafting runs entirely off the self-contained ~27 KB system prompt. **Why removed:** the Google Drive connector was mis-scoped to a Drive parent folder, ingesting ~16 unrelated SEO/PR files **and a customer-PII file** (ids, names, emails, phones), all with empty `containerTags`. Only repo references: `scripts/verify-supermemory.py`, `scripts/build_workflow.py`. **Open risk:** the leaked PII file is still indexed and must be deleted manually.

### HubSpot (external CRM — read-only from this repo)

From this repo HubSpot is read-only: `hubspot_lookup.py` (Layer 1) live-reads a contact at draft time to inject a "RETURNING CUSTOMER" block. The write side (contact + deals sync) lives in a separate project (`~/projects/whatsapp-classifier/hubspot-migration/`). **Identity key:** phone (cid → digits/resolved → normalized `971…`). **Fields read** (`_PROPS`): `firstname`, `lastname`, `phone`, `customer_type`, `hermes_summary`, `hermes_data_source`, `hermes_data_quality_flag`, `last_quote_amount_aed`, `last_quote_yacht`, `primary_objection`. **Read by:** the drafter (appended to `behavioral_context().formatted`). **Safety:** gated by `HUBSPOT_LOOKUP_ENABLED` (LIVE since 2026-06-14); fail-open; honors the Layer-3 exclusion guard; 1.0 s timeout; 300 s TTL cache (caches "no match"); money-scrub before injection; skips `hermes_data_quality_flag`; token never logged.

### Google Drive (encrypted, backup-only)

Not queried by the system — the offsite DR target, encrypted snapshots only. `scripts/backup.sh` (local on-disk, same EC2 disk that filled to 100% on 2026-05-29; keeps newest 3 of each `postgres_*.sql` + `config_*.tar.gz`); `scripts/offsite-backup.sh` (03:30 cron; **config** bundle + **data** bundle; both **GPG-encrypted with an asymmetric PUBLIC key** `dubriani-offsite-backup` — the box can encrypt but **never decrypt** its own backups; private key on the operator's Mac + password manager; uploaded via `rclone` remote `dubriani-drive` scope `drive.file` to `dubriani-offsite-backups/`; retention 7 data / 30 config). The only customer data on Drive is whatever is inside the encrypted `pg_dump`.

### Where they overlap — authority matrix

| Store | Keyed by | Holds the customer as | Live / offline |
|---|---|---|---|
| Postgres `customer_facts` | WhatsApp cid | The operational pipeline record | LIVE source of truth |
| HubSpot contact | phone | CRM history / returning-customer summary | LIVE, read-only into drafts |
| Supermemory | (would be Drive doc) | Knowledge/PII chunks | INERT (PII residue only) |
| Google Drive | (inside `pg_dump`) | Frozen encrypted copy of all rows | OFFLINE backup only |

- **"Did they book, and which yacht?"** → Postgres `customer_facts.booked_yacht` (or the audited index) is the *only* trustworthy signal. `booking_date_abs`/`booking_time`/`dates`/`yachts` are inquiry-populated **traps**; HubSpot and the date fields must **not** answer this.
- **Live label / pipeline state / timings** → Postgres.
- **Returning-customer history** → HubSpot `hermes_summary` (descriptive context only; figures scrubbed, never quoted).
- **Identity across the duplicate-cid problem** → Postgres: `merged_into` (006) marks canonical; `lid_phone_map` (box-only) + reconcile guards resolve `@lid`↔phone. Resolve by **phone + @lid, never by name alone**.
- **Disaster recovery** → the Google Drive encrypted bundle.

Postgres is the spine; HubSpot is a read-only CRM overlay, Supermemory a disabled knowledge layer, Drive a one-way encrypted dump.

---

## 7. Known fragilities & gotchas

Recurring traps, latent bugs, and "this already bit us" gotchas. Glossary: **WAHA** = unofficial self-hosted WhatsApp HTTP API (`devlikeapro/waha`); **n8n** = the workflow engine ("Dubriani Phase 1B", id `azPIy9OcDwiPV5uY`); **Hermes bridge** = the Python HTTP service on the box (port 8788); **`@lid` vs `@c.us`** = the two sender-id forms; **approval-first** = every customer-facing message needs a one-tap operator approval.

1. **WAHA is an UNOFFICIAL WhatsApp API — ToS / ban risk + silent breakage.** Rides the `devlikeapro/waha` container (WEBJS engine, free CORE tier) driving a real WhatsApp Web session. Meta can ban the number; protocol changes can break WAHA without notice; the CORE tier **cannot send native file attachments — it falls back to a Drive link** (the cause of item 13). Status: accepted risk, mitigated by human approval; Phase 1C migration to Meta's official Cloud API is the real fix but unstarted.
2. **Phone ↔ `@lid` identity split — one human becomes two records.** The same customer arrives as `@c.us` and as `@lid`, creating two `customer_facts` rows; labels/drafts/state/"did they book" fragment. Past burns: a confirmed Von Dutch 40 booking lived under `@lid` and was invisible; ~75% of the active book is `@lid`. Status: structural root, partially mitigated (`lid_phone_map` built+seeded, 3,917 rows, inert); phone-keyed identity (the cure) unbuilt.
3. **WhatsApp RECYCLES `@lid` values — recycled-LID false merges + "stuck" wrong-name cards.** An `@lid` once resolving to Antonio can later resolve to someone else. Confirmed live (`lid_to_cus('274942918680787@lid')` returned a different person's `@c.us`). A 2026-06-14 RCA also found a monitor false positive (a `.strip()` ate a `\x1f` separator and read `merged_into` as the name). Status: interim name-conflict merge guard live (`_names_likely_same_person`, fail-closed); phone-keyed cure unbuilt; monitor `.strip()` bug NOT fixed.
4. **Identity merge picked the survivor by message count, not by booking — burying real bookings.** A chatty non-booker absorbed and hid a quiet paid customer (Qurbani/Zayn, 154 vs 12 msgs). Status: name-conflict block shipped; "canonical-by-booking" not confirmed shipped.
5. **`test_readpath_persist` — durable-store read-path guard (born from store corruption).** Locks `build_analyzer_history`: read durable `conversation_messages`, top up with WAHA, persist only newer rows (idempotent, fail-safe). The store shipped **corrupt at source** on 2026-06-07 (P2-3): `record_message` had no timestamp param (everything collapsed to backfill time), was polluted with non-sent drafts, etc. — feeding garbage to the analyzer for ~100 leads. A `ts` param now exists. Status: read-path guard in place; broader write-path fix marked supervised, may not be fully landed.
6. **`test_reconcile_repoint` — merging only updated `customer_facts`, stranding the dup's history.** Before 2026-06-09 a merge wrote only `merged_into`, orphaning the dup's `conversation_messages`/`conversation_state`. The test locks the re-point SQL + the `RECONCILE_REPOINT_ENABLED` gate. Status: shipped behind a flag, reported ON.
7. **Autonomous (human-out-of-the-loop) send is SHELVED — do not build on it.** Built (FR-4) but **shelved 2026-06-08**; severe irreversible downside for almost no benefit (only ~1 real draft ever cleared the 8/10 floor). Machinery parked-dormant; global default `approval`; the `get_mode` gate fail-closes. Status: shelved; any revival is templated/no-price behind a fixed "scored == dispatched" guarantee.
8. **Autonomous-send R0 bug: scored bytes ≠ dispatched bytes.** The bridge scores the latest pending (possibly improver-rewritten) draft while WAHA dispatches the arm-time snapshot from `autosend:<draft_id>`; nothing binds them. A fix (`/autosend-state action=update`) exists as dead code. Status: confirmed broken; irrelevant only because autonomy is OFF — must be fixed before any revival.
9. **`cache_control` prompt-caching is wire-shape-fragile.** Caching fires only if the static system prompt sits in a `cache_control: ephemeral` block, all variable content sits OUTSIDE that prefix, and the prefix is byte-identical across calls. A wrong move silently re-bills the ~19K-token prompt at full price with no error. Measured ~$11.65/day saved (−44.7%, 67% hit). Status: live + measured; protected by `test_anthropic_cache.py` — keep it green.
10. **n8n `staticData` is concurrency-unsafe — FR-3 debounce is broken BY DESIGN.** `staticData` loads/saves per-execution, so concurrent executions can't see each other's buffer; it also only persists on a *successful* execution. Status: known-broken; the real debounce lives in Redis (`handle_debounce`), but any new `staticData` state hits the same wall.
11. **pendingQueue / draft-queue clobber + unbounded growth — replies to the WRONG customer.** Telegram message ids are per-chat counters that reset on bot swaps/restarts (11 confirmed collisions), so a reply-to-card matched the oldest entry, delivering e.g. a payment link to a different customer; old `build_*.py` deploys also PUT the stale staticData snapshot. Status: race shrunk to ~1-3 s via `safe_put`; unbounded-growth flaw remains — re-prune periodically. RULE: every n8n deploy uses `safe_put`.
12. **Fabricated prices / specs — recurring LLM hallucination with no deterministic guard on the live path.** Invented AED 600 (jetski row for a yacht), AED 375 catering (twice), wrong yacht (Bliss 55 vs Von Dutch 40). The deterministic `validate_draft_prices` only hard-blocks on the (disabled) autosend path, not on the operator-approval path. A separate Satoshi "2,000 opener" defect leaked to customers 54× before the 2026-06-15 fix. Status: partially mitigated; approval-first is the practical backstop; a live-path validator is still recommended.
13. **Internal notes / the name "Zayn" can leak to customers via the file-send caption path.** On 2026-06-05 a customer twice received a verbatim internal note naming "Zayn" — the outbound scrubber cleaned only the message text list, not file-send captions, and `sendFile` falling back to `sendText` (item 1) carried the caption through. Status: a send-boundary scrub for every outbound path was specified; `test_outbound_scrub.py` exists — verify it covers the caption path before trusting it.
14. **SSH `systemctl --user` needs `XDG_RUNTIME_DIR` exported, or the bridge restart silently fails.** Over a non-login SSH session, `systemctl --user restart hermes-bridge` fails unless you first `export XDG_RUNTIME_DIR=/run/user/$(id -u)`. The SSH path is also intermittently flaky (~1 in 3 fails). Status: mitigated by the export idiom + retries — keep using them.
15. **The Telegram webhook `secret_token` is auth — re-running `setWebhook` without it strips the control-plane auth.** Re-registering without `secret_token` resets it to empty, making the control-plane forgeable. Status: documented; rely on `scripts/setup-telegram-webhook.sh`, never a bare curl; the `from.id == 5532831477` check must never be weakened.
16. **Caddy/REST-auth and binaryMode stripping; bridge reachability depends on the AWS security group, not auth alone.** The API whitelist once stripped `binaryMode` on a PUT; the bridge binds `0.0.0.0:8788` and is NOT internet-reachable **only because the AWS SG blocks 8788** (an SG change, not code, keeps it private). Status: bridge token + SG are defense-in-depth; treat the SG rule as load-bearing.
17. **WAHA→n8n inbound webhook has no HMAC signature — forgeable inbound.** Combined with item 16, only network reachability prevents forged customer messages. Per operator notes the HMAC-secret fix is HELD/not built.
18. **Genuinely-lost leads (ROOT B) leave ZERO trace — you can't even count them.** A message that arrives but fails to create a lead leaves no row/dead-letter/audit line (Ayaan +971589954694: zero footprint across all 13 tables/journald/Redis). The bug-hunt counted ~95 live intake gaps. The exact drop point is unproven (n8n's `execution_entity` is permission-denied). Status: partially addressed (intake-gap cron + 2026-06-15 phantom-card suppression); the structural "persist before drafting" change and the blast-radius count remain open.
19. **CONFIRMED means both "upcoming" and "already happened"; booking dates are free text never anchored to a calendar — Family B.** No WON/COMPLETED state; the date is literal text ("today", "May 28") never pinned. 6+ code paths re-guess "has the trip happened?" and disagree; a cash-paying customer (Tal, AED 7,600) was auto-killed to COLD. Cash deals have **no path to CONFIRMED** (requires a `payment_link_sent` row). Status: a full fix is designed (anchor dates at capture, add COMPLETED, single `event_passed()`, `booked_yacht`) but high-risk/supervised; unlanded. KEY GUARDRAIL: `booked_yacht`/the audited index is the only trustworthy "did they book" signal.
20. **"Not a customer" mislabeling — keyword-matching the LLM's free-text reasoning.** The NOT_A_CUSTOMER regex (checked before LOST) matches any reasoning mentioning spam/pitch/b2b/promo — including Dubriani's own outbound upsell — so legitimate non-converting leads get tagged "not a customer." A coupled regression had the Disregard button write `created_by='operator:disregard_button'` even when Hermes decided, blocking auto-reopen for ~19 returning LOST customers. Status: fixes designed; lexicon/ordering fix low-risk, mass rebucket supervised.
21. **Floor-clearance and re-scoring staleness — analyzers don't reliably re-run.** "No thanks" fails `_facts_extract_gate` (never enqueues a re-score); the hourly cron empirically did not re-score an active HOT lead either, so stale 90/100 scores persist after a rubric fix. Status: diagnostic-first fixes specified; "does the cron re-score?" is the gating question.
22. **The live drafter is the n8n direct-to-Anthropic node — much bridge logic never reaches the customer reply.** Bridge features (known-customer block, Step-2, `system-prompt.md` edits) can be "stored but unused." Several STYLE/no-invent directives reach the drafter/scorer but **not** the refine/regen/lead rewriters. Status: architectural reality — verify a change reaches the *n8n* path; HubSpot read-side was wired at `behavioral_context().formatted` to avoid n8n edits.
23. **The box is NOT a git repo; n8n workflow JSON is not push-source — clobber hazards.** The bridge runs from a flat `/home/ubuntu/hermes-bridge` (no git); the n8n workflow is not git-tracked and the committed JSON is a post-deploy mirror that lags live. Status: mitigated by `safe_put` + import-success asserts; rule: n8n edits only via `n8n_deploy.safe_put`, prompt redeploys carry HEAD forward.
24. **WAHA history-sync lag — the draft "forgets" a detail the customer just gave.** If WAHA hasn't synced the latest messages at draft time, the model re-asks for a known date/yacht/duration. Status: the durable store + read-path top-up (item 5) is the structural mitigation; not fully closed.
25. **Supermemory was removed from the MVP — and carried a PII-leak risk if naively re-enabled.** The Drive connector was mis-scoped to "all of Drive" and swept in a customer-PII file + ~16 SEO/PR docs with empty `containerTags`; the old `memoryContext = JSON.stringify($json)` also dumped the entire raw API response. Status: re-enable is a Phase-2 task requiring a re-scoped connector, manual PII purge, container tags, and a fixed v3 parser. Do not restore the old node verbatim.
26. **ROOT A — local Hermes CLI saturation (background lane = exactly 1 slot, unbounded acquire).** Two-lane semaphore caps background at `max(1, total-2)=1`; the defect is an unbounded `acquire()` with no timeout (414 `WAITED` events in one night). Status: low-risk env fix specified (`BRIDGE_HERMES_BG_CONCURRENCY=4` + bounded timeout) but unshipped; needs a supervised restart.
27. **Nomod payment links have no creation idempotency — double-paylink financial risk.** Both `handle_payment_link` and the `send_paylink` intent call `nomod_create_link` unconditionally; two payable links (AED 14,900 and 15,200) were sent ~3 min apart to one customer (Xeno). Nomod also has no usable webhook (polled every 2 min). Status: unguarded (financial); idempotency keyed on cid+amount within a window is specified but supervised.
28. **Anthropic credential precedence — a stray Claude Code OAuth token can take down all drafting.** On 2026-05-21 all drafting went down with 401/400 errors because `~/.claude/.credentials.json` (a Claude Code OAuth token) overrode the intended API key. Status: resolved once; recurrence-prone — if drafting 401s, check credential precedence before assuming an outage.
29. **n8n PUT "unauthorized" flicker + post-import webhook 404.** n8n intermittently returns `{"message":"unauthorized"}` on a valid PUT; after importing a workflow, the in-memory webhook registration doesn't refresh (Telegram 404s) until `docker restart n8n-n8n-1`. Status: handled by retry budgets (up to 12×) + a mandatory docker restart in `deploy_bridge.py`.

### Additional fragilities surfaced during section authoring (30–46)

30. **Quality-check (`Hermes Improve`) error output is unconnected — fails silent.** The post-post improvement loop's `Hermes Improve` node has `onError=continueErrorOutput`, but its error output (`main[1]`) is wired to **no** downstream node. If `/quality-check` errors or the bridge is briefly down, the loop silently dies and the un-polished draft stands, with no alert (contrast `Claude AI`/`Send Draft to Telegram`, whose errors go to `Build Alert → Alert Zayn`).
31. **Most bridge calls use `continueRegularOutput`, masking bridge failures.** 81 nodes use `onError=continueRegularOutput` (incl. `Persist Draft`, `Mark Sent`, `Record Op Reply`, `Debounce Buffer/Flush`); a failed HTTP call does NOT branch — the error payload flows down the **success** path and the next node consumes it as valid data. Because the bridge is the single dependency for nearly all state, a hiccup can produce wrong-but-silent behaviour (e.g. a draft "marked sent" that wasn't).
32. **Claude `max_tokens=1024` can truncate JSON drafts and break `Parse Response`.** All four Anthropic nodes request `max_tokens=1024` and demand JSON-only. A long itinerary/quote can hit the cap → truncated JSON; `Parse Response` sits on the Claude **success** output, so a malformed-but-HTTP-200 body isn't caught by the node's error branch and throws inside `Parse Response`, stalling that draft.
33. **Telegram webhook secret is optional → admin gate degrades to `from.id`-only at runtime** (related to #15). `Verify Admin` enforces the `x-telegram-bot-api-secret-token` header **only when** `$env.TELEGRAM_WEBHOOK_SECRET` is set; otherwise it falls back to `from.id === 5532831477`, which Telegram does not cryptographically guarantee against a crafted POST. Losing the env var on an n8n restore/rebuild silently weakens the gate.
34. **Single hardcoded operator id + single WAHA session — no multi-operator / multi-number failover.** The approver is hardcoded as Telegram id `5532831477` in `Verify Admin`; every WhatsApp send hardcodes `session:"default"`. No second operator, backup approver, or second WhatsApp number without editing node code — a single point of human + transport dependency.
35. **Architecture doc is stale (51 nodes documented vs 202 live).** `docs/hermes-architecture.md` describes a 51-node workflow with a short endpoint list (`/draft`, `/save-rule`, `/set-mode`, `/health`); the live workflow is 202 nodes using ~35 bridge endpoints. The `/draft` endpoint named in the doc is not even the live draft path. A newcomer relying on the doc would mis-model the system.
36. **Reanalyze cron can resurrect a declined-then-COLD lead on any new inbound** (see §2.2, §5). `_filter_reanalyze_cids` drops terminal labels but explicitly **keeps `COLD`**, so an analyzer-closed (`auto:analyzer_close`/`score0`) lead is re-queued, re-scored, and can be re-promoted to active — no operator gate. The `/followup-sweep` SQL excludes `auto:analyzer%`; the reanalyze drain has no such exclusion.
37. **Reanalyze cron posts not-convertible disregard cards at any hour (no quiet-hours).** `source=reanalyze` skips the UAE working-hours gate by design; it shares the analyze body that fires "🛑 not-convertible" Telegram cards, so a 4am-Dubai inbound the LLM verdicts `close` can page the operator overnight (rate-limited only by `closecard:<cid>` 20 h dedup).
38. **Hourly-sweep follow-up branch double-fires with `/followup-sweep` when re-enabled.** The n8n hourly-sweep has a full `Split Followups → Draft → Persist → Send → Log` chain neutralised by the bridge returning `eligible_followups: []` unless `HOURLY_SWEEP_FOLLOWUPS=1`. When it was on it collided with the canonical `*/30` `/followup-sweep` (Milena got two cards). The dormant chain is a loaded gun: one env var re-enables nudges from a path lacking the lock + recipient-verify + quality badge.
39. **Reanalyze export: name says DISABLED but `active` flag is `true`.** The workflow name is "...(DISABLED — enable while watching box load)" yet the JSON has `"active": true` and an `event:'activated'` publish entry. Actual current state cannot be confirmed read-only from the export.
40. **n8n HTTP timeouts are shorter than the bridge re-entrancy lock.** Pipeline Analyze and Reanalyze nodes set timeout 600000 ms (10 min) but the bridge takes `lock:pipeline_analyze` with `EX 2400` (40 min). A >10-min sweep aborts the n8n request (no `onError`) and fails the execution while the bridge keeps draining; the next tick short-circuits with `previous_sweep_still_running`. Net: silent skipped runs + misleading n8n errors.
41. **Pipeline Review schedule depends on the n8n instance running in UTC.** Node names claim "09:00/17:00 Dubai" but the cron expressions are `0 5 * * *` / `0 13 * * *` (UTC). If the n8n container TZ is `Asia/Dubai` (or non-UTC), the digest fires at the wrong local time with no other signal — the "Dubai" labels are documentation only and can silently lie.
42. **Payment confirmation Telegram notifications are best-effort and silently lost.** In payment-poll the bridge can flip a lead to `CONFIRMED` in Postgres, but `Notify Matched`/`Notify Unmatched` use `onError: continueRegularOutput` — a Telegram outage means the DB shows `CONFIRMED` while the operator never sees the alert. Same swallow pattern on every `Send*` node across all five workflows.
43. **Payment poll promotes terminal leads (LOST/DISREGARDED) to CONFIRMED with no extra check.** `handle_poll_payments` promotes on `prev_label != 'CONFIRMED'` for any matched real deposit. It guards below-threshold amounts and payer-mismatch, but a real-deposit charge matched to a terminal lead is promoted straight to `CONFIRMED` — and Layer-3 matching is a fuzzy ±1 AED / ±30 min single-row check.
44. **Pipeline Analyze auto-COLD can close a live lead on an LLM misjudgement.** On a `close` verdict OR `importance_score == 0` the analyzer demotes `NEW/WARM/HOT/NEEDS_ATTENTION` to `COLD`. A guard stack (won/paid, unreliable, recent manual override, unverifiable passed-date, lock) blocks most bad cases, but the demotion is automated and driven by a single LLM verdict on possibly-incomplete WAHA history; the reanalyze cron runs the same path every 5 min, multiplying exposure.
45. **Review auto-heal turns a "read-only" digest into a WAHA-writing job.** `handle_review`'s auto-heal calls `refresh_customer_facts_from_waha(...)` (writes `customer_facts` name/yachts/dates) for broken-intake and stale high-value leads, and `mark_review_seen` writes `last_review_seen_at`. Under WAHA slowness this adds write side-effects and risks the 90 s node timeout — the twice-daily cron mutates data, not just reads.
46. **No SQL parameterisation — escaping is the caller's job** (also noted in §3a). `db.py._psql` runs `docker exec ... psql -c "<sql>"` with the SQL fully string-interpolated; the only protection is `_lit()` (quote-doubling), and the docstring explicitly says the caller is responsible for SQL escaping. One missed wrap on customer-controlled text (names, message bodies, WhatsApp IDs) can break a query or inject SQL.
47. **A live Supermemory API key sits in `.env.txt` in plaintext.** Unlike the gitignored `.env`, the repo's `.env.txt` holds a real `SUPERMEMORY_API_KEY=sm_…` value in cleartext, right next to the `.env.example` template (confirmed read-only on 2026-06-15). Anyone with repo (or backup) access gets a working Supermemory token. It should be revoked/rotated and the file removed from the working tree and history. *(Surfaced during this read-only mapping pass; not fixed.)*

### Operating rules distilled from the above (do these, every time)

- Approval-first is the safety net for the unofficial-API + hallucination risks — never weaken it.
- Resolve identity by **phone + `@lid` together**, never by name alone; verify "did they book" against `booked_yacht`/the audited index, never date fields.
- Every n8n deploy goes through `n8n_deploy.safe_put`; never hand-edit live JSON or push a stale local mirror.
- Restart the bridge with the `XDG_RUNTIME_DIR` export; always pass `secret_token` to `setWebhook`.
- Keep the `cache_control` guard test green; assume a change isn't live until you've confirmed it reaches the **n8n** drafting path.
- Ask the operator before any box state change (per the standing read-only / per-action-approval rule).

---

## 8. Tenant seams (Dubriani-specific → future config)

Every place a **Dubriani-specific value is baked into code, config, or the AI prompt**. To run this agent for a different charter business, each item would need to become per-tenant configuration; today almost none of it is. Values are scattered across the n8n workflow JSON (the live AI brain), the Python bridge, shell scripts, and a few canonical Markdown docs — and the same facts (prices, fleet, identity) are duplicated across all three layers, so multi-tenanting is more than swapping one config file.

> **Persona vs codename — note the discrepancy.** Some sections call the customer-facing concierge "Hermes"; the authoritative system-prompt node (cited below) says the bot **signs as "Maria"** to customers (12×), while **"Hermes" is the internal system codename**. Both appear in the table below. Prefer the prompt-node value (Maria) for the customer-facing persona name.

### Seam table

| Seam | What it is | Where it lives (file / node) | Current Dubriani value | Notes |
|---|---|---|---|---|
| **AI persona name** | First-person name the bot signs as | `phase-1b-telegram.json` → node 2 system prompt ("You speak in the voice of **Maria**… You sign as Maria") | **Maria** | 12× in the prompt. No env knob. |
| **Operator / approver name** | The human who approves every draft | Same prompt (9× "Zayn"); JSON schema field `notes_for_zayn` | **Zayn** | Renaming means editing the schema, not just text. |
| **Brand / company name** | Trading name in greetings/signatures | Prompt (15× "Dubriani"); everywhere | **Dubriani Yachts** / "Dubriani Charters Leisure Yachts and Boats Rental L.L.C" | |
| **System codename** | Internal name for the whole agent | Code comments, `cron-*.py`, memory | **Hermes** | Internal only; not customer-facing. |
| **Full system prompt / brand voice** | ~30 KB of rules (tone, qualification flow, negotiation, occasion scripts) | `phase-1b-telegram.json` node 2 | "v2, draft-approval mode" prompt | The single biggest tenant artifact; hardcoded JSON string. |
| **Market / cultural segment table** | Per-country-code playbook with win-rates | Prompt §1 "META-RULE B" | UAE/India/USA-CA-AU/UK/Russia-KZ rows, "based on 14,804 chats incl. 998 named VIPs" | Tuned to Dubriani's mix + history. |
| **Brand differentiators** | Selling points | Prompt §3 | "First & only Dubai yacht co. accepting crypto (USDT/BTC/ETH)", "AED 500 cash certificate", "AED 200 partner exotic-car discount", "7,371+ guests / 633 ProvenExpert reviews" | Marketing claims + perks. |
| **Per-yacht price allowlist (validator)** | Deterministic guard flagging wrong AED/hr | `server.py` `_CANON_YACHT_RATES` (~3655) | 24 yachts, e.g. `von dutch 40:{1400}`, `satoshi:{(2000,3000),1500}`, `ferretti 780:{5500}` | "Satoshi 2,000 opener defect" lives here (2,000 is the floor, open at 3,000). |
| **Catering / add-on price allowlist** | Same validator for food/extras | `server.py` `_CANON_CATERING`, `_CANON_ADDONS` (~3684) | fine dining 2,500; premium BBQ {2500,1500}; balloon 300; cake {300,500} | |
| **Yacht rate-ordering map** | Ranks leads by hourly value | `review.py` `YACHT_RATE` (~424) | ~60 yachts, 799→20,000 AED/hr (`pershing 82:5500`, `sofiya:20000`) | A SECOND copy of fleet prices. |
| **Canonical catalog block** | Human-authored fleet + catering + add-on price book | `tools/catalog/catalog-block.canonical.md` | §7 fleet (4 tiers), §8 catering, §9 shisha, §9.5 watersports, §10 occasions | Satoshi opener policy ("open EVERY quote at AED 3,000/hr") in §7.1. |
| **Payment-link ceiling** | Hard cap on any link | `routes.py` ~1160 `PAYLINK_MAX_AED` | env, default 500000 | |
| **Deposit-promotion floor** | Min AED counting as a real deposit | `payments.py` ~36 `CONFIRM_PROMOTION_MIN_AED` | env, default 500 ("10% of cheapest yacht") | Fleet-specific rationale. |
| **Payment currency** | Currency stamped on every link | `payments.py` ~162-171 | hard-coded `"currency": "AED"` | Also assumed AED in charge-matching (~1331). |
| **Admin / approver Telegram chat-id** | The ONLY account allowed to approve; alert recipient | `.env.example` `TELEGRAM_ADMIN_USER_ID`; `server.py:369` `ADMIN_CHAT_ID`; `server.py:2058` `DISK_ALERT_CHAT` (literal); workflow JSON; ~10 `scripts/` files | **5532831477** | Env in most places, but **hardcoded int literal** in `server.py:2058`, `scripts/*.py`, `pipeline-hourly-sweep.json`. |
| **Brochure / menu file registry** | Map of yacht/menu/route → Google-Drive links | Prompt "# FILE REGISTRY" (node 2) | dozens of `slug: https://drive.google.com/file/d/…/view` | All Dubriani's Drive. "Never invent links." |
| **Fleet / yacht list** | Yachts the bot may quote | Catalog §7; `_CANON_YACHT_RATES`; `YACHT_RATE` | ~60 yachts (Essential/Premium/VIP); Satoshi 70 "Dubriani-owned, prioritize" | URL pattern `dubriani.com/yacht/<slug>/`. |
| **Drop-off location pin** | Maps pin for boarding | `waha.py:175` `DROP_OFF_PIN`; `docs/confirmation-and-location-templates.md` | `https://maps.app.goo.gl/1aT4Vtqwe71FKi3AA` | A guard rewrites ANY non-canonical link to this. |
| **Parking location pin** | Pin for own-car arrivals | `waha.py:176` `PARKING_PIN` | `https://maps.app.goo.gl/kP8dHqhnrizYVxAw8` | In the allowed-pins set. |
| **Departure marinas list** | Marinas operated from | Prompt §3 | Dubai Marina, Dubai Harbour, Marsa Al Arab, Jumeirah Bay Island, Port de La Mer, Business Bay | |
| **Google review URL** | Link appended when asking for a review | `routes.py:3790` `GOOGLE_REVIEW_URL`; prompt `g.page/dubrianiyachts`; test `g.page/r/Cd9gFEpKn3w9EBM/review` | env value **`https://g.page/r/Cd9gFEpKn3w9EBM/review`** (code default empty; brand value `g.page/dubrianiyachts`) | Code default empty though brand URL is known — inconsistent (Fragilities). |
| **Business identity block** | Company contact card | Prompt §3 "Identity" | HQ Marina Plaza Tower–2901, Dubai Marina; Office UAE +971 4 550 6309, US +1 (754) 900-1310, UK +44 7868 811 587; **WhatsApp +971 58 950 2303** (`wa.me/971589502303`); Telegram `t.me/dubriani`; Signal `+97145506309`; email `bookings@dubriani.com`; web `dubriani.com`; Instagram `@dubrianiyachts` | |
| **WhatsApp catalog link** | Native WhatsApp catalog | Prompt | `wa.me/c/97145506309` | "Don't send … alone." |
| **WAHA WhatsApp session name** | Session the bridge sends through | `waha.py:238,258,282,347` | hard-coded `"default"` | One WhatsApp line per deployment. |
| **WAHA / n8n base URLs** | Infra endpoints | `.env.example` `WAHA_BASE_URL`, `N8N_BASE_URL` | `https://waha.13-63-82-112.sslip.io`, `https://n8n.13-63-82-112.sslip.io` | Embed the box IP `13.63.82.112`. |
| **Supermemory credential / container tag** | Long-term memory store | `.env.example` `N8N_CRED_SUPERMEMORY_ID=aKreN1AB4QYfQKlz`; `scripts/verify-supermemory.py` | tag = "the tag that matched your Dubriani content" | Memory namespace is tenant data. |
| **Timezone assumption** | All scheduling / quiet hours / "today" math | `labels.py:1193`, `server.py:174` (`DUBAI_MIDNIGHT`), `review.py:123`, `cron-*.py`, `test_slot_passed.py` | **Asia/Dubai = UTC+4, no DST** (often `timedelta(hours=4)`) | Quiet hours 22:00–08:00 + working-hours are Dubai-clock literals. |
| **Staff/crew/agent exclusion list** | Block-list suppressing proactive outreach to insiders | `exclusion_phones.json` (loaded by `hermes_exclusion_guards.py`) | 400 unique phones: internal_staff 16, external_crew_vendors 133, external_agents 251 | Pure Dubriani roster data. |
| **Owned-yacht prioritization** | Push the company-owned boat first | Catalog §7.1 / prompt | "Sunseeker Satoshi 70 — Dubriani-owned, prioritize" | Business-model-specific. |

### Additional tenant seams (from the n8n workflow JSON + bridge code)

| Seam | Where | Current value |
|---|---|---|
| Hermes bridge base address (all state/logic calls) | All ~70 bridge HTTP nodes; every bridge call in all 5 cron workflows | `http://172.18.0.1:8788` (Docker gateway; not internet-reachable) |
| Anthropic model id | `Claude AI`/`(Refine)`/`(Regen)`/`(Lead)`; `routes.py:6355` `_anthropic_draft`, `:6405` `_anthropic_score` | `claude-sonnet-4-6` (max_tokens 1024 draft / 300 score; `anthropic-version 2023-06-01`; system block `cache_control: ephemeral`) |
| Anthropic API key (n8n credential) | `Claude AI` nodes | credential "Anthropic API" id `jo8QCWLcdwI5Uqek` |
| Bridge auth token (n8n credential) | Bridge HTTP nodes (+15 inline `X-Bridge-Token` refs) | credential "Hermes Bridge" id `IgIcvPoibuAayVDx` (HTTP header auth) |
| Telegram bot token | All `api.telegram.org` nodes; `server.py:194-195` | `$env.TELEGRAM_BOT_TOKEN` (bot "Dubriani Hermes"); base `https://api.telegram.org/bot` |
| Telegram webhook shared secret | `Verify Admin` node | `$env.TELEGRAM_WEBHOOK_SECRET` (optional; gate degrades to `from.id`-only if unset) |
| WAHA API key (n8n credential) | WAHA HTTP nodes | credential "WAHA API" id `OaAspqZjcpsXIowi` |
| Chat-history fetch depth | `Get Chat History` query | `limit=15` (`downloadMedia=false`) |
| n8n owner project / author | `shared[].project` + `activeVersion.authors` | Zayn Kazemi `<z@ynkazemi.nl>`, projectId `cW9yfaMW5jqMcI5f` |
| Request-body tuning constants | trigger HTTP node `jsonBody` | payment-poll `{window_hours:24, page_size:50}`; pipeline-analyze `{cap:20}`; reanalyze `{source:'reanalyze'}`; hourly-sweep `{}`; review `{mode:'scheduled'}` |
| Schedule intervals (per-tenant cadence) | scheduleTrigger nodes | payment-poll 2 min; pipeline-analyze 1 h; hourly-sweep `0 * * * *`; review `0 5`+`0 13`; reanalyze 5 min |
| Schedule timezone assumption | pipeline-review-cron cron vs labels | `0 5`/`0 13` UTC = 09:00/17:00 Dubai only if n8n TZ is UTC |
| `BRIDGE_PG_CONTAINER` | `db.py:23` | `n8n-postgres-1` (default) |
| `BRIDGE_PG_USER` | `db.py:24` | `hermes_rw` (default) |
| `BRIDGE_PG_DB` | `db.py:25` | `n8n` (default) |
| `BRIDGE_REDIS_CONTAINER` | `db.py:26` | `n8n-redis-1` (default) |
| `BRIDGE_PORT` | `server.py:140` | `8788` (default; binds `0.0.0.0`) |
| `BRIDGE_TOKEN` | `server.py:139, 5205-5207` | no default — must be set in `~/hermes-bridge/.env` or the service refuses to start |
| `NOMOD_WEBHOOK_SECRET` (svix HMAC) | `routes.py:7587-7596` | expects `whsec_<base64>`; Nomod (UAE payment provider) specific |
| Install paths | `server.py:133-135`; `hermes-bridge.service:7-8` | `%h/hermes-bridge` (= `/home/ubuntu/hermes-bridge`); `system-prompt.md`/`.env` co-located |
| Python interpreter pinned in the unit | `hermes-bridge.service:8` | `ExecStart=/usr/bin/python3 %h/hermes-bridge/server.py` |
| Nomod link branding + currency + UA | `payments.py:55, 167-172` | title prefix `Dubriani Yachts — `; currency `AED`; `_NOMOD_UA='DubrianiHermesBridge/1.0'` |
| Operator name scrub + drafter notes field | `waha.py:167` `_INTERNAL_NAMES`; `routes.py ~6380` | `_INTERNAL_NAMES=('zayn',)`; drafter contract field `notes_for_zayn` |
| WAHA system display names filtered as push-names | `waha.py:106` `_WAHA_SYSTEM_NAMES` | `('WhatsApp Business', 'Dubriani admin chat')` |

### What it would take to multi-tenant

At minimum: (1) externalize the entire system-prompt string (persona/operator/brand names, identity card, segment table, brochure registry) out of the n8n JSON into a per-tenant template; (2) collapse price/fleet data into **one** source of truth (currently three: `server.py`, `review.py`, `catalog-block.canonical.md`); (3) move the admin chat-id, location pins, review URL, WhatsApp session, and infra URLs into env with no hardcoded fallbacks; (4) make timezone/quiet-hours config rather than `UTC+4` literals.

---

## 9. Credential / OAuth-token revocation status

Read-only liveness GETs were run on **2026-06-15** against every credential that could plausibly be "the OAuth token from the earlier session":

| Credential | Liveness check | Result |
|---|---|---|
| Telegram Hermes bot token | `getMe` | HTTP 200 → **LIVE (NOT revoked)** |
| Telegram admin bot token / `ADMIN_TG_TOKEN` | `getMe` | HTTP 200 → **LIVE (NOT revoked)** |
| n8n REST API key | `GET /api/v1/workflows` | HTTP 200 → **LIVE (NOT revoked)** |
| HubSpot private-app token `pat-eu1-5dfb0` (HubSpot labels this an "oauth-token" in its error strings) | `GET /crm/v3/objects/contacts` | HTTP 200 → **LIVE (NOT revoked; read scope works)** |
| rclone Google-Drive OAuth (remote `dubriani-drive:`) | `about` | returns quota → **LIVE (NOT revoked)** |

**Conclusion.** The phrase "the OAuth token from the earlier session" is ambiguous — it could mean the HubSpot pat token, the n8n API key, a Telegram bot token, or the Google-Drive OAuth. **Every candidate tested is still LIVE.** So whichever was meant, it is **NOT revoked as of 2026-06-15.** Recommend the operator revoke whichever they intended.

---

## 10. Appendix: version snapshots & repo layout

- **Workflow backups.** The committed `workflows/*.PRE-*.json` files (e.g. `phase-1b-telegram.PRE-DEBOUNCE_WINDOW.json`, `PRE-PAYLINK-SOURCE`, `PRE-TGSECRET`, `phase1b.PRE-*`, dated `PRE-DEPLOY-*`, and the `reanalyze-cron-...PRE-RENAME-20260610_033130.json` export) are **timestamped pre-deploy snapshots**, taken automatically right before a deploy — they are not live workflows. Only the no-`PRE-`-suffix files are current.
- **Database migrations.** `db/migrations/` holds **14** additive migrations (mostly `ALTER TABLE`): 001 labels/locks + history/state/corrections, 002 `v_lead_summary` (recreated 004), 003 `autonomous_sends.notes`, 004 `importance_*`/`disregard_*`, 005 `followup_count`, 006 `merged_into`, 007 `draft_log`, 008 `booked_yacht`, 009 `name_locked`, 010 `conversation_messages`, 011 `booking_date_abs`/`booking_time`/`addons` (and the box-only `lid_phone_map`), 012 re-point support, 013 `analysis_unreliable`, 014 `last_analyze_attempt_at`. Several base tables have no `CREATE TABLE` in the repo (created out-of-band on the box). `edit_corrections` lives in `scripts/sql/`, not `db/migrations/`.
- **Build / deploy tooling.** `scripts/` holds the `build_*`/`deploy_*` tooling: `n8n_deploy.py` (`safe_put()` re-fetches `staticData` immediately before a PUT and fails closed — mandatory for every n8n edit), `deploy_bridge.py` (prepends the `XDG_RUNTIME_DIR` export, retries flaky SSH, restarts the bridge + docker), `build_workflow.py`, `setup-telegram-webhook.sh` (always includes `secret_token`), `backup.sh` / `offsite-backup.sh`, `recycled_lid_audit.py`, `verify-supermemory.py`.
- **Repo HEAD.** The source of truth is `/Users/macbook/projects/dubriani-ai-agent` on branch **`catalog-consolidation-2026-06-02`**; the deployed copy is `/home/ubuntu/hermes-bridge/` on the box (which is **not** a git repo and can drift). `docs/hermes-architecture.md` is stale (describes a 51-node workflow vs the 202-node live file — see Fragilities #35).