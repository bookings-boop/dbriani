# Dubriani/Hermes — Consolidated RCA (2026-06-06)

Read-only root-cause investigation of 4 bug reports. **No code changed.** For owner approval before any fix.
Run via 4 parallel workflows. Full raw outputs: `/private/tmp/claude-501/-Users-macbook/482a4a74-7e98-4323-89b4-057d7c2e9a09/tasks/{wqun7c302,wn0mqoiy0,wbobmnjjv,wi8uqi33z}.output`

Bugs:
- **A — Draft pipeline** (price/score/improve-apply/style/upsell) — ✅ DONE
- **B — Lead analysis** (Marc HOT 90/100 despite declining + no-captain) — ⏳ running
- **C — Review sort/visibility** (Emilie on top; Milenski missing) — ⏳ running
- **D — Confirmed card** (Antonio: confirmed+paid renders as bare AWAITING) — ⏳ running

---

## BUG A — Draft pipeline (HIGH confidence)

### Ground truth (catalog is authoritative, NOT ambiguous)
| Item | Value |
|------|-------|
| **Von Dutch 40** | **AED 1,400/hr, 8 pax**, slug `von-dutch-40` — **NO discount exists in any prompt copy** |
| **Bliss 55** | AED 1,400/hr list, **17 pax**, standing anchor discount → **AED 1,100/hr ("ALWAYS apply")** |
| **AED 600** | = **Jetski–Normal 1 hr** (catalog §9.5) — the wrong row the model grabbed |
| Hardcoded specials | ONLY Bliss 55 (→1,100), Satoshi morning floor 1,500, Sunseeker 88 |
| "Operator rule 39 / AED 2,000 Satoshi discount" | **DOES NOT EXIST in repo.** Only ~2,000 Satoshi figure = §10 Proposals "negotiable ~AED 2,000 for 4+ hrs". Likely hallucination — confirm vs live-DB `behavior_rules`. |

### Root causes
- **A-S1 — "improvement does nothing / improved draft appended as text below":** ONE n8n node `Apply Improvement` in `workflows/phase-1b-telegram.json`, two defects:
  - (a) **Card-render fallback:** in-place swap uses `tp.text.indexOf(orig)` where `orig = draft_text` (`\n\n`-joined), but the card body renders multi-message drafts as a numbered preview `1. … 2. …` → `indexOf` returns -1 → falls into the `'improved draft:' + append` branch. Single-message drafts swap fine; **multi-message (the common case) always append below.**
  - (b) **Silent discard:** `newScore > score` strict gate throws away any equal-or-worse regen (7→7) with no operator signal. (`score>=8` is an intended no-op.)
  - ⚠️ **The improved text IS written to Redis** (`fields={draft_text,messages,...}` → `Improve Commit` → `/queue regen-commit`, `routes.py:352`). So tapping **Approve actually sends the improved version** even though the card shows it appended. This is also a "sent != seen" consistency risk (cf. 2026-05-28 incident).
  - Regen cutoff is `score < 8` → a 7 **does** trigger regen (my "7 is above cutoff" guess was **wrong**). The auto-rewrite happens server-side via `/draft-gated` (`routes.py:5160`), not an n8n Anthropic node.
  - **Fix:** rebuild the card body from `g.messages` using the SAME numbered formatting `Queue & Format` uses; substitute DRAFT section in place. n8n-only (no bridge deploy). Must deploy via a `build_*.py` re-fetch/`safe_put`, NOT by hand-editing local JSON.
  - **Test gap:** NO `test_*.py` covers n8n card render → manual staging test with multi- AND single-message drafts.

- **A-S2 — fabricated price (600):** Hypothesis "catalog not injected" **REFUTED** — full §7 catalog IS injected into drafter (n8n `Build Prompt`), scorer (`build_quality_query` → `system-prompt.md`). Real causes:
  - No **deterministic price validator** (only soft `NO_INVENT_DIRECTIVE`, `server.py:1027-1093`; `test_no_invent.py` only asserts the string exists, never validates a price).
  - **Asymmetric grounding:** 3 hand-synced catalog copies (`catalog-block.canonical.md` [source doc, not loaded at runtime], `system-prompt.md`, n8n inline) + scorer sees DB `behavior_rules` the drafter doesn't.
  - No **high-salience per-yacht fact card** → model retrieves wrong row from a 70KB table.
  - **Fix:** (1) deterministic price/spec validator on draft path (load `catalog-block.canonical.md` at runtime, parse AED + yacht names from `messages[]`, reject/flag mismatches); (2) inject filtered per-yacht fact card; (3) consolidate to ONE runtime catalog.

