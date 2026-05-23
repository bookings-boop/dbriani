# 2026-05-23 evening — fixes shipped

Live deploys after the pre-launch audit. All four bugs were operator-visible.

## 1. Payment URL hallucination — `686556d`

**Bug:** customer received `pay.nomodapp.com` as a bare, unclickable placeholder. Hermes ignored §19's anti-URL clause and wrote the URL itself instead of letting Build Payment Message append the real `/en/l/<id>` link.

**Fix (two layers):**
- `workflows/phase-1b-telegram.json` Parse Response now scrubs `pay.nomodapp.com`, `https://pay.nomodapp.com`, `[link]`, `[payment link]`, `[insert link]` from every draft message before Queue & Format / Build Payment Message / autosend see it. Tidies trailing artifacts; falls back to CTA when the result would be empty.
- `hermes-bridge/server.py` new `sanitize_draft_messages()` + integrated into `_draft_followup` for the nudge/follow-up draft path.

**Verified:** 9-case unit test, including the exact operator-quoted text.

## 2. Autosend double-message race — `f0e206f`

**Bug:** draft created for msg1 → operator hit AUTO (Auto Wait ~30s) → customer sent msg2 mid-wait → second draft created → Auto Wait elapsed and sent the stale msg1 reply, then operator sent the msg2 reply → customer got two replies back-to-back.

**Fix:**
- Bridge: new action `POST /autosend-state action=disarm_by_customer customer_id=…` — SCAN `autosend:*`, GET each, filter by `customer_phone`, DEL matches. Iteration cap (64).
- Workflow: new HTTP node `Cancel Prior Autosend` wired off Format Context (parallel arm, `onError=continueRegularOutput`). Fires `disarm_by_customer` on every inbound customer message before Build Prompt.

**Verified:** end-to-end integration test against live bridge — arm fake autosend → call `disarm_by_customer` → key DELed.

## 3. WAHA pushName fallback — `15ced15` + `a358289`

**Bug:** 10 of 11 /review cards displayed "Unknown" because the WAHA webhook payload omits `notifyName` / `pushName`. Format Context's `customerName: wh.notifyName || wh.pushName || ''` evaluated to empty for every real customer.

**Fix:**
- New `waha_lookup_push_name(cid)` helper with 5-minute in-process cache.
- `_customer_facts` falls back to WAHA pushName when no name resolved.
- Relaxed the phone-string filter so WhatsApp display strings like `+44 7869 651761` survive — operator can match them directly to their WhatsApp contact list.

**Result on 9 active customers in DB:**
- before: 1 named (Qurbani), 10 "Unknown" (per audit count)
- after: 4 real names + 5 phone-string fallbacks, **0 "Unknown"**

## 4. `/review` last-4 fallback — `d8106d9`

**Bug:** Cards still showed "Unknown" when WAHA had no contact entry at all.

**Fix:** Use `_name_fallback(customer_id)` (`…XXXX` last-4-digits) when name is empty AND WAHA pushName lookup also returned empty. (Superseded in practice by #3 — phone strings cover most cases; this is the final safety net.)

---

## Files touched
- `hermes-bridge/server.py` (+170 / -10)
- `workflows/phase-1b-telegram.json` (+62 / -2)
- `docs/fixes-2026-05-23-evening.md` (this file)

## Commits
```
a358289 fix(waha): keep phone-string pushNames in name fallback
d8106d9 fix(/review): use last-4-digits fallback instead of 'Unknown'
15ced15 fix(customer_facts): fall back to WAHA pushName when no name resolved
f0e206f fix(autosend): cancel armed timers when customer sends a new message
686556d fix(payment): deterministic guard against hallucinated Nomod URLs
```

## Not pushed to origin
Default-branch push remains blocked by the auto-mode classifier. The commits are local; the live EC2 deploys (bridge + n8n workflow) are independent and current.
