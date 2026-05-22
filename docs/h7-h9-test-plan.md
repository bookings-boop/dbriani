# H7–H9 Behavioural Test — Autonomous Mode + Caps — RESULTS

**Date:** 2026-05-22
**Status:** ✅ **Suite complete (2026-05-22).** H7 ✓ · H8 ✓ (after the BUG-1 fix) · H8b ✓ · break-conditions ✓ 3/3 · H9 ✓ · `/manual` ✓ · `/caps` ✗ (command not wired).
**Test-run tag:** `test_run=2026-05-22_h7_h9`
**Test phone:** +971509767187 — WhatsApp conversation id `274942918680787@lid`

*(This file previously held the test procedure; it was replaced with the run
results. The procedure is in git history at the commit before this one.)*

## Summary

| | |
|---|---|
| **Works** | Autonomous-mode activation · break-condition detection (`break_condition` emitted + carried onto the draft) · the autonomous branch engages and runs the countdown. |
| **BUG-1 — FIXED** | The `Auto Decide` `staticData` failure is fixed (Redis-backed) and **H8 re-passed** — see the H8 re-test below. |
| **Safe for production** | **Approval mode — yes.** **Autonomous mode — core safety verified:** caps, break-conditions and the `/manual` kill switch all pass; auto-send works. Gaps: `/caps` command not wired (cosmetic) · `Mark Auto Sent` `staticData` residual (cosmetic). |

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

**↻ Re-test after the BUG-1 fix (2026-05-22) — ✅ PASS.** With the Redis-backed
autonomous branch deployed, H8 was re-run: execution **646** ran
`Arm Autosend → Get Autosend → Auto Decide (proceeded) → Auto Send WAHA` — the
auto-send fired, WAHA confirmed delivery to the test phone, `autonomous_sends`
logged `kind=auto`. Root cause resolved — see `docs/bug-1-autonomous-send-fix.md`.

### H8b — 5-consecutive cap — ✅ PASS (direct verification)
The consecutive-cap logic was verified directly against `/autosend-check`
(the `__captest__` run): commit calls 1–5 returned `auto_send:true`, call 6
returned the checkpoint, exactly one `checkpoint` row logged. The live
5-countdown version was skipped (~20–40 min of countdowns, no new evidence).

### Break-condition tests — ✅ 3/3 PASS
Test conversation autonomous; each break message flipped it back to
`approval` via `Find Break → Break Check → Flip To Approval → Break Alert` —
no auto-send, `break_reason` recorded, Telegram alert fired.

| Condition | break_condition | Execution |
|---|---|---|
| `discount_request` ("too expensive… a discount?") | `hit:true` | 650 |
| `human_request` ("…a real person, not a bot") | `hit:true` | 651 |
| `negative_sentiment` ("so frustrating… not happy…") | `hit:true` | 652 |

### H9 — take-back — ✅ PASS (demonstrated)
No separate run needed: each of the three break tests flipped the
conversation `autonomous → approval` with a recorded `break_reason`.

### `/caps` — ❌ FAIL — command not wired
Sending `/caps` fell through `Process Text Reply → Route Text Action →
Ack No Pending` (the "no pending draft" fallback) — no cap status returned.
The **bridge `/caps` endpoint works** (a direct call returns the status), but
**nothing in the workflow routes the `/caps` command to it** — `Process Text
Reply` has no `/caps` branch. Minor — operator convenience, not a safety
path. Backlogged as BUG-2.

### `/manual` — kill switch — ✅ PASS
Execution 655 ran `Process Text Reply → Route Text Action → Hermes Set Mode →
Confirm Mode → Send Mode Reply`; `manual_killswitch` flipped every
conversation to `approval` (`activated_by=manual_killswitch`) — **0
autonomous remaining**.

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
