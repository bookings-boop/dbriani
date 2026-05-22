# Nomod Payment Links — Minimal Build Plan

> **STATUS: APPROVED — build ON HOLD.** Operator approved this plan and both
> additions below (`payment_summary` field + `PAYMENTS_ENABLED` kill-switch).
> Build is held until the operator signals "begin Nomod build"; bug fixes per
> `docs/bug-diagnosis-2026-05-22.md` take priority first.
> Scoped-down version of `docs/nomod-integration-plan.md` for a single
> 2–3 hour build session. Approval-gated; no autopilot; no polling.

**Goal:** When a booking is confirmed, Maria drafts a WhatsApp message containing a real Nomod payment link. The operator reviews and approves it on the existing Telegram card; approval sends it via the existing WhatsApp path. That's it.

**Time budget:** 2–3 h for the build. **If the estimate goes over 3 h, I stop and report** (per your instruction). The manual paid test (item 6) is operator-driven and runs after the build.

---

## Scope

### Building today (6 items)
1. **Bridge `POST /payment-link`** — creates a Nomod link, returns the URL.
2. **Hermes detection** — `should_send_payment` + `payment_amount` (+ one short `payment_summary` line, see note) in the draft JSON.
3. **Workflow branch** — on `should_send_payment`, call `/payment-link`, build the WhatsApp message with the link.
4. **Approval card header** — payment draft shows a header with amount + customer + booking.
5. **Approve → send** — operator approves; the message goes out via the **existing** WhatsApp send path (no new send code).
6. **Manual paid test** — a 100 AED link, paid with the operator's own card, refunded via the Nomod dashboard.

### Deferred (NOT today — per operator decisions)
`payments` database table · payment-status polling / poll workflow · duplicate-link prevention · min/max amount caps · link expiry (use Nomod default) · "Edit amount" card button · refund command (dashboard only) · audit DB (logs only).

---

## Operator decisions applied
- **Link expiry:** none — omit `expiry_date`, Nomod default.
- **Follow-up / urgency wording:** Hermes decides per-message from conversation patterns — **nothing hardcoded**. The urgency framing lives in Hermes's normal reply text, not a fixed template line.
- **Max amount cap:** none. (Bridge still rejects a non-positive amount — basic input validation, not a business cap.)
- **Refunds:** human-only, Nomod dashboard.
- **Polling:** deferred — operator checks the Nomod dashboard manually for paid/unpaid.
- **Audit:** minimal — bridge `log()` lines only, no DB.
- **Duplicate prevention:** deferred — operator catches it at the approval gate.
- **Min/max sanity:** deferred — operator verifies the amount before approving.

> **Detection fields — APPROVED BY OPERATOR (three fields total):** beyond the
> stated `should_send_payment` + `payment_amount`, Hermes also emits a short
> **`payment_summary`** string (e.g. `"Sunseeker Satoshi 70 · Sat 14 Dec ·
> 6 guests"`) — the card header and the WhatsApp message both need a booking
> description. Operator approved this addition.

---

## Component detail

### 1. Bridge `POST /payment-link`  (`hermes-bridge/server.py`)
- New env in `~/hermes-bridge/.env`: `NOMOD_API_KEY` (already set), `NOMOD_API_BASE=https://api.nomod.com/v1`, `PAYMENTS_ENABLED=true` (one-line kill-switch — flip to `false` to instantly disable — **APPROVED by operator** as the production-money safety switch).
- Helper `nomod_create_link(amount, summary, customer_name)` — `urllib` POST to `{NOMOD_API_BASE}/links`, header `X-API-KEY`, 10 s timeout. Body: `currency:"AED"` (hard-coded — never from the LLM), `items:[{name:summary, amount:"<n>.00", quantity:1}]`, `title:"Dubriani Yachts — <name>"`, `note:summary`. No `expiry_date`.
- Endpoint `_payment_link(payload)` — reads `{amount, payment_summary, customer_name}`; if `PAYMENTS_ENABLED` false or `amount` not a positive number → `{ok:false, error:...}`; else create the link → `{ok:true, link_url, link_id}`. **Fail-safe — always returns 200**, never raises to n8n.
- One `log()` line per call: timestamp, customer, amount, link_id / error.
- Register `/payment-link` in the `do_POST` allowed-paths tuple + dispatch. Token-gated by `X-Bridge-Token` like every existing endpoint.

