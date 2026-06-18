# USDT-Only Invoice Flow — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the operator serve a USDT customer end-to-end — itemized invoice preview they verify/edit, then a proforma PDF (with the USDT wallet + amount) + canned USDT text, no card link; tax invoice on USDT-received.

**Architecture:** New bridge endpoints (`/usdt-quote`, `/usdt-edit`, `/usdt-issue`, `/usdt-tax`) build/compute/store a quote in Redis and post an operator-only preview card; a 🪙 button on the main n8n card starts it and its preview buttons drive it. The wallet address + USDT math live entirely in Python; the LLM only ever rewrites invoice line items.

**Tech Stack:** Python 3 (stdlib only — `urllib`, `json`, `math`, `re`), `http.server` bridge, Redis + Postgres via `docker exec` (`db.py`), n8n workflow edited directly in Postgres (`workflow_entity` + `workflow_history`), chromium-in-docker PDF render, Anthropic Messages API.

## Global Constraints

Copied verbatim from the spec — every task's requirements include these:

- **Wallet address is ALWAYS `os.environ['USDT_TRC20_ADDRESS']`** (currently `TM9Yfh5Lef26XHLv993eTL2KroahMNpJSx`). NEVER LLM-composed, NEVER hardcoded in n8n, NEVER in any LLM prompt.
- **USDT amount = `math.ceil(total_aed / 3.6725)`**, where `total_aed = round(subtotal * 1.05, 2)` and `subtotal = round(Σ qty*rate, 2)`. Whole integer (no cents). Crypto only — invoices keep precise VAT.
- **USDT invoice = ×1.05 (5% VAT, NO 2% card fee).** Proforma uses the random-gap counter (`invoice_counter` id=1, from 3000); tax uses the sequential gap-free counter (id=2, starts 3333). They do not share a number.
- LLM edit (`/usdt-edit`) rewrites **line items only**; its output schema forbids any wallet/financial string; code recomputes all amounts afterward.
- Bridge deploy: edit Mac mirror `~/projects/dubriani-hermes-bridge/` → `scp` to box `/home/ubuntu/hermes-bridge/` → `systemctl --user restart hermes-bridge`. `.bak-usdtflow` before each file's first edit on the box.
- n8n edits go to **BOTH** `workflow_entity` AND `workflow_history` (versionId `b68eb9c4-1db3-4e3f-9a51-adeaad026d7f`), applied with a Python build script + dollar-quoted UPDATE, **snapshot first** + python-diff verify (md5 of nodes must match between tables after). **No `Parse Callback` edit** — every new callback carries a real draft_id (`<action>:<draft_id>[:<extra>]`) so it passes the `Draft Exists?` gate. `docker restart n8n-n8n-1` to load.
- DB users: bridge tables (`invoice_counter`, `invoices`) via `hermes_rw` (docker exec, no DELETE — DELETE needs admin `$POSTGRES_USER`); workflow tables via admin `$POSTGRES_USER`.
- Single-deployer (confirm `who`/ssh-count/no editor-git-restart before starting). Verify everything on test number **971509767187**. `INVOICE_ENABLED=1` already live.

---

## File Structure

- **Modify** `invoice/invoice_gen.py` — `compute()` and `generate_invoice()` become rail-aware (`d['rail']=='usdt'` ⇒ no card fee); `_build_html()` adds a "Crypto Payment (USDT)" block + drops the card-fee totals row for usdt.
- **Modify** `invoice_hooks.py` — add `compute_usdt_quote(items)`, `issue_usdt_proforma(cid, name, items, ref)`, `issue_usdt_tax(cid, name, items, ref)`.
- **Modify** `routes.py` — add `_usdt_preview_card(...)`, `handle_usdt_quote`, `handle_usdt_edit`, `handle_usdt_issue`, `handle_usdt_tax`.
- **Modify** `server.py` — import the 4 handlers; add their paths to the allowlist + dispatch.
- **Create** `test_usdt_invoice.py` — unit tests for compute + rail-aware invoice + issue funcs (mock `_psql`/`generate_invoice`/WAHA).
- **Modify** n8n workflow `azPIy9OcDwiPV5uY` (via Postgres) — 🪙 button on `Queue & Format` + `Auto Prep`; `Route Action` outputs `usdtquote`/`usdtissue`/`usdtedit`/`usdtcancel`/`usdtreceived`; `Prep USDT`, rail branch in the amount-reply path, and the call-bridge nodes.

---

## Task 1: Rail-aware invoice computation

**Files:**
- Modify: `invoice/invoice_gen.py` (`compute`, `generate_invoice`)
- Test: `test_usdt_invoice.py`

**Interfaces:**
- Produces: `compute(items, paid=0.0, rail="card") -> (sub, vat, card_fee, total, due)`. For `rail=="usdt"`, `card_fee==0.0` and `total==sub+vat`.

- [ ] **Step 1: Write the failing test**
```python
# test_usdt_invoice.py
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "invoice"))
import invoice_gen

def test_usdt_compute_no_card_fee():
    items = [{"name": "Charter", "qty": 1, "rate": 5000.0}]
    sub, vat, fee, total, due = invoice_gen.compute(items, rail="usdt")
    assert sub == 5000.0 and vat == 250.0 and fee == 0.0 and total == 5250.0

def test_card_compute_unchanged():
    items = [{"name": "Charter", "qty": 1, "rate": 5000.0}]
    sub, vat, fee, total, due = invoice_gen.compute(items)  # default card
    assert fee == 262.5 and total == 5512.5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/projects/dubriani-hermes-bridge && python3 -m pytest test_usdt_invoice.py -k compute -v`
Expected: FAIL — `compute()` takes no `rail` kwarg.

- [ ] **Step 3: Implement**

