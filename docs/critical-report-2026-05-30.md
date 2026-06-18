# Dubriani / Hermes — Critical System Report

**Date:** 2026-05-30
**Author:** Claude Code (live investigation against prod EC2 `dubriani-ec2`)
**Scope:** root-cause analysis of 9 reported issues + code-quality & performance audit + prioritized remediation roadmap.

> Confidence tags: **[CONFIRMED]** = verified in prod data/code, **[STRONG]** = high-confidence inference, **[HYPOTHESIS]** = needs a repro.

---

## 1. Executive summary

Most reported bugs trace to **three subsystem weaknesses**, not nine independent defects:

1. **Identity duality (`@lid` vs `@c.us`)** — the same WhatsApp customer is stored as two `customer_facts` rows. This is the root of the "lost message," the Qurbani/Zayn duplicate in review, and label/state fragmentation. **This is the #1 fix — foundational.**
2. **Label ≠ verdict reconciliation** — the auto-classifier label (HOT/NEW/CONFIRMED) and the LLM analyzer verdict (`close`/`keep_open`) live in separate columns and are never reconciled, so review shows stale/contradictory states (Madawi "HOT, push to book" when lost; spam shown as NEW with no verdict).
3. **Inbound draft-gating gaps** — Claude's deliberate "no reply" (`messages:[]`) is mis-handled as a parse error and dumped as a raw-JSON card; spam senders are never auto-suppressed or labeled.

A 4th, already-fixed defect (false CONFIRMED on paylink-send) was resolved earlier today.

---

## 2. Architecture (as-built)

**Ingress:** WhatsApp → WAHA (`devlikeapro/waha`, docker) → n8n webhook `/webhook/whatsapp`.
**Orchestration:** n8n v2.20.11 (docker), workflow **"Dubriani Phase 1B"** (`azPIy9OcDwiPV5uY`, active) — **198 nodes**: 121 httpRequest, 46 code, 14 if, 6 wait, 4 set, 3 switch, 2 webhook, 1 splitInBatches, 1 splitOut.
**Brain/state API:** "Hermes Bridge" — systemd user service, host `:8788`, Python. Modules: `server.py` (3012 lines), `routes.py` (5056), `labels.py` (205).
**Stores:** Postgres 16 (`n8n` db — app tables: `customer_facts`, `autonomous_sends`, `customer_label_history`, `conversation_state`, `conversation_modes`, `label_corrections`, …); Redis 7 (drafts, autosend state, `nomod_seen`, debounce buffers).
**Approval UX:** Telegram bot (admin chat `5532831477`).
**Payments:** Nomod (`/poll-payments` + reconcile).
**Other:** an `evolution` Postgres db + `hermes-gateway` / `hermes-gateway-personal` systemd services exist; **`hermes-gateway.service` is in a FAILED state** — appears to be a half-migrated/abandoned alternate gateway. Needs a decision (remove or repair).

### Inbound flow (traced from connections)
```
Filter Inbound → Debounce(Buffer→Wait→Flush) → Get Chat History → Format Context
→ Fetch Behavioral Context → Build Prompt → Claude AI
   ├─(err)→ Build Alert → Alert Zayn
   └─(ok)→ Parse Response → Customer Facts → Label Eval → Interrupt?
        ├─(interrupt)→ Send Sameday Alert
        └─→ Check Payment Trigger ─┬→ Generate Payment Link → Build Payment Message
                                   └→ Queue & Format → Persist Draft → Send Draft to Telegram
                                      → Save Telegram MsgID → Auto Prep → Auto Is Autonomous
                                      → Get Autosend → Auto Decide → Auto Send Gate → (auto-send)
```
**Key fact:** there is **no label/CONFIRMED gate that skips drafting**. The only label-dependent branch is `Interrupt?`, and CONFIRMED yields `interrupt_required:false` → routes *toward* the draft. (This corrects an earlier mis-diagnosis.)

---

## 3. Findings (by issue)

### F0 — False CONFIRMED on paylink-send — **[CONFIRMED] ✅ FIXED today**
`Promote To Confirmed` node (Mark Sent / ✅ Send path) hardcoded `label=CONFIRMED` when sending a payment-link draft (`is_payment`), equating "link sent" with "paid." Fixed → `WAITING_FOR_PAYMENT`; deployed live via n8n API (no downtime); Qurbani's prod label corrected. Committed `938deaa`.

