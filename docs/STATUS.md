# Dubriani AI Agent — Current Status

**As of 2026-05-22, end of the extended build session.** Honest snapshot.
Chronological detail: `docs/decisions.md`. Build history is on `main`.

## Live system

- n8n workflow "Dubriani Phase 1B" — **91 nodes, active**.
- Hermes bridge — deployed, **boot-safe** (`systemctl --user` enabled +
  user lingering on).
- WAHA WhatsApp session `default` — **WORKING** (+971 58 950 2303).
- Docker stack (n8n / waha / postgres / redis / caddy) — `unless-stopped`.

## Works — confirmed

- **Approval-mode core flow** (operator-confirmed): customer message →
  Claude draft → Telegram card → operator ✅ Send → WhatsApp reply.
- **FR-1** Edit→feedback loop · **FR-2** `/lead` · reply-to-card→Claude rule.
- **Code-review fixes** (all deployed, verified): #1 `evaluate_caps`
  double-count/QC, #2 dead `/draft` branch removed, #3 autosend error outputs
  → Telegram alerts.
- **Draft guard** — Send / Auto / Skip / Regen / Edit show "draft expired"
  instead of crashing on an orphaned card (verified end-to-end).
- **`scripts/n8n_deploy.py`** — safe-deploy helper; re-fetches `staticData`
  before every PUT. **All workflow deploys go through it.**
- **Break-condition detection** — the draft prompt emits `break_condition`
  and it is carried onto the queued draft (verified in H8). The break-gate
  (`Find Break → Break Check → Flip To Approval`) is deployed.

## Broken / not safe

- 🔴 **BUG-1 — autonomous auto-send does not fire.** After the countdown,
  `Auto Decide` rechecks the draft via n8n `staticData` and silently aborts.
  **Autonomous mode is non-functional — keep it OFF for real customers.**
  Fails safe (drafts just wait as approval cards). See
  `docs/h7-h9-test-plan.md` + `docs/feature-backlog.md` BUG-1.
- 🔴 **FR-3 debounce — broken by design.** Same root cause as BUG-1: n8n
  `staticData` is not reliable across concurrent executions / Wait resumes.
- 🟡 **FR-5** background improver + learning loop — deployed, never
  behaviourally tested.

## Git

All session work is **merged to `main`** (`131ca2a`, 53 commits).
`overnight-build` is fully merged (`== main`).

## Pending work (priority order)

1. **BUG-1 / FR-3 — Redis-backed state redesign.** Move draft/queue state
   off n8n `staticData` onto Redis (already on the box). Fixes autonomous
   auto-send *and* the FR-3 debounce. A real architecture rebuild — its own
   brainstorm → spec → plan → build.
2. **Rotate exposed secrets** — `N8N_API_KEY` + the Telegram bot token.
   Runbook: `docs/secret-rotation.md` (operator-driven).
3. FR-5 — a real behavioural test of the improver + learning loop.
4. PDF-attachment handling — operator attaches a PDF → Claude drafts from it.

## Key files

- `docs/decisions.md` — chronological build log.
- `docs/feature-backlog.md` — FR-1…FR-5 specs + **BUG-1**.
- `docs/h7-h9-test-plan.md` — autonomous-mode test results (H8 FAIL).
- `docs/break-condition-detection-design.md` / `-plan.md` — that feature.
- `docs/secret-rotation.md` — secret-rotation runbook.
- `scripts/n8n_deploy.py` — the only sanctioned workflow-deploy path.
- Workflow backups: `workflows/phase-1b-telegram.PRE-*.json` (gitignored).
