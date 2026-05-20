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