- **A-S3 — verbose / not Ritz-Carlton:** STYLE_DIRECTIVE + TONE_DIRECTIVE (`server.py:1101-1153`, "think Ritz-Carlton… elegant restraint… short considered sentences") reach the **drafter** (`Claude AI` appends `Fetch Behavioral Context.formatted`) and **scorer** (`build_quality_query`), but **NOT the rewriters** `Claude AI (Refine/Regen/Lead)` (system field = `$json.systemPrompt` only). So operator-triggered refine/regen strips the brevity rule. **No hard brevity cap exists anywhere.**
  - **Fix:** bake STYLE/TONE into shared base `system-prompt.md` (covers all 4 LLM nodes) OR add `Fetch Behavioral Context` to refine/regen/lead chains. Optionally add an enforceable per-bubble brevity cap + a verbosity penalty in the scorer (today it only penalizes cramming).

- **A-S4 — upsell ignores chosen yacht (CONFIRMED):** deterministic Python `_party_size_fit_line(party_size)` (`labels.py:1143-1173`) mandates "NEVER suggest below capacity / recommend largest fitting option" — signature takes party size ONLY, zero chosen-yacht awareness. `_lead_state_block` SQL (`server.py:1271-1276`) doesn't even `SELECT yachts`. The party=10 vs VD-40=8 gap is real; the bug is *how* it's handled. **Caveat (Gap #4):** the rule only fires on a clean integer; "up to 10"/"1-10" → returns "" → upsell comes purely from LLM with no mandate at all.
  - **Fix:** pass chosen/landing yacht into the fit logic; soften to "lead with their pick, flag the gap, offer ONE fitting alternative — don't silently drop it"; parse `Page: /yacht/<slug>/` from first inbound into a structured field; handle range/free-text party sizes. Do NOT delete the mandate (fixes a real prior 50-guest bug).

- **A-S5 — drift:** Committed **HEAD `b2784d4` == box** (all 6 post-`1e65a98` commits marked "Deployed bridge-only 2026-06-06"; `eaf1b38` "brings git up to the box"). **Working tree AHEAD** — 4 uncommitted LOST-label files (labels/review/routes/server) NOT on box. Local n8n JSON is a **post-deploy mirror, not push source**. Catalog has independent drift via `~/wf-put.json` (outside repo).
  - **Safest pre-flight (read-only):** md5sum box `~/hermes-bridge/*.py` vs `git show HEAD:hermes-bridge/<f>`; run `drift_check_catalog.py`; GET-only live n8n pull. Do NOT run `do_n8n_put.py` (mutates).

### Open gaps to verify before implementing Bug A
1. **`/draft-gated` directive injection** — NOT directly verified. If it lacks STYLE/TONE/NO_INVENT/price-ground, the auto-regen can go verbose (A-S3) AND fabricate price (A-S2) → ties S1+S2+S3 into one path. MUST confirm.
2. **"Rule 39 / 2,000 Satoshi"** provenance — only live DB `fetch_behavior_rules` can settle.
3. **Range party-size** path (A-S4 Gap #4).

### Owner decisions (Bug A)
1. Von Dutch 40: is **1,400/hr flat** the only correct quote (no sanctioned discount)?
2. "Rule 39 / 2,000 Satoshi discount" — real live-DB rule or hallucination to suppress?
3. Price enforcement: **HARD gate** (reject/regenerate on mismatch) vs **soft flag**?
4. Capacity overflow (10 vs VD-40=8): (a) lead with chosen + note gap, (b) chosen + 1 alternative, (c) hard-switch (current)? And "chose this yacht" = landing referral, named-in-chat, or both?
5. Brevity: hard per-bubble cap (max sentences/words)? Should scorer penalize verbosity?
6. A-S1 secondary: visible badge on discarded regen? Accept Redis-holds-improved-while-card-shows-original (Fix 1 closes it anyway)?
7. LOST-label working tree: commit+deploy before or after these fixes?

---

## BUG C — Review sort/visibility (HIGH confidence; code + live-DB verified)

### Ground truth
- **Emilie:** canonical row `137813169274972@lid` = label **CONFIRMED**, importance **0**, dates "Thu May 28, 4–7 PM" (**EVENT PASSED ~9d ago**, customer silent 224h), msg 155. A merged duplicate (`224167731408910@lid`, HOT) is correctly hidden. A stale May-27 inbound makes `_owes_reply=True`.
- **Milenski +4915758869064:** resolves via WAHA to **single** id `25941719961655@lid`, facts-name **"Milena"** (extraction mislabel), pushName "milenski 🫶🏽". label **HOT**, importance **68**, **27 msgs** (operator said 2 — imprecise), yachts "Elan 44, Elise 50" = **900 AED/hr (cheapest tier)**, date **Jun 11 (future)**, merged_into NULL. **Passes every filter — present in v_lead_summary.**

### Root causes
- **C-1 SORT (Emilie on top):** `_awaiting_section_for()` CONFIRMED branch (review.py:132-139) routes any CONFIRMED that owes a reply → top AWAITING_REPLY with **NO passed-event guard** → a dead score-0 expired booking tops the report. **Side-effect of last session's Antonio CONFIRMED→AWAITING fix.** HIGH.
- **C-2 VISIBILITY (Milenski missing):** NOT a filter. **HOT cap `REVIEW_CAP_HOT=10` (review.py:58) + rate-first `_rate_key`=(owes_reply, yacht_max_rate, score) sort (717-723) before `items[:cap]` slice (793)** → cheap-yacht high-intent lead ranks ~24/34 → falls into collapsed "+N more" overflow → invisible in `/review`, visible only via `/review hot`. `_expected_value()` (rate×likelihood×importance, review.py:562) exists but is wired ONLY to the header "Top by value" digest, never the main list = the open **"EV sort"** item. HIGH.
- **C-3 CATCH-ALL GAP (landmine):** `render_review` review.py:654 `if label not in sections: continue` **silently drops** any lead whose label isn't in the sections dict. Box/git-HEAD have NO `LOST` key → **a LOST-labeled lead would vanish.** Not Milenski's cause, but critical for the LOST deploy.

### Contradiction resolved
- Stream C2 wrongly said Milenski "absent at source" (broken query: `LIKE '%869064%'` on an @lid that has no phone digits + blocked from WAHA key). C1+C3 resolved him via WAHA. **Verdict: present, buried — NOT an intake bug.** → Emma/Ayaan may be the SAME burial OR a different cause; their findings decide.

### Fixes (all in review.py; tests: test_awaiting_section.py, test_render_review.py)
- **C-Fix1** (LOW, isolated, high impact): add `not _booking_date_passed(row)` guard to CONFIRMED branch (132-139) → passed-event CONFIRMED stays in CONFIRMED "won"; keep Antonio behavior for **upcoming** bookings. *(Reconcile with Bug D: upcoming = surface with full card; passed = stay won.)*
- **C-Fix2** (LOW, safety net — DO FIRST, de-risks LOST): replace silent `continue` at 654 with a route to a visible bucket → no lead can ever vanish.
- **C-Fix3** (MEDIUM, core EV-sort): replace `-_yacht_max_rate` with `_expected_value(score,row)` in the sort keys + value-aware cap. **Correct composite sort = (1) active-and-owed first [exclude CONFIRMED-passed/NOT_A_CUSTOMER/LOST/DISREGARDED], (2) expected_value desc, (3) longest-waiting.** Note: making AWAITING the global unanswered queue overrides the 2026-06-02 "badge-in-tier" decision → owner call.
- **C-Fix4** (cosmetic): facts-name "Milena"→"milenski".

### Owner decisions (Bug C)
1. Passed-event CONFIRMED + stale inbound → CONFIRMED "won" (recommended) or NO ACTIVE SALE? Keep Antonio behavior for upcoming CONFIRMED (recommend yes)?
2. EV-sort scope: within-section / also cap-selection / global cross-tier queue (c best matches your intent but overrides badge-in-tier decision)?
3. Cap policy: raise/remove cap vs value-aware cap + float owed+high-importance above cap (recommended)?
4. Confirm Milena == Milenski; fix the extraction name?

### Drift
review.py load-bearing lines IDENTICAL across box/local/HEAD (±4 lines from local LOST insert). Author against box copy or reconcile box→git first. **Recommend C-Fix2 (catch-all) before deploying LOST.**

---

## BUG D — Antonio CONFIRMED card (HIGH confidence; WAHA + DB + code verified)

### ⚠️ OPERATIONAL — ACT TODAY (independent of code)
Antonio's **address + viewing-time request is genuinely UNANSWERED right now** (last inbound 2026-06-06 13:34 "tell me what time i can go tomorrow"). An unsent draft **"11am works — Dubai Harbour, Gate 3…"** is already sitting in `draft_log` (id 776/779). **Operator should reply today.** His viewing is **tomorrow Jun 7**, charter Jun 20.

### Ground truth — TWO operator assumptions REFUTED
- **Payment NOT missing:** `payment_received` AED **3,534.3** (autonomous_sends id 188, 2026-05-28); CONFIRMED set by payment webhook. → **Memory note "Antonio payment record missing" is STALE — correct it.**
- **Confirmation WAS sent (partially):** outbound 2026-05-28 19:15 "you're confirmed ✅ Bliss 55 · Jun 20 · 16:30–19:30 📍 Dubai Harbour" — but **NO specific berth/gate/address ever sent** (customer's "Can you share address" unanswered since Jun 5).
- Real Antonio = `31890199318638@lid` (phone +971588211789 via WAHA), CONFIRMED, Bliss 55, ~12 guests, clean (no merges). Visit reason = pre-charter inspection of booked Bliss 55 + finalize food/details.
- **Identity note (non-causal):** a DIFFERENT HOT "Antonio" `971509767187@c.us` holds the brochures/menus; "Qurbani" `274942918680787@lid` is wrongly merged into THAT one (open Qurbani un-merge). Real Antonio unaffected.

### Root causes (all `review.py`, same subsystem as Bug C)
- **D-1 (CONFIRMED detail skipped):** `render_review` gates the "✅ booked & paid — yacht·date·party·💰paid" block on `label_key=="CONFIRMED"` (review.py ~820), but `_awaiting_section_for` routes a CONFIRMED+owed lead into AWAITING_REPLY → `label_key` != CONFIRMED → block skipped → falls to generic "🧠 Hermes 88/100 · last read:…". Paid amount IS computed (server.py:3303-3311) but only consumed in the CONFIRMED branch. HIGH.
- **D-2 (no confirmation/berth-sent tracking):** zero schema field/autonomous_sends-kind for berth/confirmation-sent → /review can't warn. Greenfield. HIGH.
- **D-3 (visit reason not surfaced):** reason lives in `importance_reasoning` + dates "viewing" clause; never rendered (only `suggested_action` shows). HIGH.

### Fixes (reconcile with Bug C — same file/branch)
- **D-Fix1** (MEDIUM): extract `_confirmed_detail_bits(row)` helper; gate on `row['label']=="CONFIRMED"` not `label_key`; call from CONFIRMED **and** AWAITING branches → confirmed+owed lead shows ✅ badge + paid amount + booking/viewing time **alongside** the 🔴 needs-reply badge. *(Complements C-Fix1: passed-event CONFIRMED → stays "won"; upcoming CONFIRMED+owed → full card in AWAITING.)*
- **D-Fix2** (LOW): surface viewing date/time (stop `_safe_display_date` collapsing it) + visit reason in the helper.
- **D-Fix3** (HIGH dependency — do last): new `autonomous_sends` kind `confirmation_sent`/`berth_sent` logged at send-time + subquery → `row['confirmation_sent_at']` → render `⚠️ confirmation/berth not sent` when CONFIRMED+paid+not-sent.
- Tests: `test_review_owe_and_close.py` / `test_awaiting_section.py` — owed CONFIRMED row asserts "✅", "AED 3534.3", date, 🔴, ⚠️.

### Owner decisions (Bug D)
1. Show BOTH ✅ booked&paid AND 🔴 needs-reply (recommend yes)?
2. ⚠️ berth threshold: require specific berth/gate, or does "Dubai Harbour" clear it?
3. Confirmation-sent source: new autonomous_sends kind at send-time (recommended) vs WAHA-derive (unreliable)?
4. Auto-draft/send the berth message ("Gate 3" draft exists) vs operator-confirm?
5. AED 700 extension upsell at viewing — surface? auto vs confirm?
6. Merge the two Antonio identities + un-merge Qurbani?

---

## BUG H — Ayaan Nadeem NEVER INGESTED 🚨 HIGHEST SEVERITY (HIGH confidence)

**Ayaan +971589954694 has ZERO footprint in every layer** — WAHA gateway logs (0), n8n logs (0), all 13 Postgres tables (0 rows), 38,830 bridge journald lines (0), Redis drafts (0). Searched all phone + name variants. Positive controls (Emma, Milena) DID appear → search method works.

- **Classification: NEVER INGESTED — intake/WAHA-level failure.** His inbound never entered the pipeline → no lead created → no draft/outbound/`/review` row. **This is DIFFERENT from Milenski/Emma** (who exist but are buried/dropped at render). **Fixing the display bug would NOT surface Ayaan.**
- **Why "completely missed":** a message that arrives but fails to create a lead leaves **ZERO trace today** — no dead-letter, no audit. So we can't even *count* how many Ayaans there have been. **This is potentially many silently-lost bookings = the highest-severity issue in this whole batch.**

### Root cause → NEW Subsystem 6: Intake/ingestion (WhatsApp → WAHA → n8n webhook → bridge)
Not in `review.py`/`server.py` display code. Candidates (need confirmation):
1. **WAHA session continuity** — container restarted 2026-06-01 17:54 UTC; a disconnect/outage window silently drops inbounds (tie to existing WAHA session watchdog).
2. **n8n intake workflow** (`/webhook/whatsapp`) filters (group/broadcast/contact gating) discarding his message before the bridge.
3. **No dead-letter / unhandled-inbound audit** — add intake-level logging so drops become visible (this is the systemic fix).
- LID-masking ruled out (even a masked @lid would've created a row).
- Confidence: HIGH no lead ever created; MEDIUM on exact failure point (WAHA logs only retain back to Jun 1; if he messaged earlier, can't pin WAHA-side — but persistent stores confirm no lead regardless of date).

### Side-finding (pre-answers part of Bug E)
**Emma `197251238523069@lid` EXISTS** — HOT, score 88, 11 msgs, not merged, IN v_lead_summary, with drafts + bridge/WAHA activity. So Emma is a **display/burial** case (like Milenski), **NOT** an intake failure. (E/F workflow will give the exact render mechanism + overtexting evidence + greeting issue.)

### Owner decisions (Bug H)
1. Authorize a deeper read-only intake audit (WAHA session history, n8n webhook filters) to pin the exact drop point?
2. Add an intake dead-letter/audit log so future silent drops are visible + alertable? (Strongly recommended.)
3. **Operational: Ayaan is a real lost lead right now** — do you want to manually reach out to +971589954694? (We have no message from him in-system to reply to.)

---

## BUG G — Reengage line almost never used (HIGH confidence; code + live-DB verified)

### Ground truth
- Line is a hardcoded constant `GHOST_RECOVERY_PHRASES` (server.py:314-322): `"Have you given up on booking a private yacht?"` mapped to windows `hot_30m_2h` + `cold_lastshot`. Engine = n8n hourly sweep → `/hourly-sweep` → `scan_followup_eligibility()` → `/draft-followup` → **operator-approval card** (already propose/approve, not auto-send).
- **Prod (14 days):** the "given up" line proposed ~3×, sent **1–2×** total. `warm_24h_72h` ("just checking in…") is the workhorse (7 drafted/4 sent). Quantitatively confirms "almost never."
- Current silent pool sits mostly in dead zones: HOT 31 (avg 126h), WARM 9 (avg 186h), NEW 8 (avg 67h), COLD 2.

### Root cause — engine targets the WRONG population (+ 3 compounding gates)
- **G-1 SEMANTIC INVERSION (keystone):** candidate SQL (server.py:3134-3135) requires `last_operator_reply_at IS NULL OR < last_customer_message_at` → only leads where the **customer spoke last and we owe a reply**. The reengage population is the **opposite** (we replied, they ghosted). Every operator reply sets `last_operator_reply_at=now()` → permanently excludes them. The `we_replied` flag is computed (server.py:3118) and **never used (dead code)**. HIGH.
- **G-2 cold_lastshot near-unreachable:** needs COLD + 72-168h, but SQL caps <168h while cold-decay only fires >168h (labels.py:1099). Razor-thin. HIGH.
- **G-3 dead-zone bands:** `_silence_window_for` (server.py:3077-3091) leaves NEW never eligible, HOT >24h, WARM <24h/>72h, COLD <72h uncovered.
- **G-4 hard one-shot:** `already_drafted` gate (server.py:3144-3145) blocks any 2nd nudge → makes `FOLLOWUP_CAP=2` unreachable. Manual `/review [Draft nudge]` passes no `silence_window` → never produces the verified phrase.

### 🔑 RECONCILES Bug E (overtexting) ↔ Bug G (underused) — SAME subsystem 5
Not contradictory: both = the engine targeting the wrong set. Fix = **fix targeting/reachability, KEEP the anti-spam caps.** Proposed cadence (all PROPOSE/operator-approve, never auto-send):
| Step | Trigger (silence since OUR reply) | Phrasing |
|---|---|---|
| Nudge #1 (soft) | ≥24h, HOT/NEEDS_ATTENTION/WARM | "Just checking in…" |
| Nudge #2 (the line / last shot) | #1 unanswered AND ≥48-72h cooldown AND silence 3-14d | "Have you given up on booking a private yacht?" |
- Max 2 nudges/cycle (keep `FOLLOWUP_CAP=2`), cooldown ≥48h (convert one-shot→time-gate), then lifecycle: 2 nudges → 7d silent → cold-decay → dormancy auto-close → analyzer_close → **LOST** (already suppressed from engine). Closed loop, no overtexting.

### Fixes (server.py; tests: test_reengage.py, test_cold_decay.py, test_followup_note.py)
- **G-Fix1 (keystone):** add 2nd eligibility branch in `scan_followup_eligibility` for "we replied → customer silent" (measure silence from our reply; use the dead `we_replied` flag). Run through ALL existing caps.
- **G-Fix2:** convert `already_drafted` one-shot (3144-3145) → 48h cooldown, bounded by cap.
- **G-Fix3:** reconcile cold_lastshot — (a) widen COLD band + SQL upper bound (recommended) vs (b) lower cold-decay threshold (wider blast radius).
- **G-Fix4 (optional):** wire `/review [Draft nudge]` to pass silence_window + give `_we_last` a cooldown escape hatch. **Overlaps Bug B** (analyzer RULE 8 server.py:2219-2225 vs anti-pushiness 2249-2254 contradict — align both to cooldown).
- Recommended minimal scope: **G-Fix1 + G-Fix2 + G-Fix3(a).**

### Owner decisions (Bug G)
1. Confirm reengage targets "we replied → customer silent" set?
2. Cadence numbers: first-nudge 24h, last-shot band 3-14d, cooldown 48-72h, max nudges (keep 2?), stop ceiling 7d?
3. PROPOSE/approve only (recommended)?
4. cold_lastshot fix (a) vs (b)?
5. Wording variants + append name?
6. Channel: engine-only (recommended) vs also un-suppress RULE-8 on /review cards?

---

## BUG B — Marc scored HOT 90 despite declining + wanting bareboat (HIGH confidence; WAHA + DB + code verified)

### Ground truth (verbatim chat — both operator claims CONFIRMED)
- Bareboat asked **3×**: #01 "boat to rent without boat driver licence?", #11 "No boat driver license required?", #12 "Self driving". Dubriani #14: "all our charters are crewed — a captain is included" → **service-fit mismatch** (we can't supply bareboat).
- **Explicit decline:** #16 11:53 "No thanks" (direct reply to #15 "Bliss 55 for today or tomorrow?").
- Facts correct (Bliss 55, today, 2 guests) but **score+recommendation wrong**: importance 90 / HOT / "quote smallest yacht today" was computed **06:46 — BEFORE** the bareboat insistence + decline. `customer_triggers`=0 rows (rejection never recorded).

### Root causes (Subsystem 2 — analysis engine)
- **B-i Rejection not detected:** "No thanks" absent from BOTH the LLM Rule 4 lexicon (server.py:2189) and the deterministic regex (labels.py:1136-1139, matches "not interested|…|never mind" only). HIGH.
- **B-ii Recency over-weighted:** Rule 6 "DROP EVERYTHING → 85-100 … date urgency trumps almost everything" (server.py:2205) hard-caps any same-day+engaged lead regardless of sentiment. HIGH.
- **B-iii No service-mismatch disqualifier:** bareboat/no-captain/licence absent from the rubric (server.py:2137-2264). HIGH.
- **B-iv No rejection→graceful-close pathway:** close→demote (routes.py:3474) only on verdict=close/score==0 (it returned 90); demotes to COLD not LOST; LOST is operator-only (labels.py:33-37); `_close_bucket` (labels.py:421-442) has no decline/mismatch reason; no goodbye draft. HIGH.
- **Staleness (inconclusive — needs diagnostic):** scorer didn't re-run after #16. "No thanks" fails `_facts_extract_gate` (review.py:242) → no `_enqueue_reanalyze` (routes.py:724). AND the hourly `/pipeline-analyze` cron empirically did NOT re-score Marc either → **stale 90s will persist even after a rubric fix until this is resolved.** The label engine DID run 12:00 but the **sticky-upward guard (routes.py:1391)** kept HOT (sig `sticky_hot`).

### Fixes (ordered)
- **B-Fix0 (diagnostic, DO FIRST):** confirm whether `/pipeline-analyze` cron re-scores active HOT leads — gates everything (if scorer never re-runs, no rubric change takes effect).
- **B-Fix1:** let decline/sentiment keywords pass `_facts_extract_gate` → enqueue re-score on a decline.
- **B-Fix2:** card stops printing "push toward booking" + drops same-day urgency bonus when last inbound is a decline/mismatch (review.py `_why_line`/`score_lead`).
- **B-Fix3 (rubric, the decision-maker):** add SERVICE-MISMATCH rule ABOVE Rule 6 → verdict=close/low-cap; broaden Rule 4 to polite declines; Rule 6 must NOT override rejection/mismatch; on close set `suggested_action`="draft graceful goodbye + recommend closing". MEDIUM (LLM behavior; mitigate low-cap+confirm). No unit test today → add golden fixture.
- **B-Fix4 (deterministic backstop):** add polite declines + bareboat to `_deterministic_break` regex + add to `_HARD_DEMOTE_SIGNALS` so it bypasses sticky-upward and demotes. MEDIUM (false-positive risk e.g. "no thanks, what about Saturday?" → require standalone reply to a booking offer).
- **B-Fix5:** route closed declines to 💔 LOST (new sub-reason) not just COLD — first auto-write to LOST. *(Ties to queued LOST deploy + Bug G lifecycle.)*
- **B-Fix6:** farewell-nudge branch in `handle_draft_followup` (graceful goodbye draft). *(Delivers your "detect No thanks → goodbye → close" via Fix1→3→5→6.)*

### ⚠️ Conflict to reconcile
This makes `auto:analyzer_close` fire **MORE** for genuine declines — the **inverse** of the open *"auto:analyzer_close rein-in"* item. Must reconcile, not implement blind. (Marc shows it currently fires too *rarely* for real declines.)

### Owner decisions (Bug B)
1. Does Dubriani offer ANY bareboat/self-drive? (All evidence = crewed-only.)
2. Soft "No thanks": auto-demote vs operator-confirm first?
3. Service-mismatch: hard close (→disappears to LOST) vs low cap (~10-15, stays visible to send goodbye)?
4. Auto-draft goodbye (Pillar C) vs [Draft nudge] button?
5. Declined lead lands in LOST / new "service-mismatch" sub-reason / NOT_A_CUSTOMER? (LOST auto-write OK?)
6. Approve graceful-goodbye wording.
7. Reconcile with "auto:analyzer_close rein-in."

---

## BUG E/F — Emma missing + overtexting + greeting/name (HIGH confidence; WAHA + DB + live-code replication)

### E — visibility (SAME class as Bug C, confirmed by live replication)
- Emma `197251238523069@lid` — HOT, importance **88**, merged_into NULL, present in v_lead_summary. **No hard filter drops her.** Burial = `REVIEW_CAP_HOT=10` + rate-first `_rate_key` sort → **rank #16 of 32 HOT** → hidden "+22 more" overflow. Her Satoshi-rate (3000) loses to 9 higher-rate whales.
- 🚨 **NEW: `/review hot` re-applies cap=10 too** (routes.py:1541-1542,1605 pass `filter_label` only to the SQL, not to `render_review`) → the **"+N more — /review hot to see all" hint is BROKEN**; no /review view surfaces her. Same for the "Top by value" header digest (top-8 by EV) = the "not in Hermes analysis" symptom.
- Score depressed by overtexting side-effects: we replied/nudged AFTER her last msg → no longer "owes a reply" (forfeits top sort key) + the nudge applies **-100 damping** in score_lead.
- Identity: only `@lid` row; WAHA maps to `8618268872537@c.us` → **phone search +8618268872537 finds nothing.**

### E — overtexting = MODE (i) bubble fan-out (NOT multi-nudge)
- 17 outbound vs 6-9 inbound; consecutive-out runs [2,4,2,3,2,4]; terminal 4-run after price objection "It's expensive than others."
- **Mode (ii) nudge guards HELD** (1 nudge, FOLLOWUP_CAP=2, anti-pushiness server.py:3144-3146 working). The problem is **one reply fragmented into 2-4 bubbles** — and there is **NO bubble-count cap anywhere**: STYLE_DIRECTIVE #4/#6 (server.py:1101-1139) actively order multi-bubble ("OVERRIDES default to one message"; "EACH option in its OWN bubble"); scorer FORMATTING (server.py:2653-2661) is a one-sided ratchet (penalizes cramming, no `too_many_bubbles`); `sanitize_draft_messages` (server.py:3369-3410) never caps `len(messages)`. **Ties directly to Bug A-S3.**

### F — options before NAME (REFRAMED: bot DID greet first)
- Defect is **options before asking the name + no stage gate**, not before greeting. Roots: (1) base prompt pitches UNCONDITIONALLY (system-prompt.md §6:301 "Always send 3 yacht options"; §1:117 "Recommend 3 specific yachts immediately") — counter-rules buried in §5:280-284; (2) **no conversation-stage logic** in any layer (prompt / behavioral_context / _lead_state_block); (3) personalization wired to **WhatsApp profile name** (`Format Context` `customerName = wh.notifyName||wh.pushName`), not the asked name → addresses her by a name she never gave + used it in the first nudge line (violates Hard Rule 10).

### Fixes (coordinate: visibility w/ Bug C; bubble cap w/ Bug A-S3)
- **E-Fix-vis:** route `filter_label` through to `render_review` so `/review hot` uncaps/paginates; add an importance floor (HOT + imp≥80 + active thread guaranteed a slot). *(Merge with C-Fix3 EV-sort — ONE cap/sort policy.)*
- **E-Fix-bubble:** (a) hard `len(messages)` ceiling (~3-4) in `sanitize_draft_messages` (deterministic, robust); (b) STYLE #4/#6 upper bound; (c) scorer `too_many_bubbles` flag. *(ONE coordinated change with A-S3.)*
- **E-Fix-identity:** link `@c.us` phone to the `@lid` row (write op — merge-safety review).
- **F-Fix:** add FIRST-CONTACT/STAGE-GATE directive in `behavioral_context` (runtime, high salience) + STAGE signal in `_lead_state_block` + gate the unconditional pitch in n8n Build Prompt (+siblings) + switch name source to `customer_facts.name` (profile name = fallback).

### Owner decisions (Bug E/F)
1. `/review hot` uncap/paginate vs importance floor (pick ONE with Bug C)?
2. HOT+imp≥80 bypass rate-first sort/cap?
3. Bubble ceiling value (rec 3-4; inclusions=1 bubble, options=1/yacht)?
4. First-contact: hard-block options on first reply vs require greet+name then allow after one exchange?
5. Switch to in-chat name + demote profile name to fallback + "no name in first line"?
6. Link @c.us↔@lid identity (write)?

---

## BUG I — Xeno: contradiction + catering fabrication + 🔒 LEAK (HIGH confidence; WAHA+DB verified)
Xeno Accounts +971506798997 = `104711420166164@lid`, WARM, importance 95.
- **I-1 🔒 LEAK — CONFIRMED REACHED CUSTOMER (CRITICAL):** on 2026-06-05 the customer received **twice** (20:02:21, 20:03:24) the verbatim internal note: *"Premium BBQ menu — sending alongside fine dining per operator rule 23 (Zayn to also send catering-fine-dining manually)"* (WAHA fromMe=true; autonomous_sends id 254/255, kind=file_sent_link, fallback_used=true). Internal name **"Zayn"** + internal instruction genuinely sent. Prohibiting rule = DB behavior_rules **id 83** ("Never mention Zayn by name"). **Leak path = a FILE-SEND CAPTION on a sendFile that fell back to sendText → bypasses `sanitize_draft_messages()` entirely** (which only scrubs the messages text list for payment URLs/placeholders, never captions/internal-notes/names).
- **I-2 contradiction — REAL but NOT sent:** the AED **2,500** quote WAS sent (06-05 13:54). On 06-06 the drafter produced TWO inconsistent drafts for the discount ask — id 772 correct ("2,500 AED covers the chef") vs id 784 wrong ("375 AED per person") → **no commitment memory.** Draft 784 was HELD (would_hold, score 6) → did NOT reach customer (caught by approval hold).
- **I-3 "375" = recurring hallucination:** no "375" anywhere in ground truth; system-prompt.md:452-456 has the catering block. **Identical "invented 375 fine-dining price" incident occurred 2026-06-01** (server.py:1029/1048-1049, NO_INVENT added) — recurred → confirms soft prompt guard insufficient, **need deterministic validator** (= A-S2). Cited "Rule 37" is a **hallucinated citation** (DB rule 37 = "refer to customer as her/she" for a DIFFERENT customer).
- ⚠️ **CATERING PRICE CONTRADICTION (owner must resolve):** operator says Fine dining **2,500/person** + Premium BBQ **1,500/2pax**; prompt says Fine dining **2,500 for 2** + Premium BBQ **2,500 for 2 (min 1,500)**. These disagree — authoritative numbers needed before any fix.
- **I-4 analysis blind (partial):** suggested_action notes the discount ask but doesn't anchor to the 2,500 already quoted.

### Fixes (Bug I)
- **I-Fix-leak (FIRST hotfix):** scrub internal notes/names/bracketed instructions/rule-citations on EVERY outbound path — **especially the file-send caption / sendText-fallback path** that bypasses the current scrub. CRITICAL.
- **I-Fix-catering:** put authoritative catering prices in the single canonical config + the deterministic validator (shared with A-S2); fix system-prompt.md catering block to match operator truth.
- **I-Fix-memory:** prior outbound/quotes reach drafter+analyzer + "never contradict a prior quote" (shared with A drafter + B analysis).

## BUG J — Two stacked drafts for Xeno (#9+#10) (HIGH confidence; code+Redis+journald verified)
- **Debounce works as built, window too short:** 2-tier per-customer Redis debounce (latest-token-wins), `handle_debounce` routes.py:143-219; window = **5s if booking-signal else 15s**, computed per-message, **never extended**. #9 ("Premium deluxe menu"/"for 2") and #10 ("2 jet skis") were **~45s apart** (#9 flushed 13:44:08, #10 arrived 13:44:38) → #9 already drained+drafted before #10 → two independent drafts. Neither had a booking-signal token → both 15s tier.
- **Supersede DOES fire on inbound path** (`_draft_save`→`_supersede_pending_for_cid` server.py:396-450): #10 superseded #9, struck-through card 6746 — BUT only strikes-through (card stays visible) and **does NOT consolidate** #9's unanswered catering question into #10's reply (STALE_RISK logged AFTER the LLM drafted #10 without it). Operator then regenerated the struck card (content-only regen bypasses the revival guard server.py:644-663) and hand-merged manually.

### Fixes (Bug J)
- **J-Fix1 (primary):** make debounce a true **sliding window** (reset quiet-timer on each inbound for the cid) and/or lengthen non-signal tier 15s→45-60s.
- **J-Fix2:** feed the still-open prior pending draft + earlier unanswered message into the draft-build so the SURVIVING card answers BOTH questions (true consolidation).
- **J-Fix3:** block/​warn on content-regen reviving a superseded draft.
- Tests: test_dedup_leads, test_queue, test_claim_send.

## BUG K/L — Customer D (+919600080831 / 28149249241314@lid): drafting fact-confusion + won't close (cluster, subsystems 2+3)
Maps to known root causes (F, I, A-grounding) + a NEW "assume-sale" behavior. Not separately deep-investigated — symptoms match confirmed mechanisms.
- **K-1 name:** never asked the customer's name (knows only "D" from WhatsApp profile) = **Bug F** (no stage-gate to ask name; profile-name only).
- **K-2 won't lead to close:** all params known (yacht, Jun 22, 4:30pm, **3 hrs**, 3 guests, BYO ok) but drafter re-asks "how many hours?" and won't assume the sale / send payment link. Needs: drafter must USE extracted facts (stop re-asking) + a **NEW "ready-to-close → summarize date/yacht/time + send payment link with computed total" progression.**
- **K-3 re-asks known facts:** draft "the date is Jun 22 — how many hours?" (3/10) asks for duration already given (3).
- **L wrong yacht:** customer wants **Von Dutch 40** but draft says **Bliss 55** (AED 4,200 = 3×1,400 — rate coincidentally same, but yacht NAME wrong) → commitment/fact confusion = grounding/memory gap (**A-S2 / I-memory**).
- Compounding: the missing name worsens all of it.
- **Fixes:** Phase 3 (F stage-gate + name; I commitment-memory; drafter use-known-facts/right-yacht) + a NEW assume-sale→payment-link behavior (analysis flags ready-to-close; drafter summarizes + sends link). 
- **⚠️ OPERATIONAL NOW:** operator can manually send Customer D the **Von Dutch 40** payment link **3×1,400 = AED 4,200** for Jun 22, 4:30pm, 3 guests (BYO ok).
## BUG G — Reengage line underused — ⏳ pending
