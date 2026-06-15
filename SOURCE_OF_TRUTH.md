# Source of Truth — Data Ownership Law

This document establishes, as explicit law, **one owner per data type** for the Hermes
system. For each data category it states the single **writer of record** (the store —
and the mechanism — that is allowed to create/update that data), which stores may
**read** it, and **where the code violates that rule today**.

It is descriptive of **what the code actually does now**, derived from
[`HERMES_SYSTEM_MAP.md`](./HERMES_SYSTEM_MAP.md) §6 (Data stores) and every
"who-writes-what" signal across §2 (n8n workflows), §3 (bridge), §5 (scheduled jobs),
§7 (fragilities) and §8 (tenant seams). It proposes **no migration and fixes nothing** —
it writes the rule down and flags the conflicts.

**Last written: 2026-06-15 (read-only).**

---

## How to read this

- **Owner / writer of record** — the *one* store + mechanism that is supposed to be the
  authoritative creator/updater of this data. Everything else must treat it as derived.
- **May read** — stores/processes allowed to consume it without owning it.
- **Violation** — a place in the live code where some *other* store or an uncoordinated
  second writer also writes the same data, or where ownership is genuinely ambiguous.

**A structural fact that makes ownership mostly clean at the store level:** the n8n
workflow **never writes Postgres, Redis, or Supermemory directly** — it offloads *all*
database writes to the bridge (map §2, L60). So for live operational data, **Postgres is
the only store written, and the bridge is the only writer.** The conflicts below are
therefore of two kinds: (A) the *same data also lives in a second store* (HubSpot, the
offline audited index, catalog files), and (B) *multiple uncoordinated writers inside the
bridge* fight over the same Postgres field.

---

## Ownership ledger (summary)

