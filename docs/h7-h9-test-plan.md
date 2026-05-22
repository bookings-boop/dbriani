# H7–H9 Behavioural Test — Autonomous Mode + Caps — RESULTS

**Date:** 2026-05-22
**Status:** ⛔ **HALTED at H8** — autonomous auto-send does not reliably fire.
**Test-run tag:** `test_run=2026-05-22_h7_h9`
**Test phone:** +971509767187 — WhatsApp conversation id `274942918680787@lid`

*(This file previously held the test procedure; it was replaced with the run
results. The procedure is in git history at the commit before this one.)*

## Summary

| | |
|---|---|
| **Works** | Autonomous-mode activation · break-condition detection (`break_condition` emitted + carried onto the draft) · the autonomous branch engages and runs the countdown. |
| **Broken** | **Autonomous auto-send does not fire** — the branch aborts at `Auto Decide` after the wait. |
| **Safe for production** | **Approval mode — yes** (unchanged, operator-confirmed earlier). **Autonomous mode — NO.** Do not enable it for real customers until the `Auto Decide` bug is fixed. |

## Prerequisites — all verified (2026-05-22)

2A `evaluate_caps` (`0b1afae`) · 2B dead branch removed (`92c43d1`) · 2C error
alerts (`b5166c0`) · 3 draft guard (`415407c`,`b8da4e7`) — all committed +
deployed. Live workflow 91 nodes, active. Bridge boot-safe (enabled +
`Linger=yes`). Docker `unless-stopped` ×4. WAHA session `default` WORKING.

## Results

### H7 — Activate autonomous mode — ✅ PASS
`/set-mode autonomous` on `274942918680787@lid` → effective mode `autonomous`;
`/autosend-check` gate confirmed `auto_send:true`.
*Note:* WhatsApp delivers this phone under the linked-id `274942918680787@lid`,
not `971509767187@c.us`. The first H7 attempt targeted the raw number (wrong
id) and was redone against the `@lid`. Lesson: identify the conversation by
the id WAHA actually delivers, not the phone number.

### H8 — Verify auto-send fires — ❌ FAIL
- Test message → draft `1779431894763_w8sav` created · `break_condition:{hit:false}` (detection ✓).
- Autonomous branch engaged: `Find Break → Break Check → Auto Prep → Auto Gate
  → Auto Is Autonomous → Render Auto Card → Auto Wait (270s) → Auto Decide`.
- **`Auto Decide` returned `[]`** — branch stopped. `Auto Commit` /
  `Auto Send Gate` / `Auto Send WAHA` never ran. Zero `autonomous_sends`
  `kind=auto` rows. No message delivered to the test phone.
- Execution 645, status `success` — the branch ended cleanly; it simply did
  not send.

### H8b · break-condition tests · `/caps` · H9 · `/manual` — ⏸️ NOT RUN
Suite halted — every remaining test depends on auto-send firing, which H8
proved it does not.

## Root cause

`Auto Decide` re-checks the draft after the countdown —
`pendingQueue.find(id).status === 'pending'`. The draft *is* pending, but
`Auto Decide`'s lookup reads the draft queue from n8n **`staticData`** after
the 270-second `Auto Wait`, and returned no usable result, so it bailed.

This is the **n8n `staticData` unreliability** — the same architectural flaw
that made FR-3's debounce broken-by-design. `staticData` is not consistent
across a Wait-node resume or across concurrent executions, so the autonomous
branch's post-wait `Auto Decide` recheck cannot be trusted.

It **fails safe**: the draft just remains a normal pending approval card —
nothing wrong is auto-sent.

## Action — backlogged

`docs/feature-backlog.md` → **BUG-1**: *autonomous-send branch — `Auto Decide`'s
`staticData` recheck is unreliable across the `Auto Wait` resume; needs the
Redis-backed state redesign (the same fix FR-3 needs).*

## Test data + cleanup

Test conversation `274942918680787@lid` was deactivated → `approval` at the
end of the run. The stray wrong-id row `971509767187@c.us` is also `approval`.

- `conversation_modes` rows tagged `test_run=2026-05-22_h7_h9`: id 26–29.
- `autonomous_sends`: `kind=intervention` rows logged by the 4 `/set-mode`
  calls — not individually tagged.
- `pendingQueue` test drafts: `…_6kcln`, `…_d0ne9`, `…_w8sav`.

⚠️ `274942918680787@lid` also holds **pre-test** history (used in earlier
sessions) — clean selectively, not by a blanket `customer_id` delete.

Cleanup (run as the `n8n` Postgres superuser — `hermes_rw` has no DELETE):
```sql
DELETE FROM conversation_modes WHERE activated_by LIKE '%test_run=2026-05-22_h7_h9%';
-- autonomous_sends + pendingQueue test drafts: review first — the @lid id
-- has pre-test history that should NOT be deleted.
```
