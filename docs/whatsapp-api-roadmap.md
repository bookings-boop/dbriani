# WhatsApp API Roadmap

**Status (2026-05-20):** Phase 1B runs on **WAHA**. A migration to the official
**Meta WhatsApp Cloud API** (Phase 1C) is planned, ~2 weeks out, pending Meta
Business verification.

> The pricing and "BSUID" details below are from Zayn's research. Confirm them
> against Meta's official documentation at migration time — Meta changes both.

---

## Current — WAHA (Phase 1A / 1B)

WAHA drives a real WhatsApp account over the WhatsApp Web protocol.

**Why we stay on WAHA short-term:**
- It works and is deployed — Phase 1B is live.
- Zero migration cost right now; switching mid-stabilisation would reset testing.
- Fine for validating the agent itself — drafting, approval, conversation
  memory, proactive send.

**Its limitations:**
- **`@lid` addressing.** WhatsApp identifies many senders by an opaque
  "Linked ID" (`<digits>@lid`) instead of a phone number. This is **Meta's
  privacy feature, not a WAHA bug.** WAHA's `/lids/{lid}` endpoint resolves
  only ~70–80% of LIDs; the rest are permanently opaque. A LID-resolver +
  `lid_resolutions` cache was designed and then **rejected** — brittle, and
  made obsolete by the Cloud API move.
- **Ban risk.** WAHA automates WhatsApp Web, against WhatsApp's ToS. The
  account can be banned at any time. Approval-first drafting reduces spam-like
  behaviour but does not remove the risk.
- Not friendly to CRM/HubSpot integration.

## Target — Meta WhatsApp Cloud API (Phase 1C)

The official Meta API.

**Why we move:**
- **Real phone numbers.** Inbound messages carry the customer's actual phone —
  solves the `@lid` identification problem at the source. No resolver, no
  cache, no guessing.
- **No ban risk.** It is the sanctioned API — no ToS violation.
- **HubSpot-ready.** Real phone numbers + an official API make the deferred
  CRM-sync step straightforward.
- **Free at our volume** — see pricing below.

**The June 2026 "BSUID" change.** Meta is rolling out a Business-Scoped User ID
change (~June 2026) that also affects Cloud API identifiers — a partial privacy
measure. It is *less* complete than a raw phone number but still
**substantially better than `@lid`** for our purposes. (Verify specifics
against Meta's docs at migration time.)

**Pricing (UAE).** WhatsApp uses per-conversation pricing. For the UAE the first
**1,000 service conversations per month are free** — comfortably above our load
(~50 leads/week ≈ 200/month). Effectively free at current volume. (Verify
against current Meta pricing at migration time.)

## Migration plan

| When | What |
|------|------|
| This week | Zayn applies for Meta Business verification (parallel track — no Claude Code time) |
| On verification | Build Phase 1C on the Cloud API |
| ~2 weeks | Cut over WAHA → Cloud API |

Phase 1B's logic — drafting, Telegram approval, conversation memory, proactive
send — carries over. The migration swaps the **transport layer** (WAHA → Cloud
API webhooks + send endpoints), not the agent.

## Decisions captured

- LID resolver + `lid_resolutions` Postgres cache: **not built** — obsoleted by
  the Cloud API migration.
- `#3` reduced to a draft-header formatting cleanup (chatId / push name /
  history count) — delivered in Batch 1, no separate work.
