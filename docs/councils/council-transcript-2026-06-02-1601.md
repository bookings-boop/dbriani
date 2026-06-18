# LLM Council Transcript — Drafting in n8n vs the Bridge

**Date:** 2026-06-02 16:01 +04
**System:** Hermes / Dubriani — live WhatsApp yacht-charter booking system (Phase 1B workflow, n8n id `azPIy9OcDwiPV5uY`)
**Mode:** Claude Code, sub-agents — 5 advisors (parallel) → 5 anonymized + randomized peer reviewers (parallel) → 1 chairman.
**Caveat:** five instances of the same base model with different thinking-style prompts, NOT five different models. Unanimity treated with suspicion.

---

## Original trigger
> "stress-test this, my n8n workflow"

Scoped via two clarifying questions to: **a design/architecture decision** → **"Drafting in n8n vs the bridge."**

## Framed question (given to all 5 advisors)

> DECISION: In the live "Hermes"/Dubriani WhatsApp yacht-charter booking system, should the MAIN message-drafting (first draft + REFINE + REGEN) be moved OUT of the n8n workflow and consolidated into the Python "bridge" service, STAY in n8n, or go HYBRID (n8n orchestrates but calls one thin bridge /draft endpoint)?
>
> CURRENT ARCHITECTURE: n8n (Phase 1B, 32 nodes) builds the main draft + REFINE + REGEN by calling Anthropic directly from jsCode, using a single ~70KB "Build Prompt" node embedding the full catalog (104 prices, every yacht). The Python bridge (systemd on a 2-vCPU EC2 box, full pytest suite, git-versioned) handles everything else AND already contains working Anthropic-draft code (`_anthropic_draft`, `_gate_loop`, `_anthropic_score`) used by the "fast Draft" button + the ≥8 quality gate. The catalog therefore lives in TWO places (the 70KB n8n node + bridge `system-prompt.md`) — verified in-sync today, but two copies to maintain.
>
> CONSTRAINTS: Live revenue path, approval-first (operator OKs every draft). Recurring problem = drafter hallucinating OFF-catalog prices/specs; NO_INVENT mitigates but gets buried in 70KB. Box is 2-vCPU, load-sensitive (has melted under load). n8n jsCode is tested only by `node --check` (no real test harness); the bridge has real pytest + git. A gold-conversation retrieval feature is designed-but-unbuilt and is bridge-side. One-operator team; migration touches the live customer path.
>
> AT STAKE: hallucination rate / booking conversion, maintainability + drift risk (one catalog vs two), testability, box stability/latency, cost visibility, and the risk of surgery on a live revenue path.
>
> OPTIONS: (A) Consolidate drafting into the bridge. (B) Keep drafting in n8n (status quo). (C) Hybrid — n8n keeps orchestration but calls one thin bridge `/draft` endpoint.

## Anonymization mapping (revealed)
- **Response A** = The Expansionist
- **Response B** = The Contrarian
- **Response C** = The Outsider
- **Response D** = The Executor
- **Response E** = The First Principles Thinker

---

## Advisor 1 — The Contrarian
Option C (Hybrid) is the only defensible answer, and even it has a trap you're glossing over. But first kill Option A outright. The hallucination is your bleeding wound, not architecture elegance. Moving the prompt into the bridge does NOT lower the off-catalog invent rate by itself — the model invents the same "375 AED fine dining" whether the prompt ships from a jsCode node or a /draft endpoint. If your real goal is fewer fabricated prices, the fix is a programmatic post-draft validator that regexes every price/spec/yacht-name against the 104-entry catalog and hard-rejects unknowns. That lives wherever drafting lives — and you don't have it in EITHER place today. Don't let a migration masquerade as a quality fix.

Now the trap in C: you're about to put a synchronous Anthropic call behind a /draft endpoint on a 2-vCPU box that has melted under load and already ate one Anthropic timeout in n8n. A draft + ≥8 gate loop can mean 3-5 sequential model calls; under a burst of concurrent leads you serialize those on two cores while payments, scoring, and identity-merge contend. You moved the timeout from n8n into your revenue path. Mitigation must be explicit: async job + poll, a hard concurrency cap, and a timeout-fallback that returns the n8n draft.