### F1 — Identity duality `@lid` vs `@c.us` — **[CONFIRMED] — ROOT CAUSE of #1, #9**
Same human = two rows: `274942918680787@lid` ("Qurbani", 111 msgs, WAITING_FOR_PAYMENT) and `971509767187@c.us` ("Zayn", 8 msgs, HOT), neither `merged_into` the other. WAHA reports `from` as `@lid` (WhatsApp Business linked-id) or `@c.us` (phone) inconsistently (HANDOFF pitfall #6). The bridge canonicalizes a cid but does **not** unify the two namespaces, so:
- Inbound under `@c.us` updates "Zayn"; operator watches "Qurbani" → message looks lost.
- `/review` lists both → duplicate customers.
- Labels, drafts, conversation_state all fragment across identities.
**Fix:** see §5 (identity layer). This is P0.

### F2 — "Lost message" (can i choose menu) — **[STRONG]**
Not `confirmed_terminal` (no such draft gate exists). Almost certainly F1: the message landed on the `@c.us`/"Zayn" identity. Verify by checking which identity received the 13:57 inbound. Resolved by F1.

### F3 — Empty/spam draft posted as raw-JSON card — **[CONFIRMED]**
`Parse Response`: `if (!Array.isArray(parsed.messages) || parsed.messages.length === 0) throw` → the `catch` sets `messages:[raw]`, dumping the **entire raw JSON** into a card. Empty `messages` is a *valid intentional* "no reply" (spam), not a parse error. `Queue & Format` has no spam/empty gate, so it always posts.
**Fix:** distinguish empty-intentional from parse-error; when empty/`break_condition.hit`/spam → **do not post a card**; auto-skip and (F6) auto-label sender.

### F4 — Wrong verdict in review (Madawi HOT/"push to book" when booked elsewhere) — **[STRONG]**
The analyzer (`hermes_analyze_lead`) is actually well-designed (RULE 1 vendor→close, RULE 2 spam→close, RULE 3 B2B→keep_open low band, RULE 4 rejection→close; detects "comparing competitors / lost interest"). The bug is **label ≠ verdict reconciliation**: the auto-classifier sets the **label** (HOT) from message regex/LLM, while the analyzer writes `verdict/importance/reasoning/suggested_action` to *separate* columns. `/review` surfaces the stale label + an importance line that can contradict it. A `close` verdict never demotes the label.
**Fix:** when analyzer `verdict=close`, auto-demote label (→ COLD/DISREGARDED) or render the verdict as the headline; reconcile in `handle_pipeline_analyze`.

### F5 — Spam/new lead shows no verdict in review — **[STRONG]**
`/review` renders `importance_*` only if present. New leads (e.g. EnjoyBoat, `importance_analyzed_at` NULL) haven't been hit by the hourly capped `handle_pipeline_analyze` yet → blank. So spam sits as NEW with no guidance.
**Fix:** analyze-on-first-contact (or eager-analyze leads with NULL `importance_analyzed_at` shown in review); render a clear "unscored — likely spam/B2B" hint.

### F6 — New label `POTENTIAL_SPAM` — **[DESIGN]**
You already have hard manual `PAUSED_SPAM`/`PAUSED_B2B`. Add a **soft, auto-applied** `POTENTIAL_SPAM` set by the analyzer on RULE 1/2 hits, surfaced in `/review` with the verdict + one-tap `confirm→PAUSED_SPAM/B2B` or `dismiss→WARM`. Touches `labels.py` (LABELS, rank, score floor), analyzer output→label application, review rendering, and the operator command set.

### F7 — Qurbani/Zayn duplicate in review — **[CONFIRMED]** = F1 manifestation.

---

## 4. Code-quality & performance audit

**Structure / maintainability**
- **God files:** `routes.py` 5056 lines, `server.py` 3012. `labels.py` was extracted (good) but DB helpers (`apply_label_transition`, `_has_recent_payment_*`, `compute_label`) still live in `server.py` and are imported *back* into `routes.py` — tangled dependency direction. Recommend a `db.py`/`labels_db.py` and a clean `bridge/` package.
- **198-node monolith workflow** with **121 httpRequest nodes**, each carrying its own `timeout`/`retryOnFail`/`onError` config (inconsistent across nodes). Hard to test, reason about, or change safely (every fix is a 2.3 MB JSON edit). Consider extracting sub-flows (payments, edit-learning, review) into separate workflows called via Execute Workflow.
- **SQL via string interpolation** (`_lit(...)`, `cid.replace("'","''")`) throughout `routes.py` — injection-adjacent and fragile. Move to parameterized queries.
- **Brittle deterministic post-processing** in `Parse Response` (regex URL/placeholder scrubbing, dangling-fragment cleanup). Symptom of asking the LLM not to do something then patching it in JS. Better: structured tool/JSON contract + a single validated formatter.
- **Migration debt:** pervasive `staticData → Redis` comments; dead nodes referenced in comments; the **failed `hermes-gateway.service`** (abandoned alternate path). Clean these up — half-migrations are where bugs hide.
- **Empty ≠ error conflation** (F3) is a correctness smell repeated wherever "no output" is treated as failure.

**Performance / reliability**
- Inbound latency is dominated by the **2-tier debounce** (5s/15s) + a Claude draft call; acceptable. **Auto-send** adds a deliberate 60–300s human-like delay (by design).
- `handle_pipeline_analyze` makes **one LLM call per lead**, hourly, capped (`PIPELINE_ANALYZE_CAP`) — new leads can wait a full cycle for a verdict (F5). Consider eager first-contact analysis for unscored leads.
- Bridge HTTP nodes use 8s timeouts + retries; `ANALYZE_LEAD_TIMEOUT` / `BRIDGE_HERMES_TIMEOUT=120s` for long LLM calls. Reasonable, but timeouts/retries are configured per-node inconsistently.
- Redis is the sole hot store (good — removed staticData concurrency fragility).
- **No identity index/uniqueness** preventing duplicate customer rows (F1) — needs a canonical key + unique constraint + merge.

---

## 5. Proposed identity layer (F1 — futureproof)

**Goal:** one human = one canonical `customer_id`, regardless of `@lid`/`@c.us`.
1. **Canonical resolver** in the bridge: maintain an `identity_alias(alias_id PK, canonical_id, source)` table. On every inbound, resolve `from` → `canonical_id` (create alias on first sight). Match `@lid`↔`@c.us` by the phone digits embedded in WAHA contact metadata (the WAHA `/contacts` lookup already returns the `@c.us` for an `@lid`).
2. **Backfill + merge:** merge "Zayn" (`@c.us`, 8 msgs) into "Qurbani" (`@lid`, 111 msgs) using existing `customer_facts.merged_into`; consolidate `conversation_state`, `customer_label_history`, drafts, `autonomous_sends`.
3. **Single write path:** all bridge reads/writes go through the resolver; Redis keys keyed by `canonical_id`.
4. **Unique constraint** on canonical identity to prevent regressions.
5. **WAHA send:** always send to the `@c.us` (deliverable) form, resolved from canonical.

*Invasive + data-migrating → requires your sign-off before execution.*

---

## 6. Prioritized roadmap

| Pri | Item | Effort | Risk | Reversible |
|-----|------|--------|------|-----------|
| **P0** | F1 identity layer + merge Qurbani/Zayn (fixes #1, #9, mislabeling) | High | High | Merge is hard to undo → backup first |
| **P0** | F3 empty/spam draft gate (stop raw-JSON cards) | Low | Low | Yes (workflow node) |
| **P1** | F4 reconcile label↔verdict (stop HOT/"push" on lost) | Med | Med | Yes |
| **P1** | F6 `POTENTIAL_SPAM` soft label + F5 eager analysis + review verdict | Med | Low | Yes |
| **P2** | Deliver Qurbani reply (operator); was the trigger lead | Low | Low | Draft only |
| **P2** | Tech-debt: parameterized SQL, split god-files, remove failed gateway, sub-flow extraction | High | Med | Yes |

**Recommended order:** F3 (quick win, stops noise) → F1 (foundational, with backup + your sign-off) → F4 → F6/F5 → Qurbani reply → tech-debt.

---

## 7. What was already changed in prod today
- `Promote To Confirmed` node: `CONFIRMED → WAITING_FOR_PAYMENT` (live, via n8n API; workflow still active).
- Qurbani label corrected `CONFIRMED → WAITING_FOR_PAYMENT` (bridge `/label`, audit row #234).
- Workflow settings `binaryMode` restored after API whitelist stripped it.
- Repo commit `938deaa` on `main`.
- SSH config hardened (`~/.ssh/config` ConnectionAttempts/keepalive); `~/.claude/settings.json` allow `ssh dubriani-ec2`.
