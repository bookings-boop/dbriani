# Hermes Integration — Architecture

How the Hermes Agent is wired into the Dubriani Phase 1B workflow.

## Overview

```
 WhatsApp ──► WAHA ──► N8N workflow ──► hermes-bridge ──► Hermes Agent ──► Claude
                          │   ▲              (HTTP)        (hermes chat -q)
                          │   │
                          ▼   │
                  Telegram "Dubriani Hermes" bot
                  (draft approval / refine / rules / mode)
                          │
                          ▼
                       Postgres  (behavior_rules, customer_triggers,
                                  conversation_modes, conversation_health)
```

The N8N workflow stays the orchestrator and the approval gate. Hermes is the
drafting brain, reached through **hermes-bridge** — a thin HTTP service,
because Hermes has no native request/response API.

## Components

### hermes-bridge  (`~/hermes-bridge/` on the EC2 box)
A stdlib-only Python HTTP service, systemd user service `hermes-bridge.service`,
listening on `0.0.0.0:8788` (reached from the n8n container at `172.18.0.1:8788`).
Token-auth (`X-Bridge-Token`). Endpoints:
- `POST /draft` — customer context → `hermes chat -q … -Q` → JSON draft
  (`messages`, `notes_for_zayn`, `health`, `detected_trigger`, `suggested_rule`
  on refine, `conversation_mode`).
- `POST /save-rule` — insert a `behavior_rules` row (`active=false`).
- `POST /set-mode` — set a conversation's mode (`__ALL__` = `/manual`).
- `GET /health`.
It reads/writes Postgres by `docker exec n8n-postgres-1 psql` (role `hermes_rw`,
scoped to the 4 Hermes tables).

### N8N workflow  (`Dubriani Phase 1B — WhatsApp + Telegram Approval`, 51 nodes)
- **Drafting:** `WAHA Webhook → Filter Inbound → Get Chat History →
  Format Context → Call Hermes Bridge → Parse Response → IF Autonomous →`
  approval (`Queue & Format → Send Draft to Telegram`) **or** auto-send
  (`Auto-Send to Customer → Auto-Send FYI`).
- **Approval callbacks:** Send / Edit / Regen / Skip / Save-rule.
- **Operator text:** refine a pending draft · verbatim send · mode commands
  (`let it run` / `take back` / `pause` / `/manual`).
- **R1:** every bridge/WAHA/Telegram node error-branches to `Build Alert →
  Alert Zayn` — nothing hard-fails.

### Hermes Agent  (`~/.hermes/` on the box)
v0.14.0, model `anthropic/claude-sonnet-4-6`. The bridge invokes it headless
(`hermes chat -q "<prompt>" -Q --source tool --yolo -t memory`). The Dubriani
system prompt lives at `~/hermes-bridge/system-prompt.md`.

### Postgres tables (db `n8n`)
`behavior_rules` · `customer_triggers` · `conversation_modes` ·
`conversation_health`. Roles: `hermes_rw` (bridge, RW on those 4),
`hermes_ro` (read-only, currently unused).

### Cron (system crontab, user `ubuntu`)
- `*/15 * * * *` — `cron-reminders.py` (due-trigger reminders, quiet-hours aware)
- `0 4 * * *` UTC — `cron-daily-summary.py` (08:00 Dubai daily digest)

## Telegram bots
- **Dubriani Hermes** (`8861…`) — the live approval/refinement/admin surface.
- old `8569…` bot — retired (webhook deleted); reserved.

## Source of truth & deploy
- `system-prompt.md` → built into the workflow by `scripts/build_workflow.py`.
- `scripts/hermes_integration.py` — idempotent workflow surgery (Steps 3d/4/5/8).
- `scripts/deploy_workflow.py` — deploys the workflow via the n8n REST API
  over SSH; backs up the live workflow first.
- `scripts/n8n_check.py` — inspects recent executions.
- The bridge files are deployed by `scp`/`cat` to `~/hermes-bridge/`.

See `docs/decisions.md` for build decisions and `docs/autonomous-mode.md` for
the autonomous-mode design + its untested/deferred parts.