Pressure-test the payback: one operator, live revenue path, and the win is "one catalog instead of two that you verified in-sync today anyway." That's days of surgery for drift you can also kill with a 10-line CI diff check. Do C only if you build the validator and the async/fallback first. Otherwise STAY.

## Advisor 2 — The First Principles Thinker
Drafting is not the problem. Drift is. And the system already proved the cause today: the catalog and NO_INVENT directive live in two places, hand-verified in sync. "Verified in sync today" is an admission that nothing structurally keeps them in sync — you are one busy week from a silent fork, and the failure mode is exactly the recurring symptom you named: off-catalog prices and specs. Hallucination here is not random model behavior; it is the model reading a 70KB blob where NO_INVENT is buried at low salience and the catalog is one undifferentiated wall. You are not "fixing hallucination," you are fixing a prompt that makes hallucination likely.

So the real question is not "where should drafting run." It is "where does the single source of truth for catalog + NO_INVENT live, and what reads it." Once you ask that, the answer collapses: there must be ONE prompt artifact, and everything that drafts or scores must read THAT artifact. The drafter currently can't, because it's a separate 70KB copy.

That makes B (status quo) structurally indefensible — it is the two-copy state you already know is fragile. Between A and the hybrid: you do not need to move orchestration, retry logic, Telegram cards, or 32 nodes onto a load-sensitive 2-vCPU box. You need the prompt to come from one place. The bridge already owns `system-prompt.md` and `_anthropic_draft`. So expose exactly that: a thin `/draft` endpoint. n8n keeps orchestrating; the 70KB node dies; NO_INVENT and the catalog exist once; the unbuilt retrieval feature now has a host. The irreducible fix is collapsing two prompts into one — the hybrid does that with the least surgery on a live revenue path.

## Advisor 3 — The Expansionist
Drafting lives in n8n today. Move it into the bridge (Option A) and you stop running a 70KB string through a jsCode node and start running a real software asset — and that unlock compounds.

The big unlock is the **gold-conversation retrieval feature**. It's designed, it's bridge-side, and it is structurally impossible to feed the n8n drafter as things stand. The moment drafting lives in the bridge, retrieval plugs straight into `_anthropic_draft`. That is the single highest-leverage quality move you have: grounding every first-draft in real past *booked* conversations is exactly what kills the off-catalog hallucination problem at the root — not a buried NO_INVENT line, but live exemplars of correct pricing. Conversion follows grounding.

Second unlock: once `_anthropic_draft` is the one drafting path, the ≥8 `_gate_loop` and `_anthropic_score` wrap around the MAIN draft for free — auto-regen-until-quality on every customer message, not just the "fast Draft" button. You already built that machinery; consolidation lets it earn its keep everywhere.

Third: one prompt, one catalog, git-versioned, pytest-covered. Every future capability — A/B prompt variants, per-yacht upsell logic, multilingual drafts, price-feed auto-sync from a real source of truth — ships as a tested Python diff, not a manual API PUT against an unversioned 70KB node. The bridge becomes your drafting *platform*; n8n stays what it's good at: orchestration and the Telegram card.

Hybrid gets you the single-source-of-truth but leaves drafting logic split across two runtimes forever — you'll never fully consolidate, and retrieval/gating will always be a reach-around. Go A. The box is 2-vCPU, but drafting is I/O-bound on the Anthropic call; you're not CPU-burning, you're awaiting. Build the platform that makes the next ten features cheap.

## Advisor 4 — The Outsider
The catalog living in two places, hand-synced, is the thing that jumps out. You have a 70,000-character prompt where the "don't invent things" rule is buried, and a parallel copy of 104 prices in a Python file. Two copies of the same truth, maintained by one person, by hand. That is a guarantee that they will drift, and "the AI invents a price" is exactly what drift looks like. I'd bet some of the "hallucinations" are just the two copies disagreeing.

