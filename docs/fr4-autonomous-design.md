# FR-4 — Supervised Autonomous Mode: Implementation Design

**Status:** design — 2026-05-21. Operator explicitly authorised the build (the
first feature that lets the agent message a customer without approval).
Feature spec: `docs/feature-backlog.md` (FR-4). This document is the n8n/bridge
implementation design and the sub-step plan.

---

## Principle

A conversation is **opt-in, per-conversation**. Default is always
approval-only. When the operator presses **🤖 Auto** on a draft card, *that
conversation* goes autonomous: Claude keeps replying, but every reply is
posted to Telegram and waits out a **randomised 1–5 min window** before it
auto-sends — during which the operator can intervene. Take Over (or the
`/manual` kill switch) returns it to approval.

## Mode + caps — bridge-owned (already built)

The bridge already has the machinery (overnight build, intact):
`conversation_modes` table, `autonomous_sends` table, `get_mode` / `set_mode` /
`manual_killswitch` / `evaluate_caps` / `log_autosend`, `/set-mode`, `/caps`,
and the §5.7 caps (daily ≤20, consecutive checkpoint, QC sampling — all ON).

**New bridge endpoint — `POST /autosend-check`** `{customer_id, commit}`:
- reads `get_mode`; if mode ≠ `autonomous` → `{mode, auto_send:false}`
- if autonomous → `evaluate_caps`; if `commit` and caps pass → `log_autosend(auto)`
- returns `{mode, auto_send, reason}`

The workflow calls it: once with `commit:false` to decide whether to render an
Auto card, and once with `commit:true` at the moment of auto-send.

## Card states

| State | Header | Buttons |
|-------|--------|---------|
| **Normal** (approval) | `📩 from …` | Send · Edit · Regen · Skip · **🤖 Auto** |
| **Auto** (autonomous) | `🤖 AUTO — auto-sends ~HH:MM unless you act` | Edit · Send now · Cancel · Take over |

`Queue & Format` always renders the **normal** card (keeps the hot path fast).
If the conversation is autonomous, the auto-send branch immediately *edits* it
into the Auto card. The countdown is shown as a **static estimate**
(`~HH:MM` / `~4 min`), not a live ticking edit — same intent, far cheaper.

## New callback actions (Route Action → 6 outputs)

- **`auto`** — enter autonomous mode: bridge `/set-mode` (→ `autonomous`),
  then kick *this* draft's delayed auto-send.
- **`takeover`** — exit: bridge `/set-mode` (→ `approval`), re-render the
  normal card.
- **Send now / Cancel / Edit** on the Auto card reuse the existing
  `send` / `skip` / `edit` callbacks — no new handlers.

## The autonomous-send branch (n8n)

A second branch off `Save Telegram MsgID` (parallel to the FR-5 improver — the
improver still runs at 20s, so the auto-sent message is the *improved* one):

```
Save Telegram MsgID
  → Auto Gate          bridge /autosend-check {commit:false}; stop unless autonomous
  → Render Auto Card   edit the card → Auto card + countdown estimate
  → Auto Wait          random 1–5 min
  → Auto Send Decide   re-check draft still 'pending' (operator wins) +
                       bridge /autosend-check {commit:true}
  → [auto_send]  Auto Send (WAHA) → Mark Sent → edit card "🤖 auto-sent ✓"
    [blocked]    leave the card for approval (caps checkpoint, or operator acted)
```

**Race safety:** `Auto Send Decide` re-reads the draft `status` immediately
before sending — if the operator pressed Send / Cancel / Take over during the
window, the auto-send is abandoned. Operator action always wins.

## Safety summary

- Per-conversation opt-in; default approval; nothing auto-enables.
- Caps (daily / consecutive checkpoint / quiet-hours) — bridge `evaluate_caps`,
  all ON; a blocked cap routes the draft back to approval.
- Take Over button + `/manual` kill switch → instant return to approval.
- The `pending` re-check means operator intervention overrides the timer.
- Build it last; behaviourally test before real use.

## Build sub-steps (each deployed + checkpointed)

- **4-A** — bridge `/autosend-check` endpoint. Bridge-only, safe, no auto-send.
- **4-B** — the `🤖 Auto` button + `auto` / `takeover` callbacks + mode set and
  card re-render. Mode plumbing only — *still no auto-send happens.*
- **4-C** — the autonomous-send branch (the delayed auto-send + caps gate).
  This is the step that actually crosses into autonomous sending.
- **4-D** — behavioural test on a controlled test number before real use.

Card-render note: adding the 5th button touches every card-render point
(`Queue & Format`, `Edit Telegram (Regen)`, `Edit Telegram (Refine)`,
`Apply Improvement`) — handled in 4-B.
