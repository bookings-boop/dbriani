# Dubriani/Hermes — Production Incident Report (READ-ONLY diagnostic)
**Date:** 2026-06-07 night · **Scope:** root-cause analysis only — **nothing changed, deployed, committed, or fixed.**
**Method:** two adversarial multi-agent passes — (A) incident RCA per reported symptom (`wyucns2jg`, 18 agents, each finding skeptic-verified) and (B) regression/unresolved bug hunt over today's deploys (`wvxpjg4yl`, 59 agents, 38 confirmed). Where the two diverge, the RCA's live-evidence verdict wins and is noted.

---

## 0. Ground truth (verified — this gates every finding below)

- **Autonomous mode: GLOBALLY OFF.** `conversation_modes.__global_default__` = `approval`, set by `manual_killswitch` on **2026-06-03**. Per-customer: 42 approval / **1 autonomous**. The `/caps` HTTP endpoint does not exist on this bridge; reengage is also approval-first (`REENGAGE_MODE=operator`).
  - ⚠️ **THE ONE EXCEPTION — highest live risk:** conversation **`5532505120995@lid`** (this is Nassr, +971544855029) is **individually autonomous**, set by `activated_by=operator` at **2026-06-07 11:01:28** (no break_reason). On a *new* inbound, a draft that clears the quality floor (`>=AUTOSEND_MIN_SCORE=8`) + caps + break-checks **would auto-send with no human in the loop.** Left untouched (operator-set, read-only pass).