So my naive question: why does the catalog exist twice at all? Nothing in what you've told me justifies it. One source of truth is not an architecture debate; it's hygiene.

Option C reads, to me, as plain English: put the prompt, the catalog, and the rule in exactly one place, and have the workflow tool call it. You already have working drafting code on the Python side. You already have tests on the Python side. The example-feeding feature you want only works on the Python side. Every asset you mention lives on the Python side except the drafting itself. That's lopsided enough that keeping drafting in the visual tool looks like inertia, not a decision.

One thing nobody seems to be saying out loud: a human approves every draft, yet the AI still ships invented prices often enough to be "the recurring problem." That means your human reviewer is rubber-stamping fake numbers. Fix the source-of-truth, but also ask why the human doesn't catch a "375 fine dining" that doesn't exist. And — dumb question — why is the entire 104-item catalog in the prompt instead of looked up for the specific boat being discussed?

## Advisor 5 — The Executor
Move the main drafting into the bridge (Option A). Here is Monday morning.

Monday step one: do NOT touch n8n. In the bridge, add a `/draft` endpoint that wraps the EXISTING `_anthropic_draft` + `_gate_loop` you already run for the fast-Draft button. You are not writing new drafting code — that code is in production already. You are exposing it. Write the pytest first (empty-draft, off-catalog-price assertion, timeout path), because that is the thing n8n can never give you.

What ships behind a flag: a `DRAFT_VIA_BRIDGE` env var read by the ONE n8n jsCode node that calls Anthropic. `true` → it POSTs to `http://<container-ip>:8788/draft`. `false` → the existing 70KB-prompt path runs untouched. First-draft only. REFINE and REGEN stay in n8n on day one. You flip the flag on yourself, watch ten live drafts on the Telegram card, flip it back if anything smells wrong. Flip is one env var, not a redeploy.

Rollback: set the flag to `false`. Zero n8n node changes were made to the fallback path, so there is nothing to revert. Bridge keeps its `.bak`.

Where the real cost hides: NOT the endpoint — that is an afternoon. It is the 2-vCPU box. The fast-Draft button already calls Anthropic bridge-side and hasn't melted, but the gate loop can fire 2-3 Anthropic calls per draft; under concurrent leads that is the load that killed you before. Mitigate: a single in-process semaphore (max 1-2 concurrent `_gate_loop`) and a hard 25s timeout returning a typed error the card shows as "retry," never an empty send.

The NO_INVENT win is free and immediate: kill the 70KB duplicate, catalog lives only in `system-prompt.md`, NO_INVENT sits at the top where the model actually reads it. REFINE/REGEN migrate next week, same flag pattern, only after first-draft proves out live.

---

## Peer Reviews (reviewers saw Responses A–E anonymized)

### Reviewer 1
1. **Strongest: D.** Only one that turns the decision into a reversible, testable migration (wrap existing `_anthropic_draft` behind `/draft`, `DRAFT_VIA_BRIDGE` flag, first-draft only, watch 10, flip back); prices the real risk (gate-loop = 2-3 calls on 2 vCPU) with concrete mitigation.
2. **Biggest blind spot: A.** Asserts moving the prompt "kills hallucination at the root" and that drafting is purely I/O-bound — ignoring the gate loop serializing calls on a box that melted. B and D flag exactly this.
3. **All five missed:** none questions whether REFINE/REGEN are safely co-migratable (REFINE edits an operator-APPROVED draft — moving it could silently re-invent prices a human already blessed); and no rollout metric to measure invent-rate before/after.
4. **Untrusted agreement:** all five treat "one source of truth = less hallucination" as proven; only a programmatic catalog validator (exists nowhere today) actually stops fabricated prices. Shared bias, not earned consensus.