In `invoice/invoice_gen.py`, change `compute` signature + card_fee:
```python
def compute(items, paid=0.0, rail="card"):
    sub = round(sum(it["qty"] * it["rate"] for it in items), 2)
    vat = round(sub * 0.05, 2)
    sub_incl = round(sub + vat, 2)
    card_fee = 0.0 if rail == "usdt" else round(sub_incl * 0.02, 2)
    total = round(sub_incl + card_fee, 2)
    due = round(total - (paid or 0.0), 2)
    return sub, vat, card_fee, total, due
```
And in `generate_invoice`, pass the rail: `sub, vat, card_fee, total, due = compute(d["items"], d.get("paid", 0.0), d.get("rail", "card"))`.

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest test_usdt_invoice.py -k compute -v`
Expected: PASS (both).

- [ ] **Step 5: Commit**
```bash
git add invoice/invoice_gen.py test_usdt_invoice.py
git commit -m "feat(invoice): rail-aware compute (usdt = no card fee)"
```

---

## Task 2: Crypto block on the invoice PDF (usdt rail)

**Files:**
- Modify: `invoice/invoice_gen.py` (`_build_html`)
- Test: `test_usdt_invoice.py`

**Interfaces:**
- Consumes: `d` may carry `rail`, `usdt_amount`, `usdt_address`, `payment_ref`.
- Produces: when `d['rail']=='usdt'`, `_build_html` output contains the wallet address, the USDT amount, "TRC20", and omits the "Card processing fee" row.

- [ ] **Step 1: Write the failing test**
```python
def test_usdt_html_has_crypto_block():
    d = {"doc_type": "proforma", "rail": "usdt", "invoice_no": 3041,
         "bill_to": "Ahmed", "invoice_date": "18/06/2026", "booking_date": "—",
         "booking_time": "—", "items": [{"name": "Charter", "qty": 1, "rate": 5000.0}],
         "paid": 0.0, "usdt_amount": 1430, "usdt_address": "TM9Yfh5Lef26XHLv993eTL2KroahMNpJSx",
         "payment_ref": "3041"}
    sub, vat, fee, total, due = invoice_gen.compute(d["items"], rail="usdt")
    html = invoice_gen._build_html(d, sub, vat, fee, total, due)
    assert "TM9Yfh5Lef26XHLv993eTL2KroahMNpJSx" in html
    assert "1430 USDT" in html and "TRC20" in html
    assert "Card processing fee" not in html
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest test_usdt_invoice.py -k crypto_block -v`
Expected: FAIL — address not in html, card-fee row present.

- [ ] **Step 3: Implement**

In `_build_html`: (a) make the card-fee totals row conditional — replace the unconditional
`<tr><td class=k>Card processing fee (2%)</td>...` with
```python
    fee_row = (f"<tr><td class=k>Card processing fee (2%)</td><td class=v>{_money(card_fee)}</td></tr>"
               if d.get("rail") != "usdt" else "")
```
and interpolate `{fee_row}` where that row was.
(b) Build a crypto block and inject it just before `<div class=thanks>`:
```python
    crypto = ""
    if d.get("rail") == "usdt":
        crypto = (f"<div class=ti><div class=h>Crypto Payment (USDT)</div>"
                  f"<p>Network: Tron (TRC20)<br>Amount: <b>{d.get('usdt_amount')} USDT</b>"
                  f"<br>Wallet address:<br><b>{html.escape(str(d.get('usdt_address','')))}</b>"
                  f"<br>Reference: {html.escape(str(d.get('payment_ref','')))}</p></div>")
```
Insert `{crypto}` into the template right before `<div class=thanks>`.

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest test_usdt_invoice.py -v`
Expected: PASS (all 3).

- [ ] **Step 5: Commit**
```bash
git add invoice/invoice_gen.py test_usdt_invoice.py
git commit -m "feat(invoice): crypto/USDT block on PDF for usdt rail"
```

---

## Task 3: `compute_usdt_quote` (the money math, code-only)

**Files:**
- Modify: `invoice_hooks.py`
- Test: `test_usdt_invoice.py`

**Interfaces:**
- Produces: `compute_usdt_quote(items) -> dict {subtotal, vat, total_aed, usdt_amount, address}`. `usdt_amount` is an `int`. `address` from env (empty string if unset).

- [ ] **Step 1: Write the failing test**
```python
import math, importlib
import invoice_hooks

def test_compute_usdt_quote(monkeypatch):
    monkeypatch.setenv("USDT_TRC20_ADDRESS", "TM9Yfh5Lef26XHLv993eTL2KroahMNpJSx")
    q = invoice_hooks.compute_usdt_quote([{"name": "Charter", "qty": 1, "rate": 5000.0}])
    assert q["subtotal"] == 5000.0 and q["vat"] == 250.0 and q["total_aed"] == 5250.0
    assert q["usdt_amount"] == math.ceil(5250.0 / 3.6725) == 1430
    assert q["address"] == "TM9Yfh5Lef26XHLv993eTL2KroahMNpJSx"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest test_usdt_invoice.py -k compute_usdt_quote -v`
Expected: FAIL — `compute_usdt_quote` not defined.

- [ ] **Step 3: Implement**

In `invoice_hooks.py` (add near the top, after imports — note `import math`):
```python
USDT_RATE = float(os.environ.get("USDT_AED_RATE", "3.6725"))  # AED per USD/USDT peg

def compute_usdt_quote(items):
    """Pure money math for a USDT invoice. ×1.05 (VAT, NO card fee), then convert
    the VAT-inclusive total to whole-number USDT (round UP). Address from env only."""
    sub = round(sum(float(it.get("qty", 1)) * float(it["rate"]) for it in items), 2)
    vat = round(sub * 0.05, 2)
    total = round(sub + vat, 2)
    usdt = int(math.ceil(total / USDT_RATE))
    return {"subtotal": sub, "vat": vat, "total_aed": total,
            "usdt_amount": usdt, "address": (os.environ.get("USDT_TRC20_ADDRESS") or "").strip()}
```
(Add `import math` to the existing `import` line.)

