# Booking Report — Scoping Notes (parked)

**Status: NOT being built yet — behind higher-priority work.** This file records what a
read-only DB investigation found on **2026-06-15** about which booking data is actually
populated at `CONFIRMED` time, plus the planned design, so it isn't lost.

Source: read-only `SELECT` over all leads with `customer_facts.label='CONFIRMED'`
(there were **7** at the time). Nothing was changed. Related: see
[`SOURCE_OF_TRUTH.md`](./SOURCE_OF_TRUTH.md) §3 (booking state) and
[`HERMES_SYSTEM_MAP.md`](./HERMES_SYSTEM_MAP.md) §6 (the "inquiry-populated trap" fields).

---

## What the data actually looks like at CONFIRMED time

### 1. Reliable & final — trust these
- **`booked_yacht`** — the resolved single vessel (often more complete/correct than
  `yachts`). Populated 7/7.
- **`booking_date_abs`** — single ISO date, consistent with the conversation narrative.
  Populated 7/7.

These two are the only fields that read as **final agreed booking details**. The report
should treat them as authoritative.

### 2. "Best-known — operator must verify" — do NOT print as final
- **`booking_time`** — free text, inconsistent format, observed at least one direct
  contradiction (a row whose `booking_time` disagreed with the analyzer's own narrative)
  and one malformed value.
- **`party_size`** — soft: ranges ("3-8 guests") and explicit "to be confirmed".
- **`addons`** — incompletely captured (blank on rows where extras were clearly in play);
  absence ≠ "no extras".
- **`name`** — not booking-grade: handles, single letters, descriptive strings, mixed
  scripts; `name_locked = false` on all rows (none operator-verified).
- **`yachts`** and **`dates`** — the **inquiry "trap" fields**: they hold the *discussed*
  list, not the booking (e.g. multiple yachts floated, "May 25, May 28 (discussed)").
  Use `booked_yacht` / `booking_date_abs` instead.

Display these with a caveat and require operator confirmation; never render them as
finalized.

### 3. Special instructions have NO structured home today
- `customer_notes` and `customer_triggers` were **empty (0 rows)** for every CONFIRMED
  lead, and `customer_facts` has **no special-instructions column**.
- Special instructions therefore live **only in the WhatsApp transcript** (or implicitly
  inside `addons`).
- **Implication:** the report will need either **LLM extraction from the transcript** or a
  **new structured capture step** to surface special instructions. They cannot be read
  from a column today.

### 4. CONFIRMED mixes past and future trips — no "completed" state
- The `CONFIRMED` label covers both trips that already happened and trips still upcoming
  (observed split ~4 past / 3 future). There is no separate completed/done state.
- **Implication:** the report must **derive upcoming-vs-done itself** from
  `booking_date_abs` vs. today. Also note "paid + confirmed" ≠ "all details final" (a
  confirmed, paid lead can still have headcount/food under negotiation).

---

## Planned design (for when this is picked up)

- **Trigger:** on a lead reaching `label = CONFIRMED`.
- **Two views from the same booking:**
  - **Operator view** — full commercial detail incl. payment / balance.
  - **Captain view** — operational only, plus **balance-to-collect-on-arrival**; no
    commercial/marketing detail.
- **Gate:** the **operator verifies the details before the captain brief is sent**
  (the soft/"operator-verify" fields above are exactly why this human check is required).

---

*Captured read-only 2026-06-15. No schema or data was changed; this is a scoping note only.*
