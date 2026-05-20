# Dubriani AI Agent

WhatsApp + Gmail AI agent for **Dubriani Yachts** (Dubai luxury yacht charters).

Customer message → Claude drafts a grounded reply (in the voice of "Maria") →
Zayn approves via a **Telegram bot with inline buttons** → reply sent over
WhatsApp → logged. **Zero autonomous sends — every message is approved first.**

The agent runs as an N8N workflow on EC2. This repo is the version-controlled
source for the workflow, the system prompt, and the operational tooling.

---

## Status

| Phase | What | State |
|-------|------|-------|
| 1A | WhatsApp admin approval (14 nodes) | Live in N8N |
| 1B | Telegram approval bot — Send / Edit / Regen / Skip | In delivery — steps 0–4 |

Execution scope is **steps 0–4 only** (see `HANDOFF.md` §0). Everything beyond
— HubSpot, Postgres, Gmail, weekly distillation, Redis, the full 7-button UI —
is deferred until 0–4 are validated on real traffic for ~1 week.

| # | Step | State |
|---|------|-------|
| 0 | Rotate Anthropic API key | Done |
| 1 | Git repo + `.env` externalization | Done |
| 2 | Verify Supermemory contents + tag schema | Pending |
| 3 | Tighten Supermemory search with `containerTags` | Pending |
| 4 | Deploy Phase 1B (Telegram approval) | Pending |

---

## Repo layout

```
dubriani-ai-agent/
├── .env                          secrets (gitignored — create from .env.example)
├── .env.example                  env var template
├── .gitignore
├── README.md                     this file
├── HANDOFF.md                    full project handoff (secrets redacted)
├── system-prompt.md              canonical agent system prompt — SINGLE SOURCE
├── workflows/
│   ├── phase-1b-telegram.json         committed workflow (no secrets)
│   └── phase-1b-telegram.local.json   delivered bootstrap copy (gitignored)
├── scripts/
│   ├── build_workflow.py         regenerates the workflow from system-prompt.md
│   ├── verify-supermemory.py     Step 2 — probes Supermemory state + tags
│   └── setup-telegram-webhook.sh registers the Telegram webhook with N8N
└── docs/
    ├── credentials-map.md        how the 4 secrets reach N8N
    └── test-cases.md             T1–T6 deployment acceptance tests
```

---

## How secrets are handled

Nothing sensitive is ever committed. The committed `workflows/phase-1b-telegram.json`
contains **no secrets**:

| Secret | How it reaches the workflow |
|--------|-----------------------------|
| Anthropic API key | N8N credential `Anthropic API` (httpHeaderAuth) |
| WAHA API key | N8N credential `WAHA API` (httpHeaderAuth) |
| Supermemory token | N8N credential `Bearer Auth account` (httpBearerAuth) |
| Telegram bot token | `{{ $env.TELEGRAM_BOT_TOKEN }}` — N8N container env var |

See `docs/credentials-map.md` for setup. Real values live only in `.env`
(gitignored) and inside N8N.

---

## Editing the workflow

**The system prompt lives in `system-prompt.md` and nowhere else.** It is
embedded into the workflow's two Set nodes (`Build Prompt`, `Build Regen
Prompt`) by the build script — never hand-edit the inline copy in the JSON.

```bash
# after editing system-prompt.md (or the node graph in the .json):
python3 scripts/build_workflow.py
```

`build_workflow.py` is idempotent and never writes a secret. It aborts if a
secret-shaped string survives in the output.

---

## Deploying

See `HANDOFF.md` §6 (step 4) and `docs/test-cases.md`. In short: create the
N8N credentials, add `TELEGRAM_BOT_TOKEN` to the N8N container env, disable
Phase 1A, import `workflows/phase-1b-telegram.json`, activate, register the
Telegram webhook, run T1–T6.