- [ ] **Step 4: Run tests** — Run: `python3 -m pytest test_usdt_invoice.py -k compute_usdt_quote -v` → PASS.

- [ ] **Step 5: Commit**
```bash
git add invoice_hooks.py test_usdt_invoice.py
git commit -m "feat(invoice): compute_usdt_quote (×1.05, ceil to whole USDT)"
```

---

## Task 4: `issue_usdt_proforma` + `issue_usdt_tax`

**Files:**
- Modify: `invoice_hooks.py`
- Test: `test_usdt_invoice.py`

**Interfaces:**
- Consumes: `compute_usdt_quote`, `_allocate_number` (id=1, proforma), `_allocate_tax_number` (id=2, tax), `invoice_gen.generate_invoice`, `_ledger`, `_send_pdf`, `_notify`.
- Produces: `issue_usdt_proforma(cid, customer_name, items, payment_ref="") -> invoice_no|None`; `issue_usdt_tax(cid, customer_name, items, payment_ref="") -> invoice_no|None`. Both FAIL-OPEN.

- [ ] **Step 1: Write the failing test** (mock the render + send + db)
```python
def test_issue_usdt_proforma(monkeypatch):
    monkeypatch.setenv("INVOICE_ENABLED", "1")
    monkeypatch.setenv("USDT_TRC20_ADDRESS", "TM9Yfh5Lef26XHLv993eTL2KroahMNpJSx")
    calls = {}
    monkeypatch.setattr(invoice_hooks, "_allocate_number", lambda: 3041)
    monkeypatch.setattr(invoice_hooks.invoice_gen, "generate_invoice",
        lambda d: calls.setdefault("d", d) or {"ok": True, "pdf_path": "/tmp/x.pdf",
            "subtotal": 5000.0, "vat": 250.0, "card_fee": 0.0, "total": 5250.0, "balance_due": 5250.0})
    monkeypatch.setattr(invoice_hooks, "_ledger", lambda *a, **k: None)
    monkeypatch.setattr(invoice_hooks, "_send_pdf", lambda *a, **k: calls.setdefault("sent", True))
    monkeypatch.setattr(invoice_hooks, "_notify", lambda *a, **k: None)
    no = invoice_hooks.issue_usdt_proforma("971@c.us", "Ahmed",
            [{"name": "Charter", "qty": 1, "rate": 5000.0}], payment_ref="3041")
    assert no == 3041
    assert calls["d"]["rail"] == "usdt" and calls["d"]["usdt_amount"] == 1430
    assert calls["d"]["usdt_address"] == "TM9Yfh5Lef26XHLv993eTL2KroahMNpJSx"
    assert calls["sent"] is True
```

- [ ] **Step 2: Run to verify fail** — Run: `python3 -m pytest test_usdt_invoice.py -k issue_usdt_proforma -v` → FAIL (not defined).

- [ ] **Step 3: Implement** in `invoice_hooks.py`:
```python
def _issue_usdt(cid, customer_name, items, payment_ref, doc_type, no):
    q = compute_usdt_quote(items)
    name = customer_name or _facts(cid).get("name") or "Valued Guest"
    d = dict(doc_type=doc_type, rail="usdt", invoice_no=no, bill_to=name,
             invoice_date=time.strftime("%d/%m/%Y"),
             booking_date=(_facts(cid).get("dates") or "—"),
             booking_time=(_facts(cid).get("booking_time") or "—"),
             items=items, paid=0.0,
             usdt_amount=q["usdt_amount"], usdt_address=q["address"],
             payment_ref=(payment_ref or str(no)))
    r = invoice_gen.generate_invoice(d)
    if not r.get("ok"):
        log("usdt %s render failed" % doc_type); return None
    comp = (r["subtotal"], r["vat"], r["card_fee"], r["total"], r["balance_due"])
    _ledger(no, doc_type, "", cid, name, items, comp, r["pdf_path"], "usdt", "sent")
    cap = ("Dubriani — %s invoice #%s. Pay %s USDT (AED %s incl VAT) to the wallet on the invoice."
           % (doc_type, no, q["usdt_amount"], format(q["total_aed"], ",.2f")))
    _send_pdf(cid, r["pdf_path"], no, cap)
    _notify("🪙 USDT %s #%s — %s — %s USDT" % (doc_type, no, name, q["usdt_amount"]))
    return no

def issue_usdt_proforma(cid, customer_name, items, payment_ref=""):
    try:
        if not enabled():
            return None
        return _issue_usdt(cid, customer_name, items, payment_ref, "proforma", _allocate_number())
    except Exception as e:
        log("issue_usdt_proforma EXC (fail-open):", repr(e)); return None

def issue_usdt_tax(cid, customer_name, items, payment_ref=""):
    try:
        if not enabled():
            return None
        return _issue_usdt(cid, customer_name, items, payment_ref, "tax", _allocate_tax_number())
    except Exception as e:
        log("issue_usdt_tax EXC (fail-open):", repr(e)); return None
```

- [ ] **Step 4: Run tests** — Run: `python3 -m pytest test_usdt_invoice.py -v` → PASS (all).

- [ ] **Step 5: Commit**
```bash
git add invoice_hooks.py test_usdt_invoice.py
git commit -m "feat(invoice): issue_usdt_proforma/tax (rail=usdt, crypto details)"
```

---

## Task 5: Quote storage + operator preview card (`handle_usdt_quote`)

