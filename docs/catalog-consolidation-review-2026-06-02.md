# Dubriani Catalog — Consolidation Review (2026-06-02) — B2C ONLY

> **STATUS: REVIEW — NOT source of truth. Nothing merged into the 5 catalog copies yet.**
> Existing: live from `~/hermes-bridge/system-prompt.md` (= 4 n8n prompt nodes, byte-identical).
> Watersports: live from dubriani.com (headless-browser rendered prices). Eva 60: operator.
>
> **Operator decisions (2026-06-02):** website pricing is authoritative · **forget ALL B2B —
> customer/B2C only** · website "was→now" discounts valid · Bliss 55 anchor 1,400→1,100 valid ·
> Eva 60 → Essential tier · jetski verdicts approved (add Yamaha Standard, drop 1,800cc spec).

**Legend:** ✅ EXISTING(B2C) · 🆕 NEW · ❌ REMOVED (B2B) · ⚠️ note

---

## ❌ REMOVED — all B2B pricing & mechanics (per "forget the complete B2B part")
Pulled from the consolidated catalog (and to be removed from all 5 copies at sync):
1. **Satoshi §7.1:** "B2B partner price AED 1,000 + VAT/hr" and "24h B2B AED 12,000 + VAT".
2. **§1 "B2B partner pricing (~50% off retail)"** whole section (Satoshi 1,500 B2B, Eclipse 2,000 B2B, halved add-ons) **and the "request license before quoting B2B" rule** + the `notes_for_zayn` B2B-license example.
3. **B2B add-on column** (e-foil 500 / shisha 250 / water slide 500 B2B).

⚠️ **Consequence (intended, flagging for awareness):** an agency/partner asking for "B2B pricing" will now be quoted **B2C/website** rates; the bot no longer has a partner-rate path or license gate.

---

## PART A — YACHTS (✅ EXISTING, B2C) — prices unchanged, discounts valid
52 yachts across Essential (14) / Premium (20) / VIP (18) + Satoshi 70. **All website "was→now"
discounts are valid current B2C prices**, e.g. Sunseeker 88 **2,800** (was 4,000) · Eclipse 90 **4,000** (was 5,000) ·
Elise 50 **900** (was 1,100) · Benetti 120 **5,500** (was 6,000) · Pershing 82 **5,500** (was 7,000) ·
Sunseeker 131 / Royalty 136 / Thunder **15,000** (was 18,000) · Dolce Vita **7,500** (was 8,000) · etc.
**Bliss 55 anchor: ~AED 1,400~ → AED 1,100** (standing). Catering/beverages/shisha/occasions/multi-day unchanged.

**Satoshi 70 (§7.1) — now B2C-only:** standard **3,000/hr** · morning floor **1,500/hr** · 24h from **12,000** ·
add-ons (B2C): **E-foil → 1,500/hr (see ⚠️ below), Electronic Shisha 500, Water slide (min 4hr) 1,000, 8-hr booking → free water slide.**

🆕 **Eva 60 → Essential tier:** `| Eva 60 | 12 | 1,500 | eva-60 |` (not yet online).

---

## PART B — WATERSPORTS (🆕, website B2C) — COMPLETE
| Activity | Variant | AED |
|---|---|---:|
| Jetski — Normal | 1 hour | **600** |
| Jetski — Supercharged GP | 1 hour | **1,000** |
| Jetcar | 20 min (≤2) / 30 min (≤2) / 1 hr (≤4) | **790 / 1,200 / 1,690** |
| eFoil | 1 hour / 2 hours | **1,500 / 2,500** |
| Seabob F5 | 1 hour | **1,200** (delivered to your yacht) |
| Seabob F9S | 1 hour | **2,000** (exclusively on Sunseeker Satoshi) |
| Flyfish | 1 hour / 2 hours | **1,200 / 2,200** |
| Flyboard | 20 min / 1 hour | **1,000 / 1,800** |
| Banana Ride | 30 min / 1 hour | **800 / 1,200** |
| Donut Ride | 1 hour / "1 hour" (2nd) | **800 / 1,200** (as published) |
| Wakeboard | 1 hour | **1,200** |
| Deep Sea Fishing | 4 hours | **2,500** (boat+crew+gear, private) |
| Self-Driving Boat | 1 hour | **Enquire** (custom quote) |

- Jetski models: **Yamaha (Standard), Yamaha GP Supercharged, Sea-Doo** (Sea-Doo priced as Normal/Supercharged).
- ⚠️ **eFoil reconciliation:** the Satoshi add-on e-foil was listed at B2C 1,000/hr; per "website is correct," **eFoil = 1,500/hr** now applies. Confirm this replaces the Satoshi add-on price too (vs a different bundled rate while chartering Satoshi).

---

## Status — ✅ DEPLOYED 2026-06-02
B2C list approved + applied to **all 5 copies** (bridge `system-prompt.md` + 4 n8n prompt nodes) via the Option-B generator.
**Drift-check PASS — all 5 catalog blocks byte-identical (sha `ef8dbdc175b7`).** eFoil reconciled to 1,500 (website). Line-345 B2B-partner ops note kept (not pricing).
Backups: box `~/phase-1b-sync-backup-20260602-133507.json` + `~/hermes-bridge/system-prompt.md.bak-presync-20260602-133507`.
**Pending: operator sends a test WhatsApp message** to confirm a live draft quotes a watersport / Eva 60 correctly.
