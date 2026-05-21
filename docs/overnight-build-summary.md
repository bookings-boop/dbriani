# Overnight Build Summary — Hermes Integration

**Session:** 2026-05-20 → 05-21, autonomous.
**Branch:** `overnight-build` (review, then merge to `main`).
**Scope requested:** finish rule capture; trigger detection + reminder cron;
conversation health; autonomous mode; daily summary; testing checklist; docs.

---

## ✅ Completed & deployed

| Step | What | State |
|------|------|-------|
| 5.3 | **Permanent rule capture** — a refinement Hermes judges "durable" → a 🧠 *Save as rule* button → bridge `/save-rule` → `behavior_rules` row (`active=false`) | live |
| 5.2 | **Rule injection** — active `behavior_rules` injected into every draft | live (verified) |
| 6 | **Trigger detection** — Hermes detects time-bound customer commitments → `customer_triggers` row; **`cron-reminders.py`** every 15 min sends due-trigger reminders (quiet-hours aware) | live (verified) |
| 7 | **Conversation health** — every draft scored good/warm/at_risk/cold; prepended to operator notes + persisted to `conversation_health` | live (verified) |
| 8 | **Autonomous mode** — `conversation_modes`, `IF Autonomous` auto-send branch, operator commands (`let it run`/`take back`/`pause`/`/manual`) | **deployed but dormant** — see warnings |
| 9 | **Daily summary** — `cron-daily-summary.py` at 08:00 Dubai: rules/triggers/health/modes digest to Telegram | live (test digest delivered) |
| 10 | **Testing checklist** — `docs/test-cases-hermes.md` | done |
| 11 | **Documentation** — `hermes-architecture.md`, `decisions.md`, `autonomous-mode.md`, README | done |

Live workflow: **51 nodes**, active. Bridge + gateway services active. Hermes
tables clean (all test rows removed).

## 🔑 Key decisions (full log in `docs/decisions.md`)

- **Captured rules save `active=false`** — per your rule, they require your
  approval before they shape drafts. Activate with
  `UPDATE behavior_rules SET active=true WHERE id=<id>;`.
- **Autonomous mode deployed but dormant** — every conversation defaults to
  `approval`; `IF Autonomous` fail-closes to the approval branch; nothing
  auto-activates. (You came online, so it was built in full rather than
  deferred — but see warnings.)
- **Reminders + digest go to the "Dubriani Hermes" bot** (one operator chat);
  quiet hours 22:00–08:00 Dubai for proactive messages.
- **Bridge ↔ Postgres via `docker exec psql`** (stdlib-only bridge, no pip deps).

## ⚠️ Warnings / untested / flagged for review

1. **Autonomous mode is NOT behaviourally tested.** The auto-send branch is
   deployed but has never fired (testing needs a live autonomous conversation).
   **Run tests H7–H9 in `docs/test-cases-hermes.md` with a number you control
   before using it on real customers.**
2. **Autonomous-mode safety caps NOT built** (spec §5.7) — no daily cap, no
   per-conversation cap, no automatic break conditions. A conversation set to
   `autonomous` auto-sends every reply until you `take back` / `/manual`.
   Recommend adding caps before real use. (`docs/autonomous-mode.md`.)
3. **Manual trigger resolution deferred** — reminders fire once; marking a
   trigger resolved/dismissed is a SQL update for now.
4. **R1 error alerts are not quiet-hours gated** — an error alert can arrive at
   night. Judged low-value vs. effort (informational, "nothing force-sent").
5. No test failures encountered. Each step was verified as built (curl tests,
   execution logs, structural checks).

## 👉 Pending / needs you

- **Review + merge** branch `overnight-build` → `main`.
- **Run operator test cases** H1–H11 (`docs/test-cases-hermes.md`), especially
  H7–H9 (autonomous) before relying on autonomous mode.
- **Rotate exposed secrets** — the `N8N_API_KEY` and the admin bot token were
  pasted in chat earlier; regenerate them when convenient.
- Decide whether to add the autonomous-mode safety caps (warning #2).
- Optional: activate the old `8569` bot as a separate admin bot (currently the
  "Dubriani Hermes" bot carries everything).

## Commits on `overnight-build`
`cf805d2` baseline (Steps 3d–5.3 built) · `e4f1313` Step 6 · `ca5e75e` Step 7 ·
`9f40f40` Step 8 · `d999997` Step 9 · (+ docs commit).

## Rollback
Live-workflow backups: `workflows/phase-1b-telegram.LIVE-backup-*.json` and
`*.bak.pre-step*`. `PUT` one back via the n8n API to roll back.

---

## Post-summary update (2026-05-21)

- **Autonomous-mode safety caps — BUILT & deployed** (resolves warning #2):
  daily cap (20/day, resets 00:00 Dubai), per-conversation checkpoint (every 5
  consecutive), 5% QC sampling; `/caps` command; new `autonomous_sends` table.
  Auto-send is gated on the cap decision, fail-closed. 53-node workflow.
- **Anthropic auth blocker — occurred mid-build, now RESOLVED.** Drafting
  failed (`401 invalid x-api-key` / `400` usage limit). Root cause:
  `~/.claude/.credentials.json` was overriding the Hermes credential pool; the
  operator disabled it and moved to API-key credentials. Drafting verified
  restored (live `/draft`, 7.4s).
- **Data-loss check (requested):** no loss — `behavior_rules` /
  `customer_triggers` / `conversation_modes` are empty because they are new
  tables whose only-ever rows were build-time test data (id sequences confirm
  14 test inserts total, all cleaned up). `conversation_health` has 1 real row;
  the 52-entry draft queue in `staticData` is intact. See chat log.
- **Outstanding:** run H7–H9 + cap tests (`docs/h7-h9-test-plan.md`) — the cap
  auto-send gating is built + structurally verified, not yet behaviourally
  tested (needs a live autonomous conversation).