### Reviewer 2
1. **Strongest: D** — reversible, falsifiable plan; absorbs B's load warning (semaphore, 25s timeout, never send empty) without freezing; earns A's single-source win without "migrate everything."
2. **Biggest blind spot: A** — bets on retrieval + auto-gating while dismissing the 2-vCPU box, the exact contention that caused a timeout; assumes moving the prompt cures hallucination, which B refutes.
3. **All five missed:** nobody proposes MEASURING the invent rate first — no logged baseline, no labeled set of fabricated-price incidents, so any fix ships blind and "better" is unfalsifiable. Nobody asks whether the operator's review step needs the catalog-validator surfaced TO the human.
4. **Untrusted agreement:** "two copies → drift → hallucination" is a plausible shared story, not evidence; consolidation may not fix invention.

### Reviewer 3
1. **Strongest: D** — reversible Monday sequence; names real cost (gate-loop 2-3 calls on 2-vCPU), mitigates (semaphore, 25s timeout, never empty-send). E reaches the same destination cleanly; B is the sharpest critic but stops at "STAY."
2. **Biggest blind spot: A** — sells compounding unlocks while waving away load with "I/O-bound, you're awaiting." Most ambitious, least operationally honest.
3. **All five missed:** none questioned whether the fast-Draft path and the n8n drafter use the same prompt/model TODAY (if they already diverge, "one source of truth" understates the problem); nobody proposed the cheapest test — diff the two catalogs NOW to confirm drift is actually the cause; no regression baseline.
4. **Untrusted agreement:** the near-unanimous "two copies → drift → hallucination" chain; the model can fabricate from a single perfectly-synced catalog. Only B breaks ranks correctly — a post-draft validator kills invented prices regardless of where the prompt lives.

### Reviewer 4
1. **Strongest: D** — separates the two real questions everyone blurs and answers both: picks A but executes it as C's thin endpoint, behind a flag, fast-Draft only, rollback = set flag false; names load risk and mitigates. B is the sharpest thinker but talks itself into "maybe STAY."
2. **Biggest blind spot: A** — asserts moving the prompt "kills hallucination at the root" and that retrieval/gating come "for free," waving off the 2-vCPU risk when the gate loop is 3-5 serialized calls.
3. **All five missed:** none questioned whether the human approval gate is the real control that's failing (except C glancingly) — if a human rubber-stamps fake prices, no source-of-truth refactor fixes that; instrument the approval step. Nobody asked for data: what fraction of invents are drift vs pure fabrication?
4. **Untrusted agreement:** all five treat "two copies → therefore consolidate" as proven; only B demands the validator that actually catches fabrication.

### Reviewer 5
1. **Strongest: D** — converts the insight into a reversible, load-aware migration; respects 2-vCPU and one-operator rollback. E reaches the same architecture but stays abstract; D ships Monday.
2. **Biggest blind spot: A** — treats migration as a hallucination cure, waves off load, moves REFINE/REGEN wholesale onto the fragile box at once. B and D both catch this.
3. **All five missed:** none said measure the actual invent-rate first, or diff the two catalogs today to confirm they're even out of sync — they debate architecture before confirming the diagnosis. C gestured at it but didn't push to verify. Nobody priced the unbuilt gold-retrieval feature as the real prize all options should be judged against.
4. **Untrusted agreement:** all five treat "one source of truth, worth surgery on a live revenue path" as self-evident; B is right that a ~10-line regex validator + CI diff-check kills drift far cheaper than any migration. The shared bias: mistaking elegant architecture for the actual fix.

---

## Chairman's Synthesis

### Where the Council Agrees
Four of five advisors converge on a thin-endpoint / consolidation answer, and the diagnosis is near-unanimous: the catalog living in two hand-synced places is structurally rotten and NO_INVENT buried in a 70KB blob is low-salience. The real convergence is narrower than it looks: **put the prompt + catalog + NO_INVENT in one artifact the bridge owns, expose it as a `/draft` endpoint, and do not move 32 nodes of orchestration onto the fragile box.**

But the reviewers are right to flag the load-bearing assumption as suspect. **"Two copies → drift → hallucination → therefore consolidate" is a shared story, not evidence.** Every reviewer independently called this untrusted agreement. A model invents "375 AED fine dining" perfectly well from a single, flawless catalog — fabrication is a generation behavior, not only a sync artifact. Treat the consensus as "this is correct housekeeping," not "this fixes the bleeding."

