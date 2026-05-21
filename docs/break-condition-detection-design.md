# Break-Condition Detection for Autonomous Mode — Design

**Date:** 2026-05-22
**Status:** Approved — ready for implementation planning
**Origin:** Discovered during H7–H9 test prep — autonomous mode has no
content-based break; it would auto-send to a customer demanding a discount,
asking for a human, or venting anger, with no human checkpoint.

## Problem

The autonomous-send decision is gated only by conversation mode and the
safety caps (`evaluate_caps` — daily / consecutive / QC sample). Message
*content* never influences whether an auto-send fires. The
`conversation_modes.break_conditions` / `break_reason` columns exist but are
unused — only the `/manual` kill switch writes `break_reason`.

## Goal

When an autonomous conversation's incoming customer message hits a break
condition, do NOT auto-send. Instead: flip the conversation to `approval`
mode, record the reason, and alert the operator. The draft stays a normal
approval card for the operator to handle.

## Decisions (brainstorm, 2026-05-22)

| Decision | Choice |
|---|---|
| Detection mechanism | LLM-judged — the drafting model emits a structured flag |
| "Pricing" as a condition | Dropped — normal price questions are handled autonomously |
| Break action | Flip the conversation to `approval` + record `break_reason` + Telegram alert |

## The 3 break conditions

LLM-judged from the customer's incoming message (intent, not keywords):

- **`discount_request`** — asks for a discount, a lower price, "best price",
  says "too expensive", or haggles.
- **`human_request`** — wants a person / human / manager / "real person", or
  to be transferred off the bot.
- **`negative_sentiment`** — clearly upset, angry, frustrated, or complaining.
  NOT mild hesitation or an ordinary objection.

A normal pricing question ("how much for the 80ft on Saturday?") is **not** a
break — the AI has full pricing data and answers it autonomously.

## Detection

The initial-draft prompt (`Build Prompt` → `Claude AI` node) is extended so
the model judges break conditions and adds a field to its JSON output:

```json
"break_condition": {
  "hit": true,
  "reason": "discount_request",
  "detail": "<one short line; present only when hit is true>"
}
```

`reason` is one of `discount_request` | `human_request` | `negative_sentiment`.
When no condition is hit the model returns `{"hit": false}`.

Only the **initial inbound-draft path** needs this — an autonomous send only
ever fires on the initial draft of a new customer message. Regen / Refine /
Lead prompts are out of scope.

The draft-parse node carries `break_condition` onto the queued draft object
(alongside `customer_phone`, `draft_text`, `messages`, …) so the workflow can
read it the same way `Auto Prep` reads the draft.

## Flow

The autonomous branch hangs off `Save Telegram MsgID → Auto Prep`. A gate is
inserted before `Auto Prep`:

```
Save Telegram MsgID
  └─▶ Break Check ─┬─ break_condition.hit == false ─▶ Auto Prep ─▶ Auto Gate ─▶ … (autonomous, unchanged)
                   └─ break_condition.hit == true  ─▶ Flip To Approval ─▶ Break Alert ─▶ (end)
```

- **Break Check** — reads `break_condition` from the queued draft; routes on
  `hit`.
- **Flip To Approval** — `POST` bridge `/set-mode` with
  `{customer_id, mode:"approval", activated_by:"break_detection",
  break_reason:"<reason>: <detail>"}`.
- **Break Alert** — `POST` Telegram `sendMessage` to the admin chat:
  *"⏸️ AUTONOMOUS PAUSED — &lt;customer&gt; — &lt;reason&gt; — '&lt;detail&gt;'.
  Conversation set back to approval; the draft is waiting for you."*

On a break the autonomous branch is never entered → no countdown card, no
auto-send. The normal approval card (already posted upstream, before the
`Auto Prep` fork) stays as-is, with its Send/Edit/Regen/Skip buttons.

## Components changed

1. **Bridge `server.py`** — `/set-mode` and `set_mode()` accept an optional
   `break_reason`, written into the `conversation_modes` INSERT.
   Backward-compatible — existing callers that omit it are unaffected.
2. **Prompt + parse** — `Build Prompt` gains the break-condition instruction;
   the initial-draft parse node extracts `break_condition` onto the draft.
3. **Workflow** — 3 new nodes (`Break Check`, `Flip To Approval`,
   `Break Alert`) forming an isolated sub-branch at the autonomous entry. No
   existing node's logic is modified. 87 → 90 nodes. Deployed via
   `n8n_deploy.safe_put`.

## Error handling

- Missing / unparseable `break_condition` → treated as `hit: false`, and
  logged. Break detection is an *added* safety layer; a parse miss falls back
  to today's caps-gated autonomous behaviour — not worse than pre-feature.
- `Flip To Approval` and `Break Alert` run with `onError:
  continueRegularOutput` so a failed Telegram alert still leaves the
  conversation flipped. `Flip To Approval` is ordered first — it is the
  critical call.

## Testing

- Unit-test `Break Check`'s routing logic for hit / no-hit / missing-field
  inputs.
- H7–H9 break-condition tests become runnable: test phone (in autonomous
  mode) sends a discount request / human request / angry message → expect no
  auto-send, the conversation flipped to `approval` (verify in
  `conversation_modes` with the `break_reason`), and a Telegram alert naming
  the reason.

## Out of scope

- Regen / Refine / Lead prompts — autonomous-send fires only on initial drafts.
- Auto-resuming autonomous mode after a break — the operator re-enables it
  manually (existing 🤖 Auto button / `/auto`).
- Tuning the `negative_sentiment` threshold beyond "clearly upset" — revisit
  if it over- or under-fires in practice.
