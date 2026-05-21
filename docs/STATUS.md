# Dubriani AI Agent — Current Status

**As of 2026-05-21, end of an extended build session.** Snapshot of where
everything actually stands. Chronological detail is in `docs/decisions.md`.

## Live system

- n8n workflow "Dubriani Phase 1B" — **82 nodes, active**. The box is healthy
  (RAM free, n8n stable, executions succeeding).
- **Core flow works (operator-confirmed):** customer WhatsApp message → Claude
  draft → Telegram card → operator taps ✅ Send → reply sent over WhatsApp.

## Deployed & confirmed working

- **FR-1** — Edit button → type feedback → Claude re-drafts (refine loop).
- **FR-2** — `/lead` outbound new-lead intake.
- **Reply-to-Claude rule** — replying to a draft card routes the text to Claude
  as feedback; only `send this: X` sends verbatim; `ask this: X` re-drafts.

## Deployed but NOT verified

- **FR-5** — background improver + learning loop (bridge `/improve`, `/learn`,
  `/rules`). Never behaviourally tested.
- **FR-4** — autonomous mode: 🤖 Auto button, `/auto` `/manual`, the
  autonomous-send branch, bridge `/autosend-check`. The Auto button is sluggish
  ("~3 taps"); never behaviourally tested. Dormant until a conversation is on
  `/auto`.

## Known issues — must fix before relying on them

1. **FR-3 debounce is broken by design.** It buffers a customer's messages in
   n8n `staticData`, which n8n does not share across concurrent executions — so
   each line produces its own draft. Needs a **Redis-backed redesign** (Redis
   is already on the box).
2. **The deploy scripts clobber the live draft queue.** Each `build_*.py` PUTs
   the workflow with a `staticData` snapshot taken minutes earlier (during the
   slow, flaky-SSH deploy). Drafts created in that window are overwritten —
   this orphaned draft cards during the session. **Before any future deploy:**
   the deploy must re-fetch `staticData` immediately before the PUT (or omit it
   so n8n keeps the live copy).
3. **`Prepare Send` and the callback handlers crash on a missing draft.** When
   a card is orphaned (`draft_found:false`) the handler hits a null and the
   execution errors. They must fail gracefully ("draft expired — ask the
   customer to resend").

## Pending work (priority order)

1. Fix the deploy-script `staticData` clobber — gate for everything below.
2. Make `Prepare Send` / callback handlers graceful on a missing draft.
3. FR-3 — Redis-backed debounce redesign.
4. FR-4 / FR-5 — a real operator test pass to verify or find the bugs.
5. PDF-attachment handling — operator attaches a PDF → Claude drafts from it.
6. Rotate the exposed secrets (`N8N_API_KEY`, admin bot token).
7. `overnight-build` branch — not merged to `main`; hold until 1–4 are done.

## Key files

- `docs/decisions.md` — full chronological build log + every decision.
- `docs/feature-backlog.md` — FR-1…FR-5 specifications.
- `docs/fr4-autonomous-design.md`, `docs/hermes-revival-design.md` — designs.
- `scripts/build_*.py` — the workflow-surgery deploy scripts.
- Workflow backups: `workflows/phase-1b-telegram.PRE-*.json` (gitignored).
