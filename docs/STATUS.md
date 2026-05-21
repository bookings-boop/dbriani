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
  autonomous-send branch, bridge `/autosend-check`. **Wiring verified by
  inspection (2026-05-21):** the 🤖 Auto button routes correctly end-to-end —
  button → `Parse Callback` → `Route Action` output 4 → `Set Auto Mode` →
  bridge `/set-mode` (endpoint live). It will work on a fresh draft card; on an
  orphaned card it hits the same null-draft crash as Send (see issue #3). Still
  needs one live operator press to confirm + to diagnose the "~3 taps".

## Known issues — must fix before relying on them

1. **FR-3 debounce is broken by design.** It buffers a customer's messages in
   n8n `staticData`, which n8n does not share across concurrent executions — so
   each line produces its own draft. Needs a **Redis-backed redesign** (Redis
   is already on the box).
2. **Deploy-script queue clobber — FIXED (2026-05-21).** The old `build_*.py`
   each PUT a `staticData` snapshot taken minutes earlier (during the slow,
   flaky-SSH deploy), overwriting any drafts created in that window — this
   orphaned draft cards during the session. Fixed by `scripts/n8n_deploy.py`:
   its `safe_put()` re-fetches `staticData` in the instant before the PUT
   (lost-draft window: minutes → ~1-3s) and warns if the queue shrank. **All
   future deploys MUST go through `n8n_deploy.py`.** The old `build_*.py` are
   spent (they early-return on re-run, so cannot clobber) and are left as-is
   for history. The residual ~1-3s race is covered by issue #3's fix.
3. **`Prepare Send` and the callback handlers crash on a missing draft.** When
   a card is orphaned (`draft_found:false`) the handler hits a null and the
   execution errors. They must fail gracefully ("draft expired — ask the
   customer to resend").

## Pending work (priority order)

1. ~~Fix the deploy-script `staticData` clobber~~ — **DONE (2026-05-21)**:
   `scripts/n8n_deploy.py` `safe_put()`. Future deploys are unblocked.
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
