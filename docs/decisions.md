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

(appended as the autonomous build proceeds)
