# Email / Website-Form Lead Capture — Scope

**Status:** SCOPING ONLY. Nothing built, wired, or sent. Deprioritized behind pricing deploy + WhatsApp conversion work.
**Date:** 2026-06-15
**Basis:** read-only Gmail discovery on `bookings@dubriani.com`, 90-day window. No labels/drafts/sends/changes made. No HubSpot connection made.

---

## Core architectural finding

**HubSpot is the right *mirror*, the wrong place for *capture + dedup*.**

- The website forms are **WordPress** forms that email `bookings@` — they are NOT HubSpot forms, so HubSpot captures nothing natively today.
- HubSpot's native Gmail connector has no field parsing, no phone normalization, and no cross-channel dedup — and it would ingest the **entire inbox**.
- Per `SOURCE_OF_TRUTH.md`: the **bridge owns identity**; nothing writes a second identity store. Dedup belongs in the bridge (it already owns phone normalization, `lid_phone_map`, `do_not_feed`, the Layer-3 staff/crew block-list).

## Recommended path (when this is picked up)

```
WordPress `quotation-requests` table  →  bridge ingester (normalize + dedup)  →  HubSpot mirror
```

- **Read the WordPress `quotation-requests` table directly** — NOT email parsing (fragile), NOT HubSpot's native Gmail connector (no parse/dedup, whole-inbox ingest).
  - The WP admin already exposes each request at `…/wp-admin/admin.php?page=quotation-requests&action=view&id=N` — i.e. there is a real table behind it, the true source.
- Bridge does identity resolution; HubSpot receives the mirror (contact, + deal for bookings).
- **Capture-first; drafting/outreach later and gated** (see phasing).

## Privacy concern (locks the design)

The HubSpot **native Gmail connector would ingest the whole inbox** — payment receipts with amounts (Nomod), vessel registration / Dubai Harbour docs, tax.gov.ae, and heavy vendor spam — straight into the CRM. Any Gmail-side ingestion (if ever used) must **whitelist only lead senders** (`wordpress@dubriani.com` + classified human leads), never blanket-sync.

---

## The two lead streams (opposite difficulty)

| Stream | Source | Structure | Difficulty |
|---|---|---|---|
| **A. Website forms** | `wordpress@dubriani.com` | Rigid `key: value`, machine-generated, perfectly parseable | Easy / deterministic |
| **B. Direct human email** | individual senders | Free-text prose, rare, buried in vendor solicitations | Hard / needs a classifier |

**Stream A subject patterns:**
- `New Quotation Request: {Name}` — fields: Request ID, Name, Email, **Phone**, Country, Date, Time, Duration, Event. Dedicated sequential counter.
- `New Yacht Booking #{N}` — fields: Yacht, Date, Time, Duration, Event, Addons (JSON array), Customer, Email, **Phone**, Contact method. Separate sequential counter.
- `Yacht sales request (Layout): {name}` — separate low-volume form (yacht *sales*, not charter); contains test/spam entries.

**Stream B signal (real lead vs noise):** no single field decides it — combination of (individual human sender) + (asks about chartering: date/guests/occasion/price/availability) + (addressed as a customer). Noise = `noreply@`/`notifications@`, vendor pitches ("feature your boats", "AI booking system", SEO/review reports), transactional (payments), ops (marina/tax). This is the only place AI classification is actually required.

## Volume (measured, 90-day window)

| Stream | Rate |
|---|---|
| Quote form (`New Quotation Request`) | ~1.5/day → **~10–11/week** (IDs #130→#182 over May 12–Jun 15) |
| Booking widget (`New Yacht Booking`) | ~39 in 90d → **~3/week** |
| Direct human charter leads | **~1–3/week** (rare; e.g. Charlotte/Kymono 30-guest cocktail cruise) |
| **Total email/form leads** | **≈ 15/week** |

For comparison the WhatsApp channel is the dominant funnel (analyst week showed ~8 bookings via WhatsApp vs ~3 via the website widget). Exact WhatsApp weekly inbound was NOT measured here (would need a read-only bridge query). → Email is a **minority channel**; capture is **lower priority than conversion**.

**Answer-speed signal:** the inbox has **no triage** — only 3 user labels (`Notes`, `✔️`, `✔️✔️`), none for leads. Form emails are `noreply`, so any answering happens out-of-band in WhatsApp (not measurable from email). One starred high-value direct lead (Charlotte, Jun 4) had no visible email reply 11 days later — email is treated as a notification dump, not a worked channel.

## Cross-channel dedup approach (reuse the bridge)

1. **Primary key = normalized phone (E.164)** via bridge normalization + `lid_phone_map`. Form leads almost always carry a phone.
2. **Secondary = email** (for phone-less direct leads; HubSpot is email+phone keyed).
3. Resolution order: phone → email → else net-new. **Never match on name alone** (identity guardrail; "two Mos" collision).
4. **Flag ambiguous collisions for human review — never auto-merge silently.**
5. Gate any outreach through `do_not_feed_phones` + Layer-3 staff/crew block-list.
6. Expected split: `+971` form leads merge heavily (also WhatsApp); overseas (`+91`/`+44`/`+27`) mostly net-new on email. **Exact merge/new split must be measured by a Phase-0 dry-run before any write.**

## Capture-first / drafting-later phasing

| Phase | Scope | Risk |
|---|---|---|
| **0 — Precondition** | Confirm whether WP forms already feed the system (avoid double-capture); pick source (WP table vs email); dry-run dedup to measure merge/new split. Read-only. | none |
| **1 — Stream A capture** | Parse 3 form types → normalize phone → dedup → upsert HubSpot contact (+deal for bookings, with verification guardrail). No auto-reply. | low |
| **2 — Dedup hardening** | Collision review; intra-stream dedup (repeat submissions); merge report before any contact. | low |
| **3 — Stream B classifier** | Lead-vs-vendor classifier; capture-first + operator alert. | medium |
| **4 — Drafting (gated)** | Reuse approval-first WhatsApp drafter discipline. Never auto-send. | high — defer |

## Data-quality flags (handle at parse time)

- **Malformed phones:** stray leading zeros / wrong digit counts (e.g. `+9717985151993`, `+97197156707265`, `+270833078781`, `+9108557000043`). `Country` field is a reliable normalization hint.
- **Duplicate submissions:** same person submits 2–4× (Emad #167/#168 two phones 2 min apart; "Alexander Ward" #136/#143/#151/#152). Needs intra-stream dedup, not just cross-channel.
- **Test/spam entries** in the yacht-sales form (`riddle test`, `456456@fdfd.ggg`).
- **Booking ≠ verified paid:** `New Yacht Booking` is a widget submission, several are "Contact method: Call" (call-back requests). Per booking-verification guardrail, reconcile against `booked_yacht`/audited index before treating as revenue.

## Open decisions (for operator)

1. Capture from **WordPress DB/table** (recommended) vs email parsing?
2. Do the website forms **already** flow into the WhatsApp/HubSpot system, or only land in email? (#1 thing to confirm before Phase 1 — avoid double-capture.)
3. Bookings → create HubSpot **deals**, or contacts-only until payment-verified?

## What was NOT done

No code, no wiring, no HubSpot connection, no Gmail labels/drafts/sends, no WhatsApp/bridge query. Pure read-only Gmail discovery + this document.