### 2. Hermes detection
- **`Build Prompt` node** (embedded `systemPrompt`): append an instruction — emit `should_send_payment:true` + `payment_amount` (the confirmed total in AED) + `payment_summary` **only when** yacht, date+duration, and price are all confirmed **and** the customer shows explicit booking intent ("book it" / "let's do it" / equivalent). When unsure → `false`, and prefer to *ask* "shall I send the payment link?". The lead-in wording (and any urgency) is Maria's normal reply — written naturally, not from a template. Mirror the same line into `system-prompt.md`.
- **`Parse Response` node**: extract the three fields with safe defaults (`should_send_payment`→`false`, `payment_amount`→`0`, `payment_summary`→`""`). A missing/bad block must never break parsing of the normal reply.

### 3 & 4. Workflow branch + card header  (`azPIy9OcDwiPV5uY`)
New nodes, deployed via a `scripts/build_*.py` script + `n8n_deploy.py` safe-deploy:
- **`Check Payment Trigger`** (IF) — after `Customer Facts`. `should_send_payment === true` → payment branch; else → existing `Queue & Format` path, unchanged.
- **`Generate Payment Link`** (httpRequest) — `POST {bridge}/payment-link`, `Hermes Bridge` credential, `onError: continueRegularOutput` (a failure must not kill the run).
- **`Build Payment Message`** (code) — sets `draft_text` = Maria's reply + an appended block:
  ```

  🛥️ <yacht>   📅 <date>   👥 <guests>
  💳 total: AED <amount>

  tap to complete your booking: <link>
  ```
  and sets `payment_header` on the item:
  ```
  💳 PAYMENT LINK — AED <amount>
  👤 <customer> · <payment_summary>
  ──────────────
  ```
  If `/payment-link` returned `ok:false`, instead append an operator note `⚠️ payment link not created: <error>` and no link — the draft still posts.
- **`Queue & Format`** — minimal edit (2–3 lines): if the item carries `payment_header`, prepend it to the card text. Booking facts (`<yacht>/<date>/<guests>`) reuse the existing `Customer Facts` data already on the run.

### 5. Approve → send
No new code. A payment draft is a normal draft: it registers in the pending queue via `Queue & Format`, posts through `Send Draft to Telegram`, and the operator's existing **Send** button routes it through `Send One to Customer` (WAHA). **Skip** discards it — note: a Skipped payment draft leaves an unused, unpaid link in Nomod (harmless, no money moved; operator can archive it in the dashboard). Correcting a wrong amount = Skip + re-trigger.

### 6. Manual paid test
After deploy: drive a confirmed-booking test conversation → confirm a real 100 AED link is created, the card renders with the header → operator approves → the link arrives on WhatsApp → operator **pays it with their own card** → confirms in the Nomod dashboard → **refunds it via the dashboard**. Record the link behaviour. (No automated paid-status check — polling is deferred.)

---

## Deploy sequence (standard pattern per step: backup → dry-run → deploy → verify)
1. Bridge: add env vars, deploy `server.py`, `systemctl --user restart hermes-bridge`, verify `/health` + a prereq `GET /v1/currencies` (read-only, lists AED).
2. Bridge: smoke-test `/payment-link` with a 100 AED call → confirm a real link URL comes back.
3. `Build Prompt` + `Parse Response` + `system-prompt.md` — deploy via build script, dry-run first.
4. Workflow nodes — deploy via build script + `n8n_deploy.py`, dry-run first; verify node count and wiring.
5. End-to-end (non-paid) verify: test message → link generated → card header renders.
6. Operator-driven manual paid test (item 6).

Each of steps 1–4 is committed separately to `overnight-build`.

---

## Risks (minimal build)
- **Production money** — every link is real. Mitigation: 100% approval-gated; `PAYMENTS_ENABLED` kill-switch; currency hard-coded AED; operator verifies the amount on the card.
- **Wrong amount from the LLM** — operator card shows the amount prominently; operator is the check. No cap, per decision.
- **`Queue & Format` is sensitive** (already modified twice) — the edit is 2–3 lines, dry-run verified; if it looks risky at build time I stop and report.
- **SSH to EC2 has been flaky** all session — could eat into the 3 h budget. If so, I stop and report.
- **Orphaned links** — a Skipped/wrong draft leaves an unused link in Nomod (no money, harmless).

## Rollback
- `PAYMENTS_ENABLED=false` → feature instantly inert, no redeploy.
- Bridge: revert `server.py`, restart.
- Workflow: restore `workflows/phase-1b-telegram.json` via `scripts/rollback_workflow.py`. `Check Payment Trigger` defaults false → payment branch simply never runs.

## Estimate
Bridge `/payment-link` ~45 m · Hermes detection ~40 m · workflow nodes + card ~50 m · deploy/verify ~30 m → **≈ 2.5–3 h build**. Manual paid test is operator-driven, additional. **Hard stop + report at 3 h** if not deploy-verified.