**Files:**
- Modify: `routes.py` (add `_usdt_items_from_facts`, `_usdt_store`, `_usdt_load`, `_usdt_preview_card`, `handle_usdt_quote`)
- Modify: `server.py` (import + allowlist + dispatch `/usdt-quote`)

**Interfaces:**
- Consumes: `compute_usdt_quote`, `_redis`, `_tg_post`, `get_customer_facts`.
- Produces: `handle_usdt_quote(payload, send)` where payload `{customer_id, draft_id, price}`. Stores `{"items":[...], "ref":no?}` at Redis key `hermes:usdtq:<draft_id>` (TTL 3600). Posts an operator card to `DEFAULT_ADMIN_CHAT` with buttons `usdtissue:<draft_id>` / `usdtedit:<draft_id>` / `usdtcancel:<draft_id>`. Always 200.

- [ ] **Step 1: Write the failing test** (mock redis + tg)
```python
# test_usdt_routes.py
import json, routes
def test_usdt_quote_stores_and_cards(monkeypatch):
    monkeypatch.setenv("USDT_TRC20_ADDRESS", "TM9Yfh5Lef26XHLv993eTL2KroahMNpJSx")
    store = {}
    monkeypatch.setattr(routes, "_redis", lambda a: (store.__setitem__(a[1], a[2]) or (None, None)) if a[0]=="SET" else (store.get(a[1]), None))
    posts = []
    monkeypatch.setattr(routes, "_tg_post", lambda m, b: posts.append(b))
    monkeypatch.setattr(routes, "get_customer_facts", lambda cid: {"yachts": "Aurora 80ft", "dates": "Sat 14 Jun"})
    out = {}
    routes.handle_usdt_quote({"customer_id": "971@c.us", "draft_id": "d1", "price": 5000},
                             lambda code, body: out.update(code=code, body=body))
    assert out["code"] == 200 and out["body"]["ok"] is True
    assert "hermes:usdtq:d1" in store
    assert "1430 USDT" in posts[0]["text"] and "usdtissue:d1" in json.dumps(posts[0])
```

- [ ] **Step 2: Run to verify fail** — `python3 -m pytest test_usdt_routes.py -k quote -v` → FAIL.

