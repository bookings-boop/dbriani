# Cowork Targeted Integration Plan — 4 specific gaps from the 2026-05-23 inventory

> **Status:** PLAN ONLY. No system prompt changes. No deployment. No restart.
> **Scope:** Fill 4 specific gaps identified by `docs/hermes-current-state-inventory.md`. NOT a full overhaul.
> **Companions:** `docs/cowork-prompt-additions-proposed.md` (the actual proposed text), `docs/cowork-integration-open-questions.md` (operator decisions needed before build).
> **Budget:** Total additions ≤ 200 net lines. Current prompt is 584 → target ≤ 800.

---

## Current prompt state (already-shipped, not to be touched)

Per the inventory (`docs/hermes-current-state-inventory.md`) and yesterday's
calibration commit `b0d30da`, the current prompt covers:

- §0 role + draft-approval framing, §0.5 conversation context block, strict JSON output format
- §1 verified rules incl. META-RULES A/B, VIP fast-track, **Loss-reason recovery (timing ladder)**, pricing rules, payment rules, repeat-customer outreach, B2B, multi-day
- §2 Hard Rules 1–10 (Rule 5 = phone-call ban [yesterday], Rule 10 = name-overuse cap [yesterday])
- §3 Identity + brand differentiators
- §4 Voice & Tone (includes 2-use name limit, mirror tone, light emoji)
- §5 Mandatory qualification (4 fields, friendly)
- §6 Recommendation Rule (incl. yesterday's "you CANNOT send files" prelude)
- §7 Yacht catalog (Satoshi 70 / Essential / Premium / VIP / Doha / Retired)
- §8 Catering (Roberto's, Russian fine dining, beverages)
- §9 Shisha, §10 Special Occasions, §11 Multi-day, §12 Chauffeur, §13 Onboard hospitality
- §14 Negotiation Playbook (Chris Voss, trust signals, sequence)
- §15 Closing & Confirmation Flow (5 steps)
- §16 Hard Stops, §17 What you DON'T do, §18 Mindset, §19 Payment link signal

**Yesterday's `b0d30da` strengthened:** §2 Length rules (caps + 3 examples), §2 Hard Rule 5 (phone-call ban + no-exceptions), §2 Hard Rule 10 (name-overuse cap), §6 file-hallucination prelude. **OFF-LIMITS for this plan.**

---

## Source documents used for THIS plan

Exactly the 6 cited per gap (not all 20 Cowork docs):

| Doc | Used for | Highlights pulled |
|---|---|---|
| `Dubriani Country Quick-Reference Card.md` | Gap 1 | 12 country profiles (5 lines each), payment-method table, escalation flags |
| `Dubriani Per-Country Message Templates.md` | Gap 1 | India "perk in pocket" close, Russia USDT pitch, GCC formal opener, Spain perk-in-pocket, China visual-first |
| `Dubriani Conversion Rules — What Works & What Doesn't.md` | Gap 2 | §3.3 — `Have you given up?` timing table (verified +3.8pp lift in 30-min-to-2-hr window) |
| `Dubriani WhatsApp Cheat Sheet.md` | Gap 2 | Ghost Recovery timing table (5 rows, verified phrasings) |
| `Dubriani WhatsApp - Example Library.md` | Gap 2, 3 | §12 ghost-recovery escalation; §13 walk-away; §14 lost-deal feedback ask |
| `Dubriani Winning Phrases Library.md` | Gap 3 | "Another strong call" urgency [121×], value-add "while we're not able to negotiate" [pattern], exit feedback ask [83×], post-confirmation kit [10×], energy-phrases vocab |

---

## Gap 1 — Per-country opener templates

### What's missing

Current §1 META-RULE B lists **5 segments**: Russian (`)` mirroring), Indian/Pakistani ("Mr [Name]"), GCC ("Mr [Name]" + Arabic phrases), USA/AU (Sweet/Got it), UK (slight formality + 24-hr follow-up). Each gets 1 line. **No payment-preference hints, no per-country negotiation move, no opener cadence, no Spain, no Germany/France/Italy, no China.**

### What to add

Replace the 5 single-line bullets in META-RULE B with a **10-row reference table** distilled from `Country Quick-Reference Card.md`. One row per segment, 4 columns: Style · Move · Pay · Perk-if-pushback.

**Country list locked from operator's spec, mapped to Cowork data:**

| Operator request | Cowork doc data |
|---|---|
| UAE | N=6,073 · 0.92% win · casual Arabic-English mix · card/cash/50-50 |
| India | N=948 · 0.95% win · haggle expected, perk in pocket, never drop hourly rate · card |
| USA/Canada/Australia | N=854+123 · 5.85%+6.50% win · direct, decisive, fast close · card, no USDT push |
| UK | N=1,028 · 2.82% win · patient (12.2% "thinking"), detail-oriented · cash on arrival real option |
| Russia/Kazakhstan | N=454 · 2.86% win · 3.8 questions/chat, verify-and-decide · USDT preferred, Chef Artem pitch |
| GCC (Saudi/Kuwait/Qatar/Bahrain/Oman) | N=575 · 1.4-7.3% win · telegraphic, formal "Mr [Name]" · cash/bank/multi-day |
| Spain | Long messages, 17.4% objection rate (highest in dataset) · perk in pocket like India |
| Germany/France/Italy | Long detailed messages · short replies redirect, send brochures/videos · multi-day cruise pitch |
| China | N=38 (small) · 13.2% win · photo-heavy preference · Mandarin host pitch |

### Where it lands

**Two options. Recommend option A (lighter).**

- **Option A (recommended, ~28 lines):** Replace current META-RULE B body (lines 47–51 + paragraph at 53) with a 10-row markdown table + a 3-line preamble. Net additions: ~22 lines (table replaces 5 bullets that get removed).
- Option B (~40 lines): Keep META-RULE B as-is (5 short bullets), add a NEW §4.5 "Country adaptation" between §4 and §5 with full table + 1-line guidance per country. Net additions: ~35 lines.

**Default plan: Option A.** META-RULE B is already the cultural-signal section; expanding it in place keeps the prompt's existing structure.

### Line landing (Option A)

Replace lines 47–53 (META-RULE B's 5 bullets + the conversion-stats paragraph that follows it) with the new table + preamble. The conversion-stats paragraph (about VIP 22.4% etc.) stays — it gets moved to come AFTER the new table.

---

## Gap 2 — Verified ghost-recovery timing windows

### What's missing

Current §1.Loss-reason recovery (lines 74–98) already has a timing ladder:

```
- 30 min silent: "Hi! Any thoughts on this one? I can hold the slot for 2 hours."
- 2 hr silent: ONLY in the 30-min-to-2-hr window — "Have you given up on booking?"
- 24 hr silent: "Just checking in if you have any update for us, are you still considering or has the plan changed?"
- 7+ days: dead, do not message.
```

Gaps vs Cowork verified data (`Conversion Rules §3.3` + `Cheat Sheet Ghost Recovery`):
- No explicit "**<30 min: DON'T send**" directive (current prompt's 30-min line is when to send, not when to wait)
- The 30-min-to-2-hr phrasing is buried in the 2-hr bullet, easy to misread
- The **2-24hr middle window** has no alternatives ("are you still there?" / "may I know the hourly rate you are considering?") — current jumps from 30-min directly to 24-hr
- No mention of the 3–7 day "last shot" window where the same phrase can be retried once
- Wording inconsistencies between current prompt and the verified-phrase source (the canonical "may I know the hourly rate you are considering?" is missing)

### What to add

Replace the existing 4-bullet timing ladder (lines 78–82) with a **5-row markdown table** matching the verified Cheat Sheet structure. Net change: ~9 lines added (5-row table vs current 4-bullet block).

### Where it lands

**§1.Loss-reason recovery** (lines 74–98). Replace ONLY lines 78–82 (the timing ladder bullets); leave the "If customer says price is too high" pivot block and the "wants 2 hours" block and the "asks for catalog" block all unchanged.

---

## Gap 3 — Verified high-converting phrases library (light touch)

### What's missing

Current prompt has rules but few specific phrasings the LLM can reach for. From the Winning Phrases Library (each `[N×]` is uses in WINNING chats only):

| Phrase | Already in prompt? | Where |
|---|---|---|
| Walk-away "we take pride in maintaining a standard of excellence" | ✅ §1 Pricing rules, rule 8 (line 153) | — |
| "Another strong call" urgency close [121×] | ❌ | — |
| Value-add "while we're not able to negotiate the rate..." [pattern] | ❌ Partly — §14 has "lead with value" but no template phrasing | — |
| Exit feedback ask "is there anything we could have done better" [83×] | ❌ | — |
| Post-confirmation onboarding (15-min-early + parking + buggy + crew) | ❌ The pattern is described abstractly in §15 step 4, but no template | — |
| Energy phrases (Sweet / Lovely / Got it / Surething / Copy that / Perfect) | Partial — META-RULE A and §4 say "casual" but only "Sweet!" and "Got it!" are quoted | — |

### What to add

**5 specific phrasings** + a small **energy-phrases vocabulary list**. Total ~35–45 lines.

Distilled per operator's spec:
1. The walk-away (already in prompt — KEEP, no change)
2. The "another strong call" urgency close — *one line*
3. The value-add "while we're not able to negotiate the rate, we'd be happy to discuss adding [X]" template — *one line*
4. The exit feedback ask — *one line*
5. The post-confirmation onboarding template — *4–5 lines*
6. Energy phrases list — *single line of vocab*

### Where they land

Spread across three existing sections, not a new section (keeps prompt structure intact):

| Phrase | Lands in |
|---|---|
| #2 "another strong call" urgency close | §1 Pricing rules block (right after rule 7, the payment-link confidence rule) — ~1 line |
| #3 value-add template | §14 Negotiation Playbook → "Sequence" subsection (line ~504) — appended as one bullet |
| #4 exit feedback ask | §16 Hard Stops → new sub-bullet "When customer says no and walks away" — ~2 lines |
| #5 post-confirmation kit template | §15 Closing & Confirmation Flow — appended after the 5-step list, ~6 lines |
| #6 energy phrases | §4 Voice & Tone — extend the existing emoji bullet with a vocab line, ~1 line |

**Net additions in §1, §4, §14, §15, §16:** ≈ 11 lines.

---

## Gap 4 — §7.6 retired/live yacht contradiction

### **CORRECTION TO YESTERDAY'S INVENTORY**

The inventory doc `docs/hermes-current-state-inventory.md` claimed:

> "§7.6 'Retired / on-request only' in the system prompt still mentions yachts that also appear in §7.2 (e.g., Élan 44, Diana 50)."

**This was a misread of the prompt.** Re-checking the live `hermes-bridge/system-prompt.md`:

- §7.2 (Essential tier) **does** list `Élan 44 | 12 | 799 | elan-44` and `Diana 50 | 12 | 1,100 | diana-50` as live.
- §7.6 (Retired) lists ONLY: Beneteau 37 ft (Sailing), Eclipse 65 ft Doha, Palazzo Yacht Doha, Sanlorenzo SX88, Maiora 105, Lana 62. **Neither Élan 44 nor Diana 50 appears.**

**There is no §7.2 vs §7.6 contradiction for these two yachts.** The inventory finding was incorrect.

The operator is still free to confirm Élan 44 + Diana 50 are correct as live (the inventory note about active customer interest in both is unchanged — `customer_facts` shows both being discussed). But there's no contradiction to resolve in the prompt itself.

### Action

- **No prompt change required for Gap 4.**
- The inventory doc should be amended with a correction note (do this in the next phase, not now).
- Operator confirmation that Élan 44 + Diana 50 remain live remains a valid open question (see open-questions doc).

---

## Total additions vs budget

| Gap | Lines added (net) | Notes |
|---|---|---|
| 1 — Country reference table | ~22 | Replaces 5-bullet META-RULE B body with 10-row table |
| 2 — Ghost recovery timing | ~9 | Replaces 4-bullet ladder with 5-row table |
| 3 — Winning phrases | ~11 | Spread across 5 existing sections; no new section |
| 4 — Yacht contradiction | 0 | False alarm; no change |
| **Total** | **~42 lines** | **Well under the 200-line budget** |

Resulting prompt size: **584 → ~626 lines** (target was ≤ 800).

The plan is deliberately conservative on additions because the existing prompt already covers ~80% of what the source docs contain — the 4 gaps target the highest-leverage missing specifics, not a content dump.

---

## Build phase — time estimate

After operator approves the proposed text (in `cowork-prompt-additions-proposed.md`) and resolves the open questions:

| Step | Time | Notes |
|---|---|---|
| Apply edits to `hermes-bridge/system-prompt.md` | 15 min | 3 surgical edits (Gap 1, 2, 3) |
| Apply same edits to workflow's Build Prompt embedded copy | 10 min | Same 3 edits, symmetric to avoid drift |
| `deploy_bridge.py` + `safe_put` | 5 min | Same dual-deploy pattern as `b0d30da` |
| Live test with 5–10 representative messages | 30 min | Country variations, ghost-recovery scenarios, value-add pivots |
| Iterate on any miscalibration | 15–30 min | Buffer |
| Commit | 5 min | Single commit, dual-file |
| **Total build phase** | **~1.5 h** | After approvals |

---

## Rollback path

Standard same-pattern rollback per `b0d30da`'s precedent:
1. Restore `~/hermes-bridge/system-prompt.md.bak.<ts>` on the box, `deploy_bridge.py` to redeploy.
2. Restore `workflows/phase-1b-telegram.json.PRE-COWORK4` backup via `safe_put`.
3. Both backups created automatically by `deploy_bridge.py` + `safe_put`.

No DB writes, no workflow node additions, no new endpoints — rollback is purely text-replacement.

---

## Open questions for operator

(Full list in `docs/cowork-integration-open-questions.md`.)

The decisions that materially change build:

1. **Gap 1 placement:** Option A (replace META-RULE B body) vs Option B (new §4.5)?
2. **Country list scope:** the 9 segments listed are correct? Add/remove any?
3. **Spain treatment:** lump with GCC, with India ("perk in pocket"), or stand-alone?
4. **Gap 3 selection:** all 5 phrasings, or trim some?
5. **Élan 44 + Diana 50:** confirm both are correctly listed as live (inventory's "contradiction" was incorrect — these yachts only appear in §7.2 Essential, not in §7.6 Retired). Are operators discussing them with current prices (AED 799 + AED 1,100)?
6. **Off-scope items from source docs we are NOT adding:**
   - China Mandarin-host pitch (operator's spec already covered this but maybe operator wants the Mandarin opener line "你好 [Name]")
   - Russia Chef Artem pitch (currently in prompt as Russian Fine Dining mention — operator may want explicit "promote Artem in Russian-customer flow")
   - Repeat-customer outreach templates from Winning Phrases ("new yacht for you", "brand-return ping") — currently §1 has a brief reference but no template

These 6 items would each add 2–5 lines if approved; total would stay under the 200-line budget.

---

## Not in scope (intentionally)

- ~/.hermes/skills/ activation — separate task per inventory finding (skills exist but no consumer)
- Workflow Build Prompt drift fix — separate task per inventory finding §1.3
- behavior_rules / customer_notes table additions — operator can capture rules via `/feedback` instead
- New tables / new architecture
- Any prompt section that yesterday's `b0d30da` touched (length rules, name overuse, file hallucination, phone calls) — OFF-LIMITS

---

**End of plan.**