- **Code state: box ≈ local HEAD, zero uncommitted code.** Local HEAD = `62b89f8` (9 commits past the brief's assumed "latest" `27d7751`), branch `catalog-consolidation-2026-06-02`, **fully synced with origin, working tree clean**. The box has **no git repo** — the bridge runs from a flat dir `/home/ubuntu/hermes-bridge`; `~/projects/dubriani-ai-agent` is local-only. By sha256, every key bridge file is **box == local-WT == local-HEAD** EXCEPT:
  - `system-prompt.md` — **box is 1 commit behind** (HAS the Charlie no-fabricate rule from `27d7751`; MISSING the §2 length-cap directive from `62b89f8`). **May be moot** — the live drafter is the n8n direct-to-Anthropic node, so it's unverified whether this file is even the live prompt.
- **The "deployed-but-not-committed" worries are STALE:** `profile_lookup` Layer 1 (`98a9e72`) and the #10b payer-mismatch guard (`9a68758`) are both **committed + pushed + deployed**, and gated exactly as designed (`PROFILE_LOOKUP_ENABLED=0`; `KNOWN_CUSTOMER_PROFILE_ENABLED=1`).
- **Only uncommitted items are non-code:** untracked docs (`docs/FIX-PLAN-2026-06-06.md`, `councils/`, etc.) and one `.bak` — no bearing on the running bridge. **A careless `git clean` would delete them.**

**Bottom line:** there is essentially no box-vs-repo drift and no uncommitted code to clobber. The incident is driven by *behavioral/architectural* defects (some shipped today, some pre-existing), not by a bad deploy of mismatched code.

---

## Ranked findings — most → least customer/revenue impact

### P0 — Live risk, decide first

**P0-1 · The only live autonomous conversation (Nassr `5532505120995@lid`).** *(non-bug; live exposure)*
A new inbound from this cid could auto-send unsupervised. **Caused tonight?** No — operator-set at 11:01 today. **This is also the real answer to the "Nassr autonomous not working" report:** (a) flipping a conversation to autonomous does **not** re-arm an *already-pending* approval-mode card — autonomy only applies to the *next* draft from a *new* inbound; (b) his drafts score **6 (< 8 floor)**, so the fail-closed quality gate withholds anyway. **Decision needed:** keep or revert this cid to approval. **Risk:** reverting is a state write → needs your go. **Safe-now:** explaining the semantics to the operator (zero code).

**P0-2 · Xeno double-paylink — FINANCIAL, still UNGUARDED.** *(confirmed, pre-existing)*
Two payable **AED 14,900 / AED 15,200** Nomod links sent ~3 min apart today (the dup-guard was reverted and never re-applied). **Root cause:** zero idempotency on link **creation** — `payments.py:159 nomod_create_link` POSTs unconditionally; `routes.py:887 handle_payment_link` and the `send_paylink` intent (`routes.py:2633`) both call it with no pre-check; the only "guard" (`_has_recent_payment_link_sent`) merely rewrites the analyzer's *suggested_action*, never gates a send. Proximate trigger: operator re-ran the paylink flow on a freshly-spawned draft card as the customer iterated add-ons. **Caused tonight?** No. **Fix:** idempotency keyed on canonical cid+amount within ~30 min (query `autonomous_sends` + Redis NX), fail toward NOT creating. **Risk: HIGH (financial/state)** — too-aggressive guard blocks legit re-issues. **When: supervised/daytime.** **Also act:** confirm whether the two outstanding links are still live/payable in Nomod (real money exposure — unverified).

### P1 — Lost / invisible customers

**P1-1 · +971568241103 — genuine lead silently dropped (ROOT B).** *(symptom confirmed; drop-point LOW confidence)*
A real **10-pax enquiry, never served**: customer_facts row exists only from the 11:53 `/refresh-facts` recovery, **0 conversation_messages, 0 drafts**. **Root cause — corrected:** the bug-hunt blamed the n8n Claude node 400-ing and skipping the customer_facts upsert; the RCA skeptic **refuted that as the primary** — more likely a **WAHA→n8n webhook delivery miss** (container-IP drift / session instability), i.e. the drop happens *before* the drafter. **Unproven** — n8n `execution_entity` is permission-denied; needs n8n-owner creds to confirm. Corroborating bug-hunt finding: the W2 "record-before-AI" keystone is wired onto a **dead endpoint** (the live reply path is `/draft`-gated), so it never fires for the real path. **Caused tonight?** No (architectural). **Fix:** persist inbound on raw WAHA webhook receipt, independent of drafting (n8n `azPIy9OcDwiPV5uY`, via `n8n_deploy.safe_put`); reuse `_is_real_lead_cid` junk guard. **Class size:** bug-hunt counts **95 live intake gaps**, all ≥7d old (43 >90d) — **quantify and drain.** **When: supervised.** The other two brief numbers (+97477660900, +971505580190) fall in the same class.

**P1-2 · +919600080831 — confirmed booking invisible + balance missing.** *(NOT a drop — refuted)*
**Root cause — corrected:** the finder said "silent intake drop, never recorded"; the RCA skeptic **refuted it** — WAHA's contact API resolves the phone and the customer **exists under `@lid 28149249241314` ("D"), CONFIRMED, Von Dutch 40.** The real causes: (1) **identity-lookup gap** — no phone↔@lid mapping, so a phone search doesn't surface the @lid record; (2) the **outstanding-balance feature is unbuilt** (deferred "Feature B", `server.py:4257`). **Caused tonight?** No. **Fix:** phone↔@lid mapping so the record surfaces + the balance/owed render (total − paid). **Risk: HIGH (financial render on hot path);** booking total is operator-supplied, not DB-derivable. **When: supervised.** **Safe-now:** flag the @lid mapping to the operator (read-only).

**P1-3 · Antonio +971588211789 — today's view/extension not reflected.** *(confirmed)*
He came to view his paid Bliss 55, asked to move start to 17:00 Jun 20, we offered a discounted extra hour — none in Hermes. **Root cause:** post-confirmation modifications are captured **only in the analyzer's free-text reasoning**, never propagated to the structured booking record (`dates/booking_time/addons/booking_date_abs`) that drives the card; `_analyze_one` writes only `importance_*` columns. **Co-primary (skeptic):** an **outbound-capture gap** — the operator's 5PM/offer message at 11:19 isn't recorded either. **Time-bomb:** `dates='tomorrow'` is unanchored and will bury his card in ~10h. **Caused tonight?** Partial (pre-existing gap; the W5-dead regression below makes it worse). **Fix:** persist post-confirmation changes to structured fields. **Risk: MEDIUM/financial** — writing onto a PAID booking; **the extension/discount price is captured nowhere → do NOT fabricate it** (operator-confirmed only). **When: supervised.**

**P1-4 · Émilie +971547366777 — self-contradicting card.** *(root cause re-prioritized)*
Top: "booked & paid AED 3,587.85"; bottom: "likely LOST, first contact, no messages". **Root cause — corrected:** not "thin/empty history" (bug-hunt) and not simply "scores a paid booking on the convert axis" (finder) — the RCA skeptic pins it on an **unanchored relative date string**: the facts extractor stored `dates='tomorrow 4–7 PM'` and never anchored it to an absolute date, so the convertibility analyzer reads it as a passed/no-show first-contact and scores 0. The render guards for this exact case were *added* (review.py byte-identical to HEAD) but the **source extraction** is the defect. Directly tied to **P2-2 (W5 dead)** — `booking_date_abs` is NULL, so the relative-date veto and future-anchor warning are both inert. **Caused tonight?** No (today's commits added guards, not the bug). **When: supervised** (touches the extractor write path). **Open:** what re-ran the analyzer at 15:05 on a CONFIRMED lead is unexplained.

### P2 — Systemic quality (affects many leads)

**P2-1 · ROOT A — local Hermes saturation.** *(confirmed)*
**Root cause:** the bottleneck is the **local Hermes CLI subprocess limiter** (`hermes_calls.py`), **not Anthropic, not box hardware.** Two-lane semaphore: total=3, **background lane = max(1, total−2) = exactly 1 slot**; no `BRIDGE_HERMES_*_CONCURRENCY` override in the box `.env`. All 414 `WAITED` events were background-priority — analyzer/fact-extraction serialized behind a single slot. Sharper framing from the skeptic: the binding defect is the **unbounded `acquire()` (no timeout)** on a deliberately tiny lane. **Caused tonight?** Partial — the bg=1 cap predates tonight (`3c69f4a`, 2026-05-28); **tonight's changes increased background volume through it** (record-before-AI writes, per-lead WAHA top-up, followup-sweep, quality-check→Anthropic). **Fix (low-risk, no code):** add `BRIDGE_HERMES_BG_CONCURRENCY=4` (+ a bounded acquire-timeout) sized for the 4-vCPU box. **When: supervised** (needs a bridge restart on an incident box). **Open:** subprocess duration/RAM never sampled; undecided whether saturation causes stale-card data-loss vs mere lag.

**P2-2 · W5 booking-detail capture is DEAD in prod.** *(REGRESSION today — `47f620a`)*
`_merge_facts` (review.py:317) carries only `name/dates/yachts/party_size`, so **`booking_date_abs/booking_time/addons` are written NULL on every upsert** — live **0/222** rows populated. **This is the common root** behind missing dates/times for Xeno, Antonio, Émilie, and the India booking, and it renders the Émilie-class relative-date veto + future-anchor warning inert. **Fix: SAFE-NOW** — add the 3 keys to the `_merge_facts` carry loop (+ an integration test through `_merge_facts → _upsert_facts_sql`; the unit test passed only because it bypassed `_merge_facts`). Verify on box before/after.

**P2-3 · Durable conversation store (W2) corrupt at source.** *(REGRESSION today — `5853a87`/migration 010)*
Four coupled defects degrade analyzer input for ~100 leads: (a) `record_message` has **no `ts` param** → all messages collapse to backfill/analysis time (recency/ordering wrong); (b) store **polluted with non-sent drafts** (quality-check candidates, "NO REPLY" placeholders); (c) a **sparse-but-non-empty store skips the WAHA fallback**, shrinking history; (d) backfilled rows carry **insert-time timestamps** → weeks-old chats read as "minutes ago." **Fix: supervised** (read-path + write-path; coordinate with P1-1's record-on-webhook change).

**P2-4 · Legit non-converting leads still labeled "not a customer".** *(confirmed; + a NEW regression)*
**Root cause:** `labels._close_bucket` checks the `NOT_A_CUSTOMER` regex (`_NAC_RE`) **before** the LOST buckets, and `_NAC_RE` matches free-text reasoning that merely *mentions* spam/pitch/b2b/vendor/promo — including words describing a single stray message or Dubriani's *own* outbound upsell. Deeper cause (skeptic): terminal labels are classified by **keyword-matching the LLM's free-text reasoning** because there's no structured `close_reason`. **Pre-existing** lexicon (2026-06-02 "bug 5e"); today's `f5d2f31` + the 85-row rebucket didn't fix the classifier. **PLUS a new regression** (bug-hunt, W1 `d9fb963`): the 🛑 Disregard button writes `created_by='operator:disregard_button'` even when **Hermes** made the close decision, and `_is_operator_close` treats any `operator*` as sticky → **auto-reopen is blocked for ~19 real LOST customers** who return (skeptic confirmed real, downgraded high→medium; the 9 DISREGARDED are genuine non-customers and correctly stay closed). **Fix:** (i) re-order/role-anchor `_NAC_RE` vs LOST; (ii) write `system:hermes_disregard` (not `operator:*`) for Hermes-decided closes. **Lexicon/ordering fix is low-risk; the corrected DISREGARDED→LOST rebucket is supervised** (mass label rewrite). **Open:** real-mis-bucket fraction of the 85 only eyeballed, not measured.

**P2-5 · Mike (crypto/USDT refund scam) still LOST, not SCAM.** *(still-unresolved)*
The one-time backfill (deferred to "Wave 2") never ran, so Mike renders in the 💔 LOST "win-back" section; worse, his LOST is `migration`-tagged (reopenable), so a returning scammer could auto-reopen on booking language. **Fix:** the deferred 1-row relabel LOST→SCAM (rank 9, never reopens). **When: needs your go** (prod DB write).

### P3 — Latent risk (no current customer harm, but will bite)

- **Step-2 known-customer recognition is STORED-BUT-UNUSED** — the 🏆 block is wired into `build_query` (bridge), but the **live drafter is the n8n Claude node**, so it never reaches the draft. And it only matches `@c.us` — **~75% of the active book is `@lid`** and never matches. *(My own Step-2 work doesn't reach the live path — exactly the doubt I flagged.)*
- **Privacy:** if that 🏆 block is ever wired to the live path, it embeds the customer's **exact lifetime revenue** into the prompt.
- **DRAFT_SAVE fires 3× per draft** (still). **Never-miss intake runs 4×/day daytime** → ~12h overnight blind window + can't drain the 95-gap backlog. **Auto-ingest can create duplicate customer_facts rows** when WAHA `/lids` is incomplete. **Never-miss + `/refresh-facts` bypass the Layer-3 exclusion guard** (staff/crew can get recovery cards). **Reengage dedup TTL (900s) < cron interval (1800s).** **Fabricated-price validator** only hard-blocks on the (off) autosend path. **NEW-section recency sort is dead** (overwritten by `_value_key`). **R0 autosend score/send mismatch** (scores latest-pending; n8n fans out its own armed draft).

---

## Caused by tonight's deploys vs pre-existing

| Genuinely introduced/worsened today | Pre-existing (tonight only added load/exposure) |
|---|---|
| W5 booking-detail dead (`47f620a`) | ROOT A bg=1 cap (`3c69f4a`, May 28) |
| Durable-store 4 defects (`5853a87`/mig 010) | Xeno paylink: no creation idempotency |
| Disregard-button reopen-block (`d9fb963` W1) | "not a customer" lexicon (`_close_bucket`, Jun 2) |
| NEW-section recency sort dead; `_is_near_ready` sort placement | Mike SCAM-backfill never run |
| Increased bg volume → ROOT A saturation | Intake-drop architecture (record on webhook) |
| | Balance/Feature-B unbuilt; recognition not wired to live path |

---

## Safe-now vs supervised (and clobber risks)

**Safe-now (low risk, clean tree, non-financial):**
- Explain Nassr's autonomous semantics to the operator (zero code).
- **P2-2 W5 `_merge_facts` 3-key add** — smallest high-leverage fix; verify on box.
- `labels._close_bucket` lexicon **ordering/role-anchoring** fix (defer the rebucket).
- Read-only operator flags: the 2 outstanding Xeno Nomod links; the +919600080831 `@lid 28149249241314` mapping.

**Supervised / daytime only (financial / state / customer-facing send):**
Xeno idempotency + voiding live links · +919600080831 balance + mapping · Antonio structured post-confirmation writes (price operator-confirmed) · ROOT-A `.env` concurrency bump + restart · Émilie relative-date anchoring · corrected DISREGARDED→LOST rebucket · Mike→SCAM 1-row backfill · any Nassr autosend-arming change · all n8n drafter wiring (P1-1).

**Clobber warnings:**
- **n8n `azPIy9OcDwiPV5uY` is NOT git-tracked and the repo JSON lags live** → edit ROOT-B / India / Émilie n8n fixes **only** via `n8n_deploy.safe_put`; `deploy_bridge.py` or a stale-repo CLI import would clobber live nodes + the active draft queue.
- **`system-prompt.md` is 1 commit behind on box** → any prompt redeploy must carry `62b89f8` forward via `deploy_prompt_safeput.py`, never overwrite HEAD with the stale box copy.
- **Nassr `5532505120995@lid` autonomous** is an intentional operator setting → a mode/autosend fix must not clobber it.
- Untracked docs + `exclusion_phones.json.bak-*` → don't `git clean`.

---

## Still unexplained (honest gaps — don't over-trust)

- **ROOT-B drop point is unproven** (n8n `execution_entity` permission-denied; needs n8n-owner creds). WAHA timestamps for +971568241103 never gathered; silent-drop class size approximate (~95 gaps).
- **ROOT-A:** subprocess duration/RAM unsampled; the pivotal n8n `/customer-facts` node timeout unknown → data-loss-vs-lag undecided. Don't raise the cap blindly without sampling RAM.
- **Émilie:** what re-ran the analyzer at 15:05 is unexplained; live-WAHA-vs-durable-store read path asserted, not proven.
- **Xeno:** whether the 2 outstanding links are still payable in Nomod (the actual exposure) is unconfirmed; +7.1% paid-vs-link delta unexplained (likely a fee).
- **India:** booking total behind the ~3,100 balance is operator-supplied; `booking_date_abs/booking_time` are NULL for **all 8 CONFIRMED** — a systemic extraction gap (= P2-2).
- **Antonio:** the extension/discount price is recorded nowhere → fabrication risk.
- **Divergence with no symptom:** `system-prompt.md` box-vs-HEAD drift may be entirely moot (unverified whether that file is the live drafting prompt — live drafter = n8n direct-to-Anthropic).

---

*Sources: RCA `wyucns2jg` (full report `~/hermes-incident-rca-2026-06-07.md`), bug-hunt `wvxpjg4yl` (full findings `~/hermes-bughunt-confirmed-2026-06-07.md`). No production state was modified in producing this report.*