- [ ] **Step 3: Implement** in `routes.py` (the `get_customer_facts`/`_tg_post`/`DEFAULT_ADMIN_CHAT` come from `server`; import lazily inside the handler as elsewhere):
```python
def _usdt_items_from_facts(cid, price):
    from server import get_customer_facts
    f = get_customer_facts(cid) or {}
    yacht = (f.get("yachts") or "").split(",")[0].strip() or "Dubriani charter"
    detail = " · ".join([x for x in [(f.get("dates") or "").strip(),
                         (f.get("booking_time") or "").strip(),
                         (f.get("party_size") or "").strip()] if x])
    name = yacht + (" — " + detail if detail else "")
    return [{"name": name, "qty": 1, "rate": round(float(price), 2)}]

def _usdt_card_text(items, q, cname):
    lines = ["🪙 USDT proforma — %s   (PREVIEW — not sent)" % (cname or "customer"), "", "Line items"]
    for i, it in enumerate(items, 1):
        lines.append("  %d. %s … AED %s" % (i, it["name"], format(it["qty"]*it["rate"], ",.2f")))
    lines += ["", "  Subtotal   AED %s" % format(q["subtotal"], ",.2f"),
              "  VAT (5%%)   AED %s" % format(q["vat"], ",.2f"),
              "  Total      AED %s" % format(q["total_aed"], ",.2f"),
              "  ──────────",
              "  Pay in USDT  %d USDT  (TRC20)" % q["usdt_amount"],
              "  Wallet  %s" % q["address"]]
    return "\n".join(lines)

def _usdt_preview(cid, draft_id, items, cname):
    import outbound_guard
    from server import _tg_post, DEFAULT_ADMIN_CHAT
    q = compute_usdt_quote(items)  # imported from invoice_hooks below
    text = _usdt_card_text(items, q, cname)
    kb = {"inline_keyboard": [[{"text": "✅ Send to customer", "callback_data": "usdtissue:" + draft_id}],
          [{"text": "✏️ Edit invoice", "callback_data": "usdtedit:" + draft_id},
           {"text": "❌ Cancel", "callback_data": "usdtcancel:" + draft_id}]]}
    _tg_post("sendMessage", {"chat_id": DEFAULT_ADMIN_CHAT, "text": text, "reply_markup": kb})

def handle_usdt_quote(payload, send):
    from invoice_hooks import compute_usdt_quote as _c  # ensure import path
    cid = (payload.get("customer_id") or "").strip()
    did = (payload.get("draft_id") or "").strip()
    try:
        price = float(payload.get("price"))
    except (TypeError, ValueError):
        price = 0.0
    if not cid or not did or price <= 0 or not (os.environ.get("USDT_TRC20_ADDRESS") or "").strip():
        send(200, {"ok": False, "telegram_text": "⚠️ USDT quote needs a customer, draft, positive price, and USDT_TRC20_ADDRESS set."})
        return
    cname = (payload.get("customer_name") or "").strip()
    items = _usdt_items_from_facts(cid, price)
    try:
        _redis(["SET", "hermes:usdtq:" + did, json.dumps({"cid": cid, "cname": cname, "items": items}), "EX", "3600"])
    except Exception as e:
        log("usdt-quote store err:", repr(e))
    _usdt_preview(cid, did, items, cname)
    send(200, {"ok": True})
```
At the top of `routes.py` near the other invoice import, add `from invoice_hooks import compute_usdt_quote, issue_usdt_proforma, issue_usdt_tax` (or import lazily in each handler to avoid circulars — match the file's existing lazy-import style; `handle_send_customer` imports inside the function, so do the same).

In `server.py`: add `handle_usdt_quote` to the `from routes import (...)` block; add `"/usdt-quote"` to the allowlist tuple; add `elif self.path == "/usdt-quote": handle_usdt_quote(payload, self._send)`.

- [ ] **Step 4: Run tests** — `python3 -m pytest test_usdt_routes.py -k quote -v` → PASS; `python3 -m py_compile routes.py server.py` → OK.

- [ ] **Step 5: Commit**
```bash
git add routes.py server.py test_usdt_routes.py
git commit -m "feat(usdt): /usdt-quote — store quote + operator preview card"
```

---

## Task 6: Free-text edit (`handle_usdt_edit`) — LLM rewrites line items only

**Files:**
- Modify: `routes.py` (`handle_usdt_edit`, `_usdt_llm_items`)
- Modify: `server.py` (`/usdt-edit`)

**Interfaces:**
- Consumes: `_anthropic_draft`-style Messages API call, `_redis`, `_usdt_preview`.
- Produces: `handle_usdt_edit(payload, send)` payload `{draft_id, feedback}`. Loads stored items, asks the LLM for new `items`, validates (list of `{name, rate}`, no TRC20/IBAN token via `outbound_guard.check_financial` on the names), updates Redis, re-posts the preview. On invalid → keep old items, tell operator.

- [ ] **Step 1: Write the failing test** (mock the LLM call + redis + tg)
```python
def test_usdt_edit_rewrites_items(monkeypatch):
    monkeypatch.setenv("USDT_TRC20_ADDRESS", "TM9Yfh5Lef26XHLv993eTL2KroahMNpJSx")
    store = {"hermes:usdtq:d1": json.dumps({"cid": "971@c.us", "cname": "Ahmed",
             "items": [{"name": "Aurora 80ft", "qty": 1, "rate": 5000.0}]})}
    monkeypatch.setattr(routes, "_redis", lambda a: (store.__setitem__(a[1], a[2]) or (None, None)) if a[0]=="SET" else (store.get(a[1]), None))
    monkeypatch.setattr(routes, "_usdt_llm_items",
        lambda items, fb: [{"name": "Bliss 90ft", "qty": 1, "rate": 5000.0}, {"name": "Catering", "qty": 1, "rate": 500.0}])
    posts = []; monkeypatch.setattr(routes, "_tg_post", lambda m, b: posts.append(b))
    out = {}
    routes.handle_usdt_edit({"draft_id": "d1", "feedback": "change yacht to Bliss, add catering 500"},
                            lambda c, b: out.update(c=c, b=b))
    assert out["c"] == 200 and out["b"]["ok"] is True
    assert "Bliss 90ft" in posts[0]["text"] and "Catering" in posts[0]["text"]
    assert "5,775.00" in posts[0]["text"]  # 5500*1.05
```

- [ ] **Step 2: Run to verify fail** — `python3 -m pytest test_usdt_routes.py -k edit -v` → FAIL.

- [ ] **Step 3: Implement** in `routes.py`:
```python
def _usdt_llm_items(items, feedback):
    """Ask the LLM to rewrite invoice line items per the operator's note. Returns a
    list of {name, rate} or None. NEVER asks for / accepts a wallet address."""
    import urllib.request, outbound_guard
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        return None
    sysmsg = ("You edit invoice LINE ITEMS for a yacht charter. Apply the operator's note to the "
              "current items. Output ONLY JSON: {\"items\":[{\"name\":string,\"rate\":number}]}. "
              "qty is always 1. NEVER output any wallet address, crypto address, IBAN, or payment string.")
    uc = "CURRENT ITEMS:\n%s\n\nOPERATOR NOTE:\n%s" % (json.dumps(items), feedback)
    data = json.dumps({"model": "claude-sonnet-4-6", "max_tokens": 500,
                       "system": [{"type": "text", "text": sysmsg}],
                       "messages": [{"role": "user", "content": uc}]}).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=data)
    req.add_header("x-api-key", key); req.add_header("anthropic-version", "2023-06-01")
    req.add_header("content-type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            resp = json.load(r)
        txt = resp["content"][0]["text"]
        t = re.sub(r"^```json\s*|^```\s*|```\s*$", "", (txt or "").strip()).strip()
        new = json.loads(t).get("items")
        if not isinstance(new, list) or not new:
            return None
        out = []
        for it in new:
            nm = str(it.get("name", "")).strip()
            ok, _ = outbound_guard.check_financial(nm)   # reject any address-shaped token in a name
            if not nm or not ok:
                return None
            out.append({"name": nm, "qty": 1, "rate": round(float(it["rate"]), 2)})
        return out
    except Exception as e:
        log("usdt-edit llm err:", repr(e)); return None

def handle_usdt_edit(payload, send):
    did = (payload.get("draft_id") or "").strip()
    fb = (payload.get("feedback") or "").strip()
    raw, _ = _redis(["GET", "hermes:usdtq:" + did])
    if not raw:
        send(200, {"ok": False, "telegram_text": "⚠️ That USDT quote expired — start again."}); return
    st = json.loads(raw)
    new_items = _usdt_llm_items(st["items"], fb) if fb else None
    if not new_items:
        send(200, {"ok": False, "telegram_text": "⚠️ Couldn't apply that edit — try rephrasing."}); return
    st["items"] = new_items
    _redis(["SET", "hermes:usdtq:" + did, json.dumps(st), "EX", "3600"])
    _usdt_preview(st["cid"], did, new_items, st.get("cname"))
    send(200, {"ok": True})
```
`server.py`: import `handle_usdt_edit`; allowlist `"/usdt-edit"`; dispatch `elif self.path == "/usdt-edit": handle_usdt_edit(payload, self._send)`.

- [ ] **Step 4: Run tests** — `python3 -m pytest test_usdt_routes.py -v` → PASS; `py_compile` OK.

- [ ] **Step 5: Commit**
```bash
git add routes.py server.py test_usdt_routes.py
git commit -m "feat(usdt): /usdt-edit — LLM rewrites line items, address stays code-side"
```

---

## Task 7: Issue + send (`handle_usdt_issue`) and tax (`handle_usdt_tax`)

**Files:**
- Modify: `routes.py` (`handle_usdt_issue`, `handle_usdt_tax`)
- Modify: `server.py` (`/usdt-issue`, `/usdt-tax`)

**Interfaces:**
- Consumes: `_redis`, `issue_usdt_proforma`, `issue_usdt_tax`, the existing `/send-usdt` self-call (amount = the VAT-inclusive total).
- Produces: `handle_usdt_issue(payload, send)` payload `{draft_id}` → issues proforma + sends USDT text (amount=total_aed) to the customer; `handle_usdt_tax(payload, send)` payload `{draft_id}` → issues tax invoice.

- [ ] **Step 1: Write the failing test**
```python
def test_usdt_issue_sends_proforma_and_text(monkeypatch):
    store = {"hermes:usdtq:d1": json.dumps({"cid": "971@c.us", "cname": "Ahmed",
             "items": [{"name": "Aurora", "qty": 1, "rate": 5000.0}]})}
    monkeypatch.setattr(routes, "_redis", lambda a: (store.get(a[1]), None))
    seen = {}
    monkeypatch.setattr(routes, "issue_usdt_proforma", lambda cid, n, items, payment_ref="": seen.setdefault("pf", (cid, items)) or 3041)
    monkeypatch.setattr(routes, "_usdt_send_text", lambda cid, total: seen.setdefault("usdt", (cid, total)))
    out = {}
    routes.handle_usdt_issue({"draft_id": "d1"}, lambda c, b: out.update(c=c, b=b))
    assert out["c"] == 200 and out["b"]["ok"] is True
    assert seen["pf"][0] == "971@c.us"
    assert seen["usdt"] == ("971@c.us", 5250.0)  # VAT-inclusive total to /send-usdt
```

- [ ] **Step 2: Run to verify fail** — `python3 -m pytest test_usdt_routes.py -k issue -v` → FAIL.

- [ ] **Step 3: Implement** in `routes.py`:
```python
def _usdt_send_text(cid, total_aed):
    """Send the canned USDT text via the /send-usdt chokepoint, amount = VAT-inclusive total."""
    import urllib.request as _u
    tok = os.environ.get("BRIDGE_TOKEN", "")
    body = json.dumps({"chatId": cid, "amount": total_aed}).encode()
    req = _u.Request("http://127.0.0.1:8788/send-usdt", data=body, method="POST")
    req.add_header("Content-Type", "application/json"); req.add_header("X-Bridge-Token", tok)
    try:
        with _u.urlopen(req, timeout=25) as r:
            return json.load(r)
    except Exception as e:
        log("usdt-issue send-text err:", repr(e)); return None

def handle_usdt_issue(payload, send):
    from invoice_hooks import compute_usdt_quote
    did = (payload.get("draft_id") or "").strip()
    raw, _ = _redis(["GET", "hermes:usdtq:" + did])
    if not raw:
        send(200, {"ok": False, "telegram_text": "⚠️ That USDT quote expired — start again."}); return
    st = json.loads(raw)
    no = issue_usdt_proforma(st["cid"], st.get("cname"), st["items"], payment_ref="")
    q = compute_usdt_quote(st["items"])
    _usdt_send_text(st["cid"], q["total_aed"])
    _redis(["SET", "hermes:usdtq:" + did, json.dumps(dict(st, proforma_no=no)), "EX", "604800"])  # keep 7d for the tax step
    send(200, {"ok": bool(no), "telegram_text": "🪙 USDT proforma #%s + payment details sent." % (no or "?")})

def handle_usdt_tax(payload, send):
    did = (payload.get("draft_id") or "").strip()
    raw, _ = _redis(["GET", "hermes:usdtq:" + did])
    if not raw:
        send(200, {"ok": False, "telegram_text": "⚠️ No USDT quote found for that card."}); return
    st = json.loads(raw)
    no = issue_usdt_tax(st["cid"], st.get("cname"), st["items"], payment_ref=str(st.get("proforma_no") or ""))
    send(200, {"ok": bool(no), "telegram_text": "🧾 USDT tax invoice #%s issued + sent." % (no or "?")})
```
`server.py`: import both; allowlist `"/usdt-issue", "/usdt-tax"`; dispatch both.

- [ ] **Step 4: Run tests** — `python3 -m pytest test_usdt_routes.py -v` → PASS; `py_compile` OK.

- [ ] **Step 5: Commit**
```bash
git add routes.py server.py test_usdt_routes.py
git commit -m "feat(usdt): /usdt-issue + /usdt-tax — proforma+text, then tax on receipt"
```

---

## Task 8: Deploy bridge + curl smoke-test the endpoints

**Files:** none (deploy + verify)

- [ ] **Step 1: Single-deployer check**

Run: `ssh dubriani-ec2 'who; ss -tnH state established "( sport = :22 )" | wc -l; ps -eo cmd|grep -E "vim|nano|git |systemctl"|grep -v grep'`
Expected: only your session; no editor/git/restart procs.

- [ ] **Step 2: Backup + scp + compile (no restart)**
```bash
ssh dubriani-ec2 'cd /home/ubuntu/hermes-bridge && for f in invoice/invoice_gen.py invoice_hooks.py routes.py server.py; do cp -p "$f" "$f.bak-usdtflow"; done'
scp ~/projects/dubriani-hermes-bridge/{invoice/invoice_gen.py,invoice_hooks.py,routes.py,server.py} dubriani-ec2:/home/ubuntu/hermes-bridge/  # mind invoice/ path
ssh dubriani-ec2 'cd /home/ubuntu/hermes-bridge && python3 -m py_compile invoice/invoice_gen.py invoice_hooks.py routes.py server.py && echo BOX_PY_OK'
```
Expected: `BOX_PY_OK`.

- [ ] **Step 3: Restart bridge**
```bash
ssh dubriani-ec2 'systemctl --user restart hermes-bridge; curl -s -m4 --retry 12 --retry-connrefused localhost:8788/health'
```
Expected: `{"status": "ok", ...}`.

- [ ] **Step 4: Curl smoke-test the quote → preview (to the operator chat, nothing to customer)**
```bash
ssh dubriani-ec2 'TOK=$(grep ^BRIDGE_TOKEN= /home/ubuntu/hermes-bridge/.env|cut -d= -f2-); curl -s -m10 -X POST localhost:8788/usdt-quote -H "Content-Type: application/json" -H "X-Bridge-Token: $TOK" -d "{\"customer_id\":\"971509767187@c.us\",\"draft_id\":\"smoke1\",\"price\":5000}"'
```
Expected: `{"ok": true}` and a 🪙 preview card arrives in the operator Telegram chat showing `Total AED 5,250.00`, `1430 USDT`, the wallet, and ✅/✏️/❌ buttons. (Buttons won't route until Task 9–10 wire the n8n side.)

- [ ] **Step 5: Commit** (deploy is not a code change; record the .bak in STATE later). No commit.

---

## Task 9: n8n — snapshot, then add the 🪙 button + Route Action outputs + amount branch

**Files:** Postgres workflow `azPIy9OcDwiPV5uY` (via a Python build script, like the 2026-06-18 button)

**Interfaces:**
- Produces: 🪙 button (`usdtquote:<draft_id>`) on `Queue & Format` + `Auto Prep`; `Route Action` outputs `usdtquote`/`usdtissue`/`usdtedit`/`usdtcancel`/`usdtreceived`; the amount-reply path branches `rail=usdt` → `POST /usdt-quote`; preview/`💵` buttons → the bridge calls.

- [ ] **Step 1: Snapshot both tables**
```bash
ssh dubriani-ec2 'PGU=$(docker exec n8n-postgres-1 printenv POSTGRES_USER); PGD=$(docker exec n8n-postgres-1 printenv POSTGRES_DB); D=/home/ubuntu/reroute-revert; for t in entity history; do :; done; docker exec n8n-postgres-1 psql -U "$PGU" -d "$PGD" -tAc "SELECT nodes FROM workflow_entity WHERE id='"'"'azPIy9OcDwiPV5uY'"'"';" > $D/nodes_pre_usdtflow.json; docker exec n8n-postgres-1 psql -U "$PGU" -d "$PGD" -tAc "SELECT connections FROM workflow_entity WHERE id='"'"'azPIy9OcDwiPV5uY'"'"';" > $D/conns_pre_usdtflow.json; md5sum $D/nodes_pre_usdtflow.json $D/conns_pre_usdtflow.json'
```
Expected: two files + md5s recorded (the revert).

- [ ] **Step 2: Write the build script** `build_usdt_flow.py` (mirror `build_usdt_button.py` from 2026-06-18): load nodes+conns, then:
  - In `Queue & Format` and `Auto Prep` jsCode: add `{ text: '🪙 USDT', callback_data: 'usdtquote:' + id }` to the inline_keyboard (next to the 💳 Send Link button) — string-insert after the `💳 Send Link` button object.
  - In `Route Action` `parameters.rules.values`: append 5 rules (clone of the `paylink` rule idx12, changing `outputKey`/`rightValue`) for `usdtquote`, `usdtissue`, `usdtedit`, `usdtcancel`, `usdtreceived`. Record their output indices (18–22).
  - Add nodes: `Prep USDT` (code: pass draft_id through, set a `rail:'usdt'` marker), `Set Awaiting USDT` (POST `/queue` mark `awaiting_amount` like `Set Awaiting Amount`), `Call USDT Quote` (httpRequest → `/usdt-quote` `{customer_id, draft_id, price}`), `Call USDT Issue` (→ `/usdt-issue` `{draft_id}`), `Prep USDT Edit` (Set Awaiting Edit + prompt) + `Call USDT Edit` (→ `/usdt-edit` `{draft_id, feedback}`), `Call USDT Tax` (→ `/usdt-tax` `{draft_id}`), `USDT Cancel Reply` (edit the card to "cancelled"). Model each httpRequest on `Disarm Autosend` (Hermes Bridge cred, `http://172.18.0.1:8788/...`).
  - Connections: `Route Action[usdtquote] → Prep USDT → Set Awaiting USDT → Edit Telegram (Amount Prompt)`; in `Build Paylink From Reply`'s downstream, insert an IF on `rail=='usdt'` → `Call USDT Quote` (else existing `Call Nomod (Reply)`); `Route Action[usdtissue] → Call USDT Issue`; `[usdtedit] → Prep USDT Edit`; the edit text reply → `Call USDT Edit`; `[usdtcancel] → USDT Cancel Reply`; `[usdtreceived] → Call USDT Tax`.
  - Write `nodes_new`, `conns_new`, and a dollar-quoted `apply_usdtflow.sql` updating BOTH `workflow_entity` and `workflow_history` (versionId `b68eb9c4…`).

- [ ] **Step 3: Run the script + python-diff check**
```bash
scp ~/.../build_usdt_flow.py dubriani-ec2:/home/ubuntu/reroute-revert/
ssh dubriani-ec2 'cd /home/ubuntu/reroute-revert && python3 build_usdt_flow.py'
```
Expected output: `CHANGED nodes: ['Queue & Format','Auto Prep','Route Action','Build Paylink From Reply'] ADDED: ['Prep USDT','Set Awaiting USDT','Call USDT Quote','Call USDT Issue','Prep USDT Edit','Call USDT Edit','Call USDT Tax','USDT Cancel Reply']`, `REMOVED: []`, `send-customer count: 4` (unchanged), conns changed keys include `Route Action` + the branch node.

- [ ] **Step 4: Inspect the diff** — confirm no `Parse Callback` change and the 4 send nodes untouched. If wrong, fix the script and re-run (idempotent; no DB write yet).

- [ ] **Step 5: Commit the script** (to the revert dir on box; not the repo).

---

## Task 10: n8n — apply, restart, and real-tap e2e on the test number

**Files:** Postgres (apply) + runtime verify

- [ ] **Step 1: Apply to both tables**
```bash
ssh dubriani-ec2 'PGU=$(docker exec n8n-postgres-1 printenv POSTGRES_USER); PGD=$(docker exec n8n-postgres-1 printenv POSTGRES_DB); docker exec -i n8n-postgres-1 psql -U "$PGU" -d "$PGD" < /home/ubuntu/reroute-revert/apply_usdtflow.sql'
```
Expected: `BEGIN / UPDATE 1 / UPDATE 1 / COMMIT`.

- [ ] **Step 2: Verify sync + restart**
```bash
ssh dubriani-ec2 'PGU=...; PGD=...; echo "entity==history nodes: $(test "$(docker exec n8n-postgres-1 psql -U "$PGU" -d "$PGD" -tAc "SELECT md5(nodes::text) FROM workflow_entity WHERE id=...")" = "$(... workflow_history ... versionId b68eb9c4 ...)" && echo YES)"; docker restart n8n-n8n-1'
```
Expected: `entity==history nodes: YES`; n8n restarts; `active=t`; no workflow load error in `docker logs --since 40s n8n-n8n-1`.

- [ ] **Step 3: Arm the watcher**
```bash
ssh dubriani-ec2 "timeout 900 stdbuf -oL journalctl --user -u hermes-bridge -f -n 0 | grep -m1 'usdt proforma\|USDT proforma'" &
```

- [ ] **Step 4: Real-tap e2e** — on the test number's card: tap 🪙 USDT → type `5000` → verify the **preview card** shows the breakdown → tap ✏️ Edit → type "add catering 500" → verify the **re-preview** shows 2 line items + AED 5,775 total + new USDT figure → tap ✅ Send → confirm the **customer (test number) receives the proforma PDF (crypto block, no card fee) + the USDT text** (byte-perfect address) → tap 💵 USDT received → confirm the **tax invoice #3333** PDF arrives. Fetch the delivered messages from WAHA to confirm byte-perfect (as in the 2026-06-18 verification).

- [ ] **Step 5: Revert-readiness note** — if any step fails: restore `nodes_pre_usdtflow.json`/`conns_pre_usdtflow.json` into both tables + `docker restart n8n-n8n-1`; restore `*.bak-usdtflow` bridge files + restart.

---

## Task 11: Persist

**Files:** repo + STATE + memory + Drive

- [ ] **Step 1: Commit bridge** — on the box: `git add invoice/invoice_gen.py invoice_hooks.py routes.py server.py test_usdt_invoice.py test_usdt_routes.py && git commit -m "feat(usdt): USDT-only invoice flow (proforma+USDT, operator preview/edit, tax on receipt)"`; sync the Mac mirror (`git fetch origin && git reset --hard origin/main`).
- [ ] **Step 2: Re-export workflow** → `dubriani-ai-agent/workflows/phase-1b-telegram.json` (normalize: 7 keys in order, indent=2, ensure_ascii=False, trailing newline) → commit. Verify the diff adds the 🪙 button + the 5 Route Action rules + the new nodes, send-customer still 4.
- [ ] **Step 3: Update STATE.md** — new section "USDT-only invoice flow LIVE" (endpoints, the preview/edit gate, USDT=×1.05, tax-on-received, commits, reverts: `*.bak-usdtflow` + `nodes_pre_usdtflow`).
- [ ] **Step 4: Update memory** — extend `dubriani_phase2_golive_2026_06_18.md` (or a new note) + MEMORY.md index; `rclone copy` memory → Drive.
- [ ] **Step 5: Clean up** box build artifacts (keep `*_pre_usdtflow` revert).

---

## Self-Review

**Spec coverage:** trigger button (T9) ✓; price entry + awaiting branch (T9) ✓; itemized preview to operator only (T5) ✓; free-text edit, LLM line-items-only (T6) ✓; ✅ → proforma PDF + USDT text (T7) ✓; USDT=ceil(total×1.05/3.6725) (T3) ✓; proforma#random / tax#3333 (T4) ✓; crypto block on PDF (T2) ✓; ×1.05 no card fee (T1) ✓; hard invariant — address env-only, LLM forbidden from addresses + guard recheck (T6) ✓; tax on 💵 received (T7,T10) ✓; reversibility (T8–T10) ✓.

**Placeholder scan:** none — every code step has real code; the n8n build (T9) reuses the proven `build_usdt_button.py` shape with the exact node/rule/connection list enumerated.

**Type consistency:** `compute_usdt_quote` returns `{subtotal,vat,total_aed,usdt_amount,address}` — used identically in T4/T5/T7. `issue_usdt_proforma(cid, name, items, payment_ref="")` / `issue_usdt_tax(...)` signatures match between T4 (def) and T7 (call). `/send-usdt` is called with `amount=total_aed` (T7) consistent with the existing `ceil(amount/3.6725)` handler. Redis key `hermes:usdtq:<draft_id>` consistent across T5/T6/T7.

**Note on T9 risk:** the amount-reply `rail=usdt` branch is the one touch to the live amount path (an IF after `Build Paylink From Reply`); snapshot + diff + revert cover it. Everything else is additive.
