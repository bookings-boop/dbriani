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
| 1B | Telegram approval bot — Send / Edit / Regen / Skip | **Live in N8N** (T1–T6 passed) |
| Hermes | Drafting brain + refinement loop, rule capture, trigger detection, health scoring, autonomous mode, daily digest | **Live** — 51-node workflow |

The workflow now drafts through the **Hermes Agent** (via the `hermes-bridge`
service on the EC2 box) and the approval surface is the **"Dubriani Hermes"**
Telegram bot. See `docs/hermes-architecture.md`, `docs/decisions.md`,
`docs/test-cases-hermes.md`, and `docs/autonomous-mode.md`.

Original Phase 1B execution scope was **steps 0–4 only** (see `HANDOFF.md` §0) — **all complete.**
Everything beyond — HubSpot, Postgres, Gmail, weekly distillation, Redis, the
full 7-button UI — is deferred until 0–4 are validated on real traffic for ~1 week.

| # | Step | State |
|---|------|-------|
| 0 | Rotate Anthropic API key | Done |
| 1 | Git repo + `.env` externalization | Done |
| 2 | Verify Supermemory — connector mis-scoped; MVP ships memory-less | Done |
| 3 | Supermemory node removed from workflow — see `docs/supermemory-status.md` | Done |
| 4 | Deploy Phase 1B (Telegram approval) — T1–T6 passed | Done |

### Deferred (post-MVP, after the ~1-week validation)

- **Risk fixes R5, R9, R10** — double-send idempotency guard; regen failure
  rendering + `max_tokens` bump; static-data concurrency (Redis migration).
- **Supermemory re-enable** (Phase 2) — see `docs/supermemory-status.md`,
  including the customer-PII file purge.
- **Phase 1C — Meta WhatsApp Cloud API migration** (~2 weeks) — replaces WAHA,
  solves `@lid` permanently. See `docs/whatsapp-api-roadmap.md`.
- **Steps 5+** — HubSpot, Postgres, Gmail, weekly distillation, 7-button UI.

### Post-deploy improvement batches

- **Batch 1 (done, commit `79bb62d`)** — #1 conversation memory, #2 one-message
  default, #4 proactive send; #3 reduced to a header cleanup (delivered with #1).

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
| Telegram bot token | `{{ $env.TELEGRAM_BOT_TOKEN }}` — N8N container env var |

(Supermemory is removed from the MVP workflow — see `docs/supermemory-status.md`.)

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
