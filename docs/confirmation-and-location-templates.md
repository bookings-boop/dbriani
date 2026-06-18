# Dubriani — Canonical Location Pins & Booking-Confirmation Template

**Source:** operator (bookings@dubriani.com), 2026-06-06. AUTHORITATIVE — the system must use these verbatim; never invent or substitute a different pin/format.
**Relates to:** Bug D (Antonio — no berth/pin ever sent; no confirmation-sent tracking) and the price/fact-fabrication theme (location pins are facts that must be grounded, not generated).

## Canonical location pins (ALWAYS use these — never a different link)
- **Drop-off / location pin** 📍 (use whenever sending "location" to a customer): `https://maps.app.goo.gl/1aT4Vtqwe71FKi3AA`
- **Coming-by-own-car / parking pin** 🅿️: `https://maps.app.goo.gl/kP8dHqhnrizYVxAw8`

> Rule: **whenever the bot sends a location to a customer, it must send the Drop-off pin above.** Dubai Harbour has no street name → the pin is mandatory.

## Booking-confirmation message — EXACT format (verbatim)
Placeholders to fill from booking data: `(DATE & STARTING TIME UNTILL END TIME)`, `(BERTH)`, `(Add gate number here if provided)`.

```
Thank-you booking is confirmed ✅ 

(DATE & STARTING TIME UNTILL END TIME)

Please try to be there 15-30 min before. - Dubai Harbour has alot of traffic currently

There is no streetname because its in harbour, please use the pin 📍 

Drop off Location📍:
https://maps.app.goo.gl/1aT4Vtqwe71FKi3AA

Coming by own Car Location 🅿️ : 
https://maps.app.goo.gl/kP8dHqhnrizYVxAw8

*please be informed there are roadworks going on and - parking is very limited*
When reached you can park in the outside parking lots 🅿️ 

*how to reach the yacht?*
for your convenience there are commercial buggys driving around the harbour yacht club which can drop you in front of the yacht for 10 AED /p

please ask the buggy operator or security for instructions and they will guide you.
➡️ *Berth parking:(BERTH) *
(Add gate number here if provided)

  Alternative - on arrival (walking)
please message us on arrival with a picture of where you are looking at - we will assist you 
➡️ *Berth parking(BERTH) *
(Add gate number here if provided)

We cant wait to sea you!
```

## CURRENT STATE (from code probe 2026-06-06) — why pins/confirmations are wrong today
- The booking confirmation is **NOT a fixed template** — it is **LLM-generated** from ~103 few-shot examples baked into the n8n `phase-1b-telegram.json` prompt (system-prompt.md has only a loose hint at lines 606-607: "[location pin + marina notes — adapt]" / "[Berth parking [code] — omit if none]"). No deterministic confirmation builder exists in routes.py/server.py.
- **No single source of truth for the pin** → at least 6 different maps links are scattered through the LIVE prompt's examples, so the bot emits whichever it pattern-matches:
  - `1aT4Vtqwe71FKi3AA` (×8) = the canonical drop-off pin (already partly present)
  - `5riJbF5tyj5aUwqr5` (×4, "Berth AD11"), `dBpMktKDNbkNZoj68`, `N2b2g73C1Fizi7866`, `amxRM3ktJZtMVFL66`, `wYimBfx6kwWZM4K88` = stray OLD pins
  - `DubrianDubaiHarbour` (×2) = a **fake/placeholder** link (not real)
  - the new canonical PARKING pin `kP8dHqhnrizYVxAw8` is **absent** (brand new).
- This is the SAME class as price fabrication (Bug A-S2 / Bug I): no grounded fact → inconsistent/wrong output. Antonio (Bug D) got "Dubai Harbour" text with NO pin at all.

## Implementation notes (for the plan — not yet built)
- This template is the payload for **Bug D-Fix3** (confirmation/berth-sent tracking): when an operator/Hermes sends the booking confirmation, send THIS, fill the date/time from the booking, fill `(BERTH)`/gate from operator-provided berth, log a new `autonomous_sends` kind `confirmation_sent`/`berth_sent`, and surface the ⚠️ "confirmation/berth not sent" flag on the /review card until it's logged.
- The location pin must be treated as a **grounded fact** (like catalog prices): the drafter must emit the canonical pin, never a generated/placeholder/different link → covered by the deterministic validator from Bug A-S2 / Bug I (extend the per-fact card to include the pins).
- Store the pins + template in the **single canonical config** (the same consolidation Bug A-S2 recommends) so drafter, confirmation flow, and any "send location" path all read one source.
- OPEN: berth/gate value source — does it come from the operator at confirm time, or from a booking field? (Owner decision.)