| Data category | Owner store | Single writer of record | May read |
|---|---|---|---|
| Customer identity / contact info | **Postgres `customer_facts`** (keyed by WhatsApp cid) | Bridge: `_upsert_facts_sql`, `canonicalize_cid`/merge, `/name` lock | Drafter, `/review` (`v_lead_summary`), `behavioral_context()`, HubSpot-lookup (matches by phone) |
| Conversation history (all channels) | **Postgres `conversation_messages`** (durable, append-only) | Bridge: `record_message` / `POST /record-message` | Analyzer, drafter, backfill, `/review` |
| Booking state ("did they book", `booked_yacht`, dates, payment) | **Postgres `customer_facts.booked_yacht`** + payment rows | Bridge analyzer (sets `booked_yacht`); `payments.py` (writes `payment_received*`, promotes → `CONFIRMED`) | `/review`, drafter, hermes-analyst, daily digest |
| Lead state / labels / scores | **Postgres `customer_facts.label` + `customer_label_history` + `importance_*`** | Bridge: `apply_label_transition` (sole mutator); `/pipeline-analyze` (scores) | `/review`, daily digest, analyzer, sweeps |
| Company knowledge (fleet, specs, pricing, SOPs, policies) | **No single owner today** — *intended* owner = the n8n system-prompt (node 2) | n8n workflow JSON (hand-edited); plus `behavior_rules` for learned drafting rules | Drafter (live), `review.py` scorer, operators |
| Backups | **Google Drive** (offsite, encrypted) — sourced from Postgres | Box cron: `backup.sh` (local), `offsite-backup.sh` (Drive) | Disaster recovery only (private key on operator's Mac) |

---

## 1. Customer identity / contact info

*(name, phone, `@lid`↔phone mapping, party size, canonical/merged identity)*

- **Owner / writer of record:** **Postgres `customer_facts`**, keyed by WhatsApp cid
  (`<digits>@c.us` or `<hash>@lid`). Written by the bridge only:
  `_upsert_facts_sql` on every inbound message; `canonicalize_cid` + merge guards
  (`merged_into`, migration 006) decide which row is canonical; `/name` writes the
  name-lock (`name_locked`, migration 009). The `@lid`↔phone resolution table
  `lid_phone_map` (box-only, seeded from WAHA `/lids`) supports this but is a lookup, not
  a second identity record. (map §6 `customer_facts`/`lid_phone_map`; §3 L394–396)
- **May read:** the drafter, `/review` (via `v_lead_summary`), `behavioral_context()`,
  `handle_info`, and `hubspot_lookup.py` (which matches the customer in HubSpot **by
  phone** but does not write back). (map §6 L690)
- **Violations / second writers today:**
  - **HubSpot also stores customer identity.** Contacts (firstname/lastname/phone/
    customer_type) are written into HubSpot by a **separate project**
    (`~/projects/whatsapp-classifier/hubspot-migration/`), independent of the bridge.
    The live system treats HubSpot as read-only, but identity is duplicated and written in
    two places by two tools keyed differently (Postgres by cid, HubSpot by phone).
    (map §6 L690)
  - **The offline audited index** (`customer_audited_index.json`, whatsapp-classifier)
    holds a parallel per-customer identity/record used as a "source of truth" for
    revenue/booking analysis. (map §6 L705; §3 L396)
  - **`refresh_customer_facts_from_waha(...)` overwrites the name field** from WAHA
    push-names during `/review` auto-heal and intake-gap ingest — a second, automated
    writer of the identity `name` that can fight the operator's `/name` lock and the
    inbound `_upsert`. (map §2 L174; §7 #45)

---

## 2. Conversation history (across all channels)

*(the message transcript and per-customer conversation timings)*

- **Owner / writer of record:** **Postgres `conversation_messages`** — the *durable,
  append-only transcript*, idempotent via the partial-unique `(customer_id, msg_id)`
  (migration 010). The sole writer is the bridge: `record_message` /
  `POST /record-message` (the fail-safe writer). Migration 012 additionally lets
  `record_message` move/delete rows during identity re-point. Per-customer **timings**
  (last inbound/outbound, nudge counts) live in `conversation_state` (also bridge-written).
  (map §6 `conversation_messages`/`conversation_state`; §3 L251)
- **May read:** the analyzer (`build_analyzer_history`), the drafter, `backfill_lcm.py`,
  and `/review`. (map §6 L663)
- **Violations / ambiguity today:**
  - **WhatsApp/WAHA is a parallel source of the same history.** The true origin of the
    transcript is WhatsApp itself, read live through WAHA. The analyzer **merges** the
    durable store with a top-up read from WAHA, and `backfill_lcm.py` **writes**
    `conversation_messages` rows backfilled *from WAHA* (~15 leads/day). So conversation
    history has two stores — WAHA (live origin) and Postgres (durable copy) — and a
    job that copies one into the other. The `test_readpath_persist` guard exists precisely
    because this dual-source merge once shipped a corrupt store. (map §5 L612; §7 #5)
  - **`last_customer_message_at` is written by two unrelated paths:** real inbound
    (authoritative) and `/review`/analysis row-creation (which leaves it NULL), with
    `backfill_lcm.py` later patching it from WAHA — i.e. the timing field has a true owner
    (inbound) plus a backfill writer plastering over rows the pipeline created. (map §5 L612)

---

## 3. Booking state ("did they book", `booked_yacht`, dates, payment status)

- **Owner / writer of record:**
  - **"Did they book, and which yacht?" → Postgres `customer_facts.booked_yacht`**,
    set by the bridge analyzer *only* when the discussed-yacht list is unambiguous and not
    already set. This (or the audited index) is the **only** trustworthy "did they book"
    signal. (map §3 L396; §6 L705)
  - **Payment status → `payments.py` (bridge).** Nomod is the external origin; the bridge
    writes `payment_received` / `payment_received_unmatched` rows, dedups on
    `nomod_seen:<charge_id>`, and promotes matched real deposits to the `CONFIRMED`
    label via `apply_label_transition`. (map §2 L147; §3 L357)
- **May read:** `/review`, the drafter, the weekly hermes-analyst, the daily digest.
- **TRAP fields (must NOT answer "did they book"):** `booking_date_abs`, `booking_time`,
  `dates`, `yachts` are **inquiry-populated** — set during pricing chat, not at booking.
  HubSpot and these date fields **must not** be used to decide booking. (map §6 L705)
- **Violations / competing owners today:**
  - **Two declared sources of truth for "did they book":** Postgres `booked_yacht`
    **and** the offline `customer_audited_index.json`. They are both blessed by the map as
    trustworthy; nothing keeps them in sync. (map §6 L705; §3 L396)
  - **HubSpot deals** (booking/closed-won) are written into HubSpot by the separate
    `hubspot-migration` project — a third store of booking outcome, outside the bridge.
    (map §6 L690)
  - **No real booking lifecycle state.** `CONFIRMED` means both "trip upcoming" and
    "trip already happened"; the booking date is free text never pinned to a calendar, and
    **6+ code paths re-guess "has the trip happened?" and disagree** — ownership of the
    *post-booking* state is genuinely ambiguous. (map §7 #19)
  - **The trap date/yacht fields have multiple writers:** the analyzer and the `/review`
    auto-heal (`refresh_customer_facts_from_waha`) both write `dates`/`yachts`. (map §7 #45)

---

## 4. Lead state / labels / scores

*(pipeline label, label history, importance score / suggested action, operator overrides)*

- **Owner / writer of record:** **Postgres `customer_facts.label`** plus the audit log
  **`customer_label_history`** (migration 001). The **sole mutator is
  `apply_label_transition`** in the bridge; every label change funnels through it.
  Importance scores (`importance_score/reasoning/suggested_action/analysis_unreliable`)
  are owned by **`/pipeline-analyze`** (hourly). Operator manual overrides are recorded in
  **`label_corrections`** (their own table). (map §6 `customer_label_history`/
  `label_corrections`; §3 L381–383)
- **May read:** `/review` (scorer via `v_lead_summary`), the daily digest, the analyzer,
  the sweeps.
- **Violations / ambiguity today:** `apply_label_transition` is the single mechanism, but
  it is fired by **many independent, uncoordinated triggers**, several of which can move a
  lead *on their own schedule*:
  - per-message `handle_label_eval`/`compute_label` (map §3 L381–383);
  - `/pipeline-analyze` auto-`COLD` on an LLM "close"/score-0 verdict (map §2 L156);
  - the hourly sweep's cold-decay → `COLD` (`hourly_sweep:cold_decay`) (map §2 L165);
  - `payments.py` → `CONFIRMED` (map §2 L147);
  - operator overrides (→ `label_corrections`) (map §6 L660).
  These do not contradict the single-writer rule, but ownership of *when* a label changes
  is spread across five subsystems with no central policy — and at least one indirect path
  (`backfill_lcm.py` flipping a long-silent `COLD` lead to "owed") can pull a lead back
  into a live outbound sweep. (map §5 L631)

---

## 5. Company knowledge (fleet, specs, pricing, SOPs, policies)

- **Intended owner:** the **n8n system-prompt node** (node 2 of `phase-1b-telegram.json`)
  — the ~27–30 KB hand-authored prompt is what actually reaches the customer reply, so it
  is the *de facto* live knowledge base. Learned *drafting rules* are separately owned by
  **`behavior_rules`** (Postgres), fed by the edit-learning loop. (map §6 L686, L666;
  §8 L795)
- **May read:** the live drafter (n8n prompt), the `review.py` scorer (prices),
  operators (catalog docs).
- **Violation — THIS CATEGORY HAS NO SINGLE OWNER (the biggest ownership breach):**
  fleet + pricing data is **triplicated** with no authority among the copies:
  1. the n8n system prompt / `_CANON_YACHT_RATES` (live drafter) — `server.py` side;
  2. `review.py` `YACHT_RATE` (~line 424), ~60 yachts 799→20,000 AED/hr — a **second
     copy** used only for lead-ranking;
  3. `tools/catalog/catalog-block.canonical.md` — the human-authored fleet/catering/
     add-on price book.
  The map's own tenant-seams section flags this explicitly: "collapse price/fleet data
  into **one** source of truth (currently three: `server.py`, `review.py`,
  `catalog-...`)." Deposit floor (`CONFIRM_PROMOTION_MIN_AED`), drop-off pin
  (`waha.py:175` + `confirmation-and-location-templates.md`), and opener-price policy are
  similarly hardcoded in multiple places. SOPs/policies live in the prompt **and** the
  catalog md **and** docs. **Supermemory** was meant to be the knowledge layer but is
  **inert** (removed 2026-05-20; only PII residue remains). (map §8 L795–852; §6 L686)

---

## 6. Backups

- **Owner / writer of record:** the **box backup crons**. `backup.sh` (02:00) writes
  local on-disk `postgres_*.sql` + `config_*.tar.gz` (newest 3 kept) on the EC2 disk;
  `offsite-backup.sh` (03:30) builds a **config** bundle + a **data** bundle, GPG-encrypts
  both with an **asymmetric public key** (`dubriani-offsite-backup` — the box can encrypt
  but never decrypt), and uploads via `rclone` remote `dubriani-drive` to
  `dubriani-offsite-backups/`. **Google Drive is the authoritative offsite copy**; the
  local disk copy is a transient staging copy that the offsite job reuses. (map §6 L692–694)
- **Source data:** a Postgres `pg_dump` — backups are strictly **downstream** of
  Postgres (§§1–4); they own nothing original. The only customer data on Drive is whatever
  is inside the encrypted dump.
- **May read (restore):** disaster recovery only — the **private key lives on the
  operator's Mac** (+ password manager), so the box cannot read its own backups.
  (map §6 L694)
- **Violations / notes:** none of ownership — local and offsite are a chain, not two
  competing writers. Operational risk only: the local copy shares the same EC2 disk that
  **filled to 100% on 2026-05-29**. (map §6 L694)

---

## Conflicts to reconcile

Every place two stores currently both write the same data, or where ownership is
ambiguous. **No fix is proposed here** — these are flags.

1. **Customer identity is written to both Postgres and HubSpot, by two different tools.**
   Bridge owns `customer_facts` (by cid); the separate `hubspot-migration` project writes
   HubSpot contacts (by phone). No back-sync; two keyings. (map §6 L690)

2. **"Did they book" has two blessed sources of truth:** Postgres
   `customer_facts.booked_yacht` **and** the offline `customer_audited_index.json`. Nothing
   keeps them consistent. (map §6 L705; §3 L396)

3. **Booking outcome is written to a third store, HubSpot deals,** by the external
   migration tool — independent of the bridge's `booked_yacht`/payment rows. (map §6 L690)

4. **Conversation history exists in two stores with a copier between them:** WhatsApp/WAHA
   (live origin) and Postgres `conversation_messages` (durable). `backfill_lcm.py` writes
   PG rows from WAHA and the analyzer merges both at read time. (map §5 L612; §7 #5)

5. **The `name` field on `customer_facts` has three writers** that can disagree: inbound
   `_upsert_facts`, the operator `/name` lock, and `refresh_customer_facts_from_waha`
   (WAHA push-name) during `/review` auto-heal and intake-gap. (map §7 #45; §3 L394)

6. **A "read-only" reporting path mutates owned data.** `/review`'s auto-heal calls
   `refresh_customer_facts_from_waha(...)` and writes `customer_facts`
   (`name`/`yachts`/`dates`) and `last_review_seen_at` — a reporting job writing
   pipeline-owned fields. (map §2 L174; §7 #45)

7. **`last_customer_message_at` (timing) is owned by inbound but overwritten by backfill.**
   Rows created by `/review`/analysis leave it NULL; `backfill_lcm.py` later patches it
   from WAHA. Two writers, one true owner. (map §5 L612)

8. **Fleet + pricing knowledge is triplicated with no authority:** the n8n system prompt /
   `server.py` `_CANON_YACHT_RATES`, `review.py` `YACHT_RATE`, and
   `tools/catalog/catalog-block.canonical.md`. The deposit floor, drop-off pin, and opener
   policy are likewise hardcoded in multiple places. (map §8 L795–852)

9. **Post-booking lifecycle state is ambiguous.** `CONFIRMED` covers both "upcoming" and
   "already happened"; the booking date is unanchored free text; 6+ code paths re-derive
   "has the trip happened?" and disagree. No store owns the answer. (map §7 #19)

10. **Label changes are owned by one mechanism but driven by five uncoordinated triggers**
    (per-message eval, hourly analyze auto-COLD, hourly-sweep cold-decay, payments→CONFIRMED,
    operator override), and an indirect path (`backfill_lcm.py`) can re-touch a `COLD`
    lead. Single writer, no single policy for *when*. (map §2 L156, L165, L147; §5 L631)

11. **Supermemory is a defined-but-dead knowledge store** with leaked customer-PII residue
    still indexed — an ownership gap (knowledge has no live KB store) plus a standing data
    risk. (map §6 L686)

---

*Provenance: derived entirely from `HERMES_SYSTEM_MAP.md` (mapped 2026-06-15, read-only).
This document records the ownership rule and the conflicts as the code stands today; it
proposes no migration and changes no behavior.*
