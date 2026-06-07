# Draft-quality remaining work — scope + options + risks · 2026-06-06

Scope-only (no code/n8n edits until owner decides). Already shipped: scorer
STYLE/TONE align, positive-8 rubric, score telemetry. This covers the 4
remaining items. Baseline HEAD `a5428a1`. n8n workflow = `azPIy9OcDwiPV5uY`
(any node change = back up + show diff before PUT).

## Confirmed architecture
- **Badge / `score`** ← n8n `Hermes Improve` → `POST /quality-check` →
  `run_hermes(build_quality_query)` = **local fine-tuned Hermes** model
  (routes.py:5036-5056). Also runs the deterministic `validate_draft_prices`
  guard (caps score ≤3 on catalog price mismatch).
- **`newScore`** ← n8n `Apply Improvement` → `POST /draft-gated` →
  `_anthropic_score` = **Anthropic claude-sonnet-4-6** (routes.py:5263-5311).
  Regenerates its OWN draft + re-scores, up to 3 attempts.
- **Swap guard** (`Apply Improvement`, workflow line 2235):
  `if newScore > score` → replace original with the gated regen.
- All scorers receive `messages.join("\n\n")` (build_quality_query:2818-2820).

## Item 1 — Unify the 2 judges (so the swap compares like-for-like)
**Root cause:** swap compares `newScore`(Anthropic, on the regen) vs
`score`(local-Hermes, on the original) — different models AND different drafts.
- **Option 1B (RECOMMENDED, surgical):** pass the ORIGINAL draft text to
  `/draft-gated`; inside it, score the original with the SAME `_anthropic_score`
  → return `original_score`. n8n swap compares `newScore` vs `original_score`
  (Anthropic-vs-Anthropic). Badge stays local-Hermes (display only). +1 Anthropic
  call per regen. Touches: bridge `/draft-gated` + n8n swap guard. **Risk: LOW.**
- **Option 1A (full unify):** make `/quality-check` ALSO use `_anthropic_score`
  so badge == gate scorer everywhere. Cleanest conceptually, but adds an Anthropic
  call to EVERY draft badge (cost), shifts badge score distribution (re-validate
  the 8-floor), and drops the tuned-Hermes judgement. **Risk: MED** (cost + floor
  recalibration). Note: `/draft-gated` cannot use local Hermes — 3× CLI scores
  blew the n8n 150s timeout (reverted 2026-06-02), so unify must be ON Anthropic.
- Note: `Parse Regen`/`Parse Refine` badges also use `/quality-check` (local
  Hermes); 1A would unify those too, 1B leaves them as display badges.

## Item 2 — Stop flattening bubbles before scoring (wall_of_text misfire)
**Root cause:** `messages` joined to one `\n\n` blob → scorer can't see real
bubble boundaries → `wall_of_text` rule (a hard ≤5 cap) misfires on correctly
split drafts.
- **Option 2A (RECOMMENDED, correct fix):** pass the bubble ARRAY through to the
  scorer and format it with explicit markers in `build_quality_query`
  (`[bubble 1] … [bubble 2] …`), and reword the rubric to judge per-bubble.
  Touches: bridge `build_quality_query` + n8n nodes that pre-join (`Check
  Pending`, `Parse Regen`, `Parse Refine` send `current_draft` as a string today
  → send `messages` array). **Risk: MED** — score distribution shifts, re-validate
  8-floor; n8n PUT.
- **Option 2B (bridge-only, lighter):** keep the join but reword the
  `wall_of_text` rubric so `\n\n`-separated content is explicitly "own line =
  correct" and only penalize comma/inline cramming. No n8n change. **Risk: LOW**
  but less precise (scorer still guesses bubble vs paragraph).

## Item 3 — Inject ground-truth catalog facts into regen (not just flag names)
**Root cause:** regen hint = `"scored N/10, fix: <flag names>"` (routes.py:5319)
— tells the model the CATEGORY of error, never the correct FACT. Ties to the
RCA price-fabrication bug (no validator feeds the right number back).
- **Option 3A (RECOMMENDED):** have `validate_draft_prices` return the CORRECT
  catalog price, and inject it into the regen hint ("Bliss 55 is AED 1400/hr, not
  the 600 you quoted; use the catalog price"). Scope to price/spec corrections
  (highest value, deterministic). Touches: bridge (price guard + hint). **Risk:
  LOW** (catalog lookup already exists).
- **Option 3B (complement):** forward `/quality-check` flags + summary into n8n's
  `Build Regen Prompt`/`Build Refine Prompt` regenHint (currently dropped). n8n
  change. **Risk: LOW-MED** (n8n PUT). Best paired with 3A.

## Item 4 — Relax the `newScore > score` swap guard
**Depends on Item 1** (meaningless until judges are unified).
- **Option 4A (RECOMMENDED, after 1):** change to `newScore >= original_score`
  (accept a tie — the regen is at least as good, often fresher) OR
  `newScore >= threshold`. Approval-gated regardless. Touches: n8n swap guard.
  **Risk: LOW** (operator still approves every swap).

## Recommended bundle + sequencing
1. **Items 1B + 4A together** (same swap-decision code; highest value) — bridge
   `/draft-gated` returns `original_score`; n8n swap uses `newScore >= original_score`.
2. **Item 3A** (bridge-only price ground-truth into regen) — independent, low-risk.
3. **Item 2** (2A correct fix, or 2B if avoiding n8n) — improves all scores; do
   last so the 8-floor is re-validated once.

All are TDD-able: `build_quality_query` formatting, `validate_draft_prices`
return value, the hint builder, and a pure swap-decision helper are unit-testable;
n8n swap-guard JS gets a golden-input test. n8n edits = back up + diff before PUT.