### Where the Council Clashes
- **Expansionist (A, all-in):** move everything; the prize is gold-retrieval grounding drafts in real booked conversations + the gate loop wrapping every draft for free. Dismisses the box risk: "I/O-bound, you're awaiting."
- **Contrarian (STAY unless validator + async/fallback built first):** days of surgery on a one-operator live revenue path to fix drift a 10-line CI diff also kills; a naive hybrid puts a synchronous multi-call gate loop on a 2-vCPU box that already melted and timed out once.
- **Middle (First Principles + Executor + Outsider):** one artifact, thin endpoint, keep orchestration in n8n — Executor adds the rails the Expansionist waved off (semaphore, 25s timeout, never-send-empty, flag-gated, first-draft only).

The Expansionist's "I/O-bound" claim is the weakest link (gate loop = 2-3 sequential calls serializing against payments/scoring/identity-merge on two cores; five reviewers flagged it). The Contrarian's load analysis is correct; his "STAY" is too conservative *only because* the Executor shows the migration can be made reversible and load-safe.

### Blind Spots the Council Caught
1. **No invent-rate baseline. Nothing is logged.** Any fix ships blind; "better" is unfalsifiable.
2. **Nobody diffed the two catalogs.** The consensus rests on "drift causes the hallucinations," yet no one ran the cheapest test to confirm drift is even occurring. If identical, the drift story is wrong, the model is fabricating from a clean source, and the validator is the only real fix.
3. **REFINE/REGEN co-migration risk.** REFINE edits an operator-approved draft; move it carelessly and you can silently re-invent a price a human already blessed.
4. **The human approval gate is itself failing.** A human approves every draft yet invented prices ship. Instrument the approval step; surface a validator's verdict to the human.
5. **The programmatic validator beats relocation for the stated problem** (Contrarian's lone-dissenter point; three reviewers said he broke ranks correctly). A check that every price/spec/yacht-name exists in the 104-entry catalog stops fabrication regardless of where the prompt lives. Exists in neither place today.

### The Recommendation
**Do not start with the migration. Start with measurement and the validator — then do the Executor's flagged thin-endpoint consolidation, not the Expansionist's all-in move.** The tie-breaking variable is what a catalog diff and an invent-rate log show.

1. **The validator is the highest-leverage fix and nearly free.** Side with the Contrarian's lone-dissenter point: a programmatic post-draft check (every price/spec/yacht-name must exist in the 104-entry catalog, else hard-reject + force regen). ~A day in the bridge's existing pytest harness; its verdict can be surfaced to the human approver, fixing the rubber-stamp gate at the same time. The actual cure for the actual disease.
2. **Then consolidate via the Executor's plan, not the Expansionist's.** One artifact (`system-prompt.md`), kill the 70KB duplicate, expose `_anthropic_draft` + `_gate_loop` behind a flagged `/draft` endpoint, **first-draft only**, `DRAFT_VIA_BRIDGE` env flag, watch ten live drafts, rollback = flip flag false. Load rails mandatory: in-process semaphore (1-2 concurrent gate loops), hard ~25s timeout returning a typed "retry" that never sends empty, fallback to the n8n draft path.
3. **REFINE/REGEN migrate later**, same flag pattern, only after first-draft proves out — with an explicit check that REFINE cannot re-invent a price in an already-approved draft.
4. **Reject the Expansionist's framing** that relocation "kills hallucination at the root" and that the box is a non-issue. Retrieval is a real prize and the bridge is its right host — but it's step N, behind the same validator and load rails.

### The One Thing to Do First
Diff the two catalogs right now — the n8n "Build Prompt" node against the bridge's `system-prompt.md` — and grep your logs / approved-draft history for the specific fabricated values (e.g. "375") to get a first invent-rate count. One command's worth of output decides everything downstream: if they've drifted, consolidation is urgent; if they're identical, the model is fabricating from a clean source and the **validator — not the migration — is the whole ballgame.**
