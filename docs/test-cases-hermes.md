# Hermes Integration — Test Checklist

Covers the Hermes integration (Steps 3d–9). Mirrors `docs/test-cases.md`
(the original Phase 1B T1–T6, still valid).

Legend: ✅ verified during the build · ⏳ needs an operator run · ⚠️ untested

---

## Verified during the build (curl / execution logs)

- ✅ **Bridge `/draft`** — returns `{ok, messages[], notes_for_zayn, …}`;
  live execution #286 ran `… → Call Hermes Bridge → Parse Response → … →
  Send Draft to Telegram` clean.
- ✅ **Bridge `/save-rule`** — inserts a `behavior_rules` row, `active=false`,
  returns a clean numeric `rule_id`.
- ✅ **Rule injection** — a test rule ("end every message with [rule-ok]")
  appeared in the draft, then was removed.
- ✅ **Trigger detection** — "I'll send the deposit tomorrow" → a
  `customer_triggers` row (`payment_promised`, reminder +30h).
- ✅ **Conversation health** — a price-objection message scored `at_risk`,
  prepended to notes + persisted to `conversation_health`.
- ✅ **`/set-mode`** + **`/manual`** — set a test customer `autonomous`, then
  `/manual` (`__ALL__`) returned it to `approval`.
- ✅ **Reminder cron** — runs every 15 min; correctly skips quiet hours.
- ✅ **Daily summary cron** — test run delivered a digest to Telegram.
- ✅ **Operator-reported:** drafting on the "Dubriani Hermes" bot + the Step 4
  refinement loop ("both passed").

## Operator test cases — please run

| # | Step | Action | Expected |
|---|------|--------|----------|
| H1 ⏳ | Drafting | Send a WhatsApp test message | Draft card from "Dubriani Hermes" with ✅/✏️/🔁/❌ buttons |
| H2 ⏳ | Refine | Reply to a pending draft: *"make it shorter"* | Card updates → "✏️ REFINED" + new draft |
| H3 ⏳ | Rule capture | Reply to a draft: *"always greet this customer formally"* | A "🧠 Save as rule?" message with a 💾 button appears |
| H4 ⏳ | Rule save | Tap **💾 Save as rule** | "✅ Rule saved (id N) — INACTIVE…"; row in `behavior_rules` |
| H5 ⏳ | Rule activate | `UPDATE behavior_rules SET active=true WHERE id=N;` then send a msg from that customer | The reply reflects the rule |
| H6 ⏳ | Trigger reminder | After a customer promises a time-bound action, wait for `reminder_date` (daytime) | "⏰ Follow-up due" message within 15 min |
| H7 ⚠️ | Autonomous on | Reply `let it run` to a draft from a **test number** | "✅ … → autonomous" confirmation |
| H8 ⚠️ | Auto-send | Message again from that test number | Reply **auto-sends** (no card) + "🤖 AUTO-SENT" notice |
| H9 ⚠️ | Take back | Reply `take back` to one of its drafts | Returns to `approval` |
| H10 ⏳ | Kill switch | Send `/manual` | "all conversations set to approval" |
| H11 ⏳ | Daily digest | Wait for 08:00 Dubai (or run the cron script) | Digest message arrives |

H7–H9 (autonomous auto-send) are **untested** — run them with a number you
control before using autonomous mode on real customers. See
`docs/autonomous-mode.md`.

## Rollback
Every deploy backed up the live workflow to
`workflows/phase-1b-telegram.LIVE-backup-*.json`. To roll back, `PUT` a backup
via the n8n API (or import it in the UI). The bridge: `git checkout` an earlier
`hermes-bridge/server.py` and `systemctl --user restart hermes-bridge`.
