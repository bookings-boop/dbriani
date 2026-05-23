# Cowork Integration — Open Questions for Operator

> **Status:** awaiting operator decisions. After answers, the build phase per `cowork-targeted-integration-plan.md` can start (~1.5 h).
> Companion docs: `cowork-targeted-integration-plan.md` (the plan), `cowork-prompt-additions-proposed.md` (the actual text).
> **Default plan proceeds with the bolded option if no answer is given.**

---

## Q1 — Gap 1 placement

The new per-country reference table can land two ways:

| Option | Description | Pros | Cons |
|---|---|---|---|
| **A (recommended)** | Replace META-RULE B's 5 single-line bullets in §1 with a 10-row markdown table. Keep the conversion-stats paragraph below it. | Smaller net additions (~17 lines), keeps §1's existing cultural-signal home. | META-RULE B body grows from 5 bullets to a 10-row table — slightly less scannable. |
| B | Keep META-RULE B as-is (5 bullets, untouched); add NEW **§4.5 Country Adaptation** between §4 and §5 with the same 10-row table. | Cleaner separation; §1 stays "rules", §4.5 is "playbook". | +35 lines instead of +17; new section adds prompt structure to learn. |

**Default: A.**

---

## Q2 — Country list scope

The proposed table covers 9 segments:

UAE · India · USA/Canada/Australia · UK · Russia/Kazakhstan · GCC (5-country bundle) · Spain · Germany/France/Italy · China

**Open:**
1. Any segment to drop? (e.g., China N=38 — small sample, but 13.2% win rate is the highest.)
2. Any segment to add that's not here? (Inventory data has Pakistan N=233, Israel N=127 — both currently OMITTED. Add either? Note: Pakistan's win rate is 1.29%, Israel 2.36%.)
3. Should the **zero-conversion countries** (Netherlands, Qatar via chat, Italy, Ukraine — all 50+ chats but 0 verified wins) get an explicit DON'T-SPEND-EFFORT-HERE bullet, or do you want that handled at the budget/audience layer outside the prompt?

**Default: ship the 9 segments as-is. No additions, no zero-conversion warning in prompt.**

---

## Q3 — Spain treatment

Spain has the highest objection rate in the dataset (17.4%) and behaves similarly to India (haggle expected, perk in pocket). Two options:

| Option | Description |
|---|---|
| **A (recommended)** | Spain gets its own row in the country table (current proposal). |
| B | Lump Spain with India in a single row labeled "🇮🇳 India / 🇪🇸 Spain (haggle expected — perk in pocket)". |

**Default: A.** Spain's culture, language acknowledgement opportunity ("¡Hola!"), and payment preference (USDT-friendly) differ enough from India to keep separate.

---

## Q4 — Gap 3 phrase selection (all 5, or trim?)

Currently proposing 5 phrasings + 1 vocab list (total ~20 lines). If prompt-budget concerns require trimming, recommended priority order:

| Priority | Phrase | Cost (lines) | Why ship |
|---|---|---|---|
| 1 | **Value-add template** (Gap 3b) | +3 | Highest-leverage — used in every negotiation step 3 |
| 2 | **Post-confirmation kit** (Gap 3d) | +10 | Closes the broken-promise loop after payment |
| 3 | **"Another strong call" urgency** (Gap 3a) | +2 | Cheap, +121× verified usage in wins |
| 4 | **Exit feedback ask** (Gap 3c) | +4 | Recovers ~50% of walk-aways (operator can also handle manually) |
| 5 | **Energy phrases vocab** (Gap 3e) | +1 | Smallest cost; cheap polish on §4 |

All 5 fit easily in the 200-line budget. **Default: ship all 5.**

---

## Q5 — Élan 44 + Diana 50

**The inventory's "contradiction" was a misread on my part.** Re-checking the live prompt:
- §7.2 (Essential tier) lists both as live: `Élan 44 | 12 | 799 | elan-44` · `Diana 50 | 12 | 1,100 | diana-50`
- §7.6 (Retired) lists: Beneteau 37 ft, Eclipse 65 ft Doha, Palazzo Yacht Doha, Sanlorenzo SX88, Maiora 105, Lana 62. **Neither yacht appears in §7.6.**

So there's nothing to resolve in the prompt itself. The valid open question remains:

1. **Confirm both yachts are still operationally available** at the stated prices (AED 799/hr Élan 44, AED 1,100/hr Diana 50).
2. `customer_facts` shows both being actively discussed in customer chats — that lines up with them being live.
3. If either is **actually retired**, it should be moved from §7.2 to §7.6 with a closest-substitute mapping.

**Default action if no answer: ship the country-table + ghost-recovery + winning-phrases edits without touching the yacht catalog. Élan 44 and Diana 50 stay where they are.**

The inventory doc (`docs/hermes-current-state-inventory.md`) should be corrected in the next phase to remove the false contradiction claim — minor doc cleanup, separate from this build.

---

## Q6 — Off-scope items from source docs we are NOT adding (operator may want)

Six things the Cowork source docs cover that this plan deliberately does NOT pull in. Each would add 2–5 lines if approved; all fit within the 200-line budget.

| Item | Source | Add? | Cost |
|---|---|---|---|
| China Mandarin opener line ("你好 [Name]! Maria from Dubriani") | Per-Country Templates | maybe | +2 |
| Russia Chef Artem explicit pitch ("Russian-speaking chef Artem onboard if you'd like") | Per-Country Templates | maybe | +2 |
| Repeat-customer outreach templates ("we have amazing news... new yacht just for you") | Winning Phrases | maybe | +4 |
| GCC multi-day pitch template ("for [N] days we can do AED [daily] per day") | Per-Country Templates | maybe | +3 |
| Brand-return ping for VIPs ("Your favorite yacht brand is back in charter") | Winning Phrases | maybe | +2 |
| Holiday greeting line ("Happy new year. May this year be your best year yet.") | Winning Phrases | likely no | +1 |

**Default: none of these — keep the build surgical.** If the operator wants any, mention in the response.

---

## Q7 — Prompt size budget check

Current: 584 lines · Proposed additions: +44 lines · Resulting: 628 lines · Hard ceiling: 800 lines.

This leaves ~170 lines of headroom for future calibrations (operator feedback, new yacht additions, new countries, etc.).

**Operator OK with this size growth?** If concerned, the table-based approach (Gap 1, Gap 2) is the most efficient form of additions — the data density per line is high. Plain-prose alternatives would cost 2–3× more lines.

**Default: ship as-is.**

---

## Q8 — Anything from the 14 OTHER Cowork docs (not the 5 used here) operator wants pulled in?

This plan only touched 6 cited docs (the ones tied to the 4 gaps). The other ~11 Dubriani-prefixed docs at `~/.hermes/skills/` cover topics this plan does NOT integrate:

- `Dubriani Ad Scheduling - Country × Time.md` — relevant to ads/marketing, not drafting
- `Dubriani Conversion Rules — What Works & What Doesn't.md` — partially pulled for §3.3 only; ~13 KB of other findings unused
- `Dubriani Customer Pattern Recognition.md` — pattern signals (24 KB)
- `Dubriani Human Communication Rules.md` — the "NEVER COPY-PASTE" rule and friends
- `Dubriani Master Agent Skill.md` — the 60-KB mindset playbook
- `Dubriani Name & Demographic Recognition.md` — the "Displaced Customer" 33% rule
- `Dubriani Per-Yacht Conversion Analysis.md` — win rates per yacht
- `Dubriani VIP Re-engagement Strategy.md` — VIP-specific follow-up plays
- `Dubriani WhatsApp Agent - System Prompt.md` — a stale parallel system prompt (36 KB, older than the bridge's copy)
- `HNW Conversion Playbook.md` — HNW-specific playbook
- `Loss Reason Analysis & Recovery Playbook.md` — loss-reason analysis

**Operator: any specific item from these that you want included in this build?** Otherwise they remain dormant (as the inventory flagged) and we tackle them in a separate task.

**Default: nothing extra in this build.**

---

## Decisions summary table (for quick reply)

| Q | Default | Operator decision |
|---|---|---|
| Q1 — Gap 1 placement | A (table replaces META-RULE B body) | |
| Q2 — Countries | Ship 9 segments as-is | |
| Q3 — Spain | A (own row) | |
| Q4 — Gap 3 phrases | Ship all 5 | |
| Q5 — Élan 44 / Diana 50 | No change (inventory was wrong; both stay live) | |
| Q6 — Off-scope adds | None | |
| Q7 — Size growth (+44 → 628 lines) | OK | |
| Q8 — Other Cowork docs | None | |

Reply with **"defaults all"** or per-Q overrides. After answers, the build phase runs ~1.5 h.

---

**End of open questions.**
