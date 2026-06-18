# USDT-Only Invoice Flow — Design Spec (2026-06-18)

## Goal
Let the operator serve a crypto-paying customer entirely in USDT — issue a **proforma invoice that carries the USDT wallet address + USDT amount**, plus the canned USDT payment text — **without first sending a card payment link**. Before anything reaches the customer, the operator sees an **itemized invoice preview** they can verify and **edit via free-text feedback**.

## Context (current state, verified 2026-06-18)
- `/send-usdt` (`routes.py:handle_send_usdt`) is live but only reachable via the 🪙 button on the paylink **Link-Sent** confirmation — i.e. paylink-gated.
- Invoices: `invoice_hooks.issue_proforma` (at paylink-send, cohort A) + `issue_tax` (at Nomod payment). `invoice/invoice_gen.generate_invoice` computes `price ×1.05 (VAT) ×1.02 (2% card fee)`; AED only, **no crypto details**.
- Decided money model: **USDT/bank rail = ×1.05 flat (no card fee)**; card rail = ×1.05×1.02.
- Tax numbering: tax = sequential gap-free counter id=2 (starts **3333**); proforma = random-gap id=1 (from 3000). Separate.
- Callback router (linear): `Route Update Type → Redis Draft Lookup → Parse Callback → Answer Callback → Disarm Autosend → Draft Exists? → Route Action`. Buttons use callback `action:<draft_id>`. `Redis Draft Lookup` resolves the draft from `callback_data.split(':')[1]`; `Draft Exists?` gates on `draft_found`. Amount entry uses a per-draft `awaiting_amount` status in Redis (`Set Awaiting Amount → Process Text Reply → Route Text Action[out13] → Build Paylink From Reply`).

## Hard invariant (non-negotiable)
The wallet address is **always** `os.environ['USDT_TRC20_ADDRESS']` (code literal). The LLM edit touches **only line-item descriptions/prices** — never the address, never the USDT-amount math. The edit LLM's output schema forbids addresses; the existing `outbound_guard` still blocks any stray TRC20/IBAN token; the 1c history redaction keeps the address out of any history fed to the LLM.

## Requirements (approved)
1. Trigger: **🪙 USDT button on the main draft card** (every customer card).
2. Operator enters the **ex-VAT price** (AED).
3. Operator sees an **itemized proforma preview** (line items + amounts + USDT + wallet) — operator-only, nothing sent to the customer.
4. Operator can **edit via free-text feedback**; the LLM rewrites line items only; code recomputes amounts + keeps the wallet literal; re-preview. Loop until ✅.
5. **✅ Send** → customer receives the **proforma PDF** (USDT variant) **+** the canned **USDT text**.
6. **USDT amount = ceil( (Σ line-item subtotal × 1.05) / 3.6725 )**, whole number (VAT-inclusive, no card fee).
7. Proforma at send; **tax invoice (#3333 sequential) when the operator taps 💵 USDT received**.
8. The invoice PDF shows a **Crypto Payment block**: wallet (TRC20) + USDT amount + network + a payment reference (the proforma #).

## Components

### Bridge
- `compute_usdt_quote(items)` → `{subtotal, vat, total_aed, usdt_amount, address}`. `subtotal=Σ(qty×rate); vat=round(subtotal×0.05,2); total=subtotal+vat; usdt=ceil(total/3.6725); address=env`.
- `POST /usdt-quote` `{customer_id, draft_id, price | items}` → build line items (from `customer_facts` or provided) → compute → store in Redis keyed by `draft_id` (TTL ~1h) → post the **itemized preview card** to the admin chat with buttons `usdtissue:<draft_id>` / `usdtedit:<draft_id>` / `usdtcancel:<draft_id>`. Fail-open (always 200).
- `POST /usdt-edit` `{draft_id, feedback}` → load stored items → LLM (system: *"Edit the invoice line items per the operator's note. Output ONLY JSON `{items:[{name,rate}]}`. NEVER output a wallet address or payment string."*) with current items + feedback → validate → recompute → update Redis → re-post preview. On invalid output: keep prior items, tell operator.
- `POST /usdt-issue` `{draft_id}` → load quote → `issue_usdt_proforma()` (PDF) + send the canned USDT text via `/send-usdt` with **amount = the VAT-inclusive total** (so the text's USDT figure equals the invoice's = the quote's `usdt_amount`; `/send-usdt` already does `ceil(amount/3.6725)`) → customer.
- `invoice_hooks.issue_usdt_proforma(cid, name, items, usdt_amount, ...)` → rail=usdt (×1.05, no card fee), proforma # (random-gap id=1), PDF w/ crypto block, send. `issue_usdt_tax(cid, ...)` → on 💵 received → tax # (sequential id=2), PDF, send.
- `invoice/invoice_gen.generate_invoice(d)` honors `d['rail']`: for `'usdt'`, `compute()` omits card_fee (`total = subtotal+vat`); `_build_html` adds a **"Crypto Payment (USDT)"** block (wallet · USDT amount · TRC20 network · ref) and drops the card-fee totals row.

### n8n (workflow `azPIy9OcDwiPV5uY`)
- 🪙 USDT button on `Queue & Format` + `Auto Prep` card builders, callback `usdtquote:<draft_id>`.
- `Route Action` new output `usdtquote` → `Prep USDT` → `Set Awaiting Amount` (rail=usdt marker) → amount prompt.
- Price reply → `Route Text Action`(awaiting_amount) → `Build From Reply` → **branch rail=usdt** → `POST /usdt-quote`.
- Preview-card buttons `usdtissue:` / `usdtedit:` / `usdtcancel:<draft_id>` → new `Route Action` outputs → `/usdt-issue` / (Set-Awaiting-Edit + prompt → `/usdt-edit`) / cancel.
- 💵 `usdtreceived:<draft_id>` button on the proforma-sent confirmation → `/usdt-tax`.
- All callback additions wired the **safe way**: callbacks carry a **real draft_id** (passes `Draft Exists?`); **no `Parse Callback` edit**; applied to BOTH `workflow_entity` + `workflow_history`; snapshot + python-diff verify.

## Data flow
`tap 🪙 (usdtquote:<id>)` → `awaiting_amount(rail=usdt)` → `[type price]` → `/usdt-quote` (compute + store + preview to operator) → `[✏️ edit → /usdt-edit (LLM items → recompute → re-preview)]*` → `[✅ → /usdt-issue]` (proforma PDF + USDT text → customer) → … → `[💵 received → /usdt-tax]` (tax invoice → customer).

## Error handling / edge cases
- `USDT_TRC20_ADDRESS` unset → refuse to quote (operator alert).
- LLM edit returns invalid/no items → keep prior items; "couldn't apply, rephrase".
- Operator never confirms → quote expires (Redis TTL); nothing sent.
- price ≤ 0 → reject at prompt.
- Preview/edit posts go to the **admin chat only**; the customer send happens **only** on `/usdt-issue` (✅).

## Reversibility / safety
- Snapshot workflow (both tables) before n8n edits; per-edit python-diff verify; `.bak` for bridge files; staged reverts (mirrors the 2026-06-18 Phase-2 method). Nothing reaches a customer until the operator taps ✅ on the verified preview.

## Out of scope
- On-chain USDT auto-detection (no watcher; operator confirms via 💵 received).
- Bank/IBAN rail.
- Changes to the card/paylink flow.

## Open defaults (chosen, can revisit)
- 💵-received = a **button** on the proforma-sent confirmation (not a command).
- Proforma carries a **payment reference** (proforma #) for matching the incoming USDT.
