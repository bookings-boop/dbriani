# Feature B — extension / add-on + balance-owed tracking (design)

**Status:** SCOPED, not built. **FINANCIAL → daytime + supervised** (same fence as
payment match-buttons). Triggered by 2026-06-07: customer D (Von Dutch 40)
agreed a 4th hour; the CONFIRMED card kept showing the original 3hrs / AED 2249.1
paid, with no balance owed. Interim non-financial flag already shipped
(`_scope_change_hint`, commit `6ecf361`) — this doc is the real fix.

## Problem
The system tracks only `paid_amount` and a free-text `dates` string with the
duration baked in (e.g. "Jun 22, 4:30pm (3hrs)"). There is **no `total_due`, no
`balance_owed`, no structured duration/add-on, and no post-booking change
capture.** So an extension or add-on after CONFIRMED is invisible: the card
shows the original scope and can't show "X still to collect."

Also note: the extra hour CANNOT be auto-priced. Von Dutch 40 catalog = AED
1400/hr, but D paid 2249.1 for 3hrs (≈750/hr — a negotiated rate). The operator
sets the extension price; the system must never invent it.

## Goal
On a CONFIRMED booking, support post-booking changes and show:
`🛥 yacht · 🗓 date · Nhrs · 👥 party · 💰 AED <paid> paid · ⚠️ AED <balance> owed · total AED <total>`
plus a one-tap **top-up payment link** for the balance.

## Proposed design
1. **Schema (migration 010):** prefer a `booking_lines` table
   `(id, customer_id, kind ['yacht_hours'|'addon'|'extension'], label, qty,
   unit_price, amount, created_at, created_by)`. `total_due = SUM(amount)`;
   `balance_owed = total_due - paid`. Keeps history + is auditable. (Lighter
   alt: two columns `total_due`, on customer_facts — but loses itemization.)
2. **Capture (operator-driven, never auto-priced):** an operator command /
   button — e.g. `/extend <who> <hours> <price>` or `/addon <who> <label>
   <price>` — appends a booking_line. Mirror the payment match-button callback
   pattern (commit `6c6905e`, currently inert) for the keyboard/parse layer.
   Optionally: the analyzer *flags* a likely extension for the operator to
   confirm (it must not set the price).
3. **Render (review.py):** replace the single "💰 X paid" line with an itemized
   booked block + a red `⚠️ AED <balance> owed` when balance > 0. Reuse
   `_confirmed_detail`. Pure render helper, TDD'd.
4. **Top-up flow:** a "💳 Send top-up link" button generates a Nomod link for
   `balance_owed`; on payment (paylink/payment-confirmed path) the balance
   recomputes to 0. Reuse the existing paylink + payment-confirmed plumbing.
5. **Pricing rule:** operator sets every amount. Catalog rate is shown as a
   *suggestion* only ("Von Dutch 40 catalog 1400/hr — your call").

## Interactions / risks
- Financial accuracy: partial payments, refunds, VAT/Nomod fee (Hard Rule 17:
  5% VAT + 2% Nomod). Compute total as items + fees, operator confirms.
- Ties into payment match-buttons (B item in the deferred queue) — build them
  together (shared payment-match + link infra).
- Keep operator-in-the-loop end to end; never auto-charge or auto-price.
- TDD: pure `balance_owed` math + render helper; payment flow tested on a
  daytime supervised run, smoke-test on a real top-up before enabling.

## Build order (daytime)
1. migration 010 + balance math (pure, TDD).
2. render itemized + balance (review.py, TDD).
3. operator capture command/button (no auto-price).
4. top-up link + payment-confirmed recompute.
Pairs with the payment match-buttons; both daytime + supervised.
