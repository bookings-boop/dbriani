# Customer Context Header — Implementation Plan

> ✅ **STATUS: IMPLEMENTED, DEPLOYED & TESTED — 2026-05-22.** All 9 tasks
> complete; all 4 end-to-end tests passed. Open questions resolved per
> recommendation (Q1 = 15s Hermes timeout). Two build-time corrections to the
> plan: (a) `extract_customer_facts` dropped its two unused params; (b) because
> the `Customer Facts` httpRequest sits in the chain, `Queue & Format` resolves
> its draft data from `$('Parse Response')` / `$('Parse Lead Response')`
> explicitly (the plan's Task 6 originally assumed `$input` still worked). See
> `docs/feature-backlog.md` for the shipped summary.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (or
> subagent-driven-development) to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show a structured customer-context header (name, dates, yachts,
party size, message #) above every approval-mode draft card in Telegram.

**Architecture:** A new Postgres table `customer_facts` (one row per customer)
is maintained by a new token-gated bridge endpoint `POST /customer-facts`. A
new workflow httpRequest node calls it per inbound draft; `Queue & Format`
prepends the returned header block to the draft card. Fact extraction is a
gated Hermes call — first message always, later messages only when a cheap
heuristic says the message likely carries a new fact.

**Tech stack:** Python stdlib bridge (`hermes-bridge/server.py`), Postgres
(`docker exec` via `_psql`), n8n workflow (deployed via `scripts/n8n_deploy.py`),
Hermes CLI for extraction.

---

## ⚠️ Critical premise correction — read before estimating

The task brief says *"`/draft` response gains `customer_header` field."* **This
premise is wrong.** Investigation of the live workflow (`azPIy9OcDwiPV5uY`, 97
nodes) found:

- The approval-mode draft pipeline is
  `Build Prompt → Claude AI → Parse Response → Queue & Format →
  Send Draft to Telegram → Save Telegram MsgID`.
- **`Claude AI` is an httpRequest node calling `https://api.anthropic.com/v1/messages`
  directly.** The workflow does **not** call the bridge `/draft` endpoint at
  all. The bridge serves only `/improve`, `/learn`, `/rules`, `/set-mode`,
  `/autosend-check`, `/autosend-state`, `/caps`. The `_draft` handler in
  `server.py` is dead code in the live system.

**Consequence:** there is no `/draft` response to extend. The feature needs its
own **new bridge endpoint** (`POST /customer-facts`) and its own **new workflow
node** that calls it. The helper functions named in the brief
(`extract_customer_facts`, `upsert_customer_facts`, `get_customer_facts`,
`build_customer_header`) are still correct — they become the internals of that
new endpoint. This plan is written against the corrected architecture.

A second wording fix: the brief says *"workflow node that posts draft cards
prepends header."* The node that **composes** the card text is `Queue & Format`
(a Code node); `Send Draft to Telegram` only transmits `$json.telegram_payload`.
The header is prepended in **`Queue & Format`**.

---

## 1. Prerequisites

| # | Prerequisite | Owner | Notes |
|---|---|---|---|
| P1 | Create the `customer_facts` table + grant `hermes_rw` (Task 1). | Operator / superuser | `hermes_rw` cannot run DDL — must run as the Postgres superuser `n8n`. |
| P2 | Bridge reachable + deployable. | — | `scripts/deploy_bridge.py` already works; the bridge is boot-safe. |
| P3 | Workflow deployable via `scripts/n8n_deploy.py` `safe_put`. | — | Already the sanctioned path. |
| P4 | `BRIDGE_FACTS_TIMEOUT` decided (see Open Question Q1). | Operator | Defaults to 15s in this plan; the brief's 5s is unrealistic for a Hermes call. |
| P5 | n8n credential `Hermes Bridge` (`IgIcvPoibuAayVDx`, httpHeaderAuth). | — | Already exists; the new node reuses it. |

No new infrastructure, no new container, no new credential.

---

## 2. Architecture

### 2.1 Components

| Component | Where | Responsibility |
|---|---|---|
| `customer_facts` table | Postgres `n8n` DB | One row per customer: name, dates, yachts, party_size, message_count, timestamps. |
| `_facts_extract_gate()` | `server.py` | Pure heuristic — should a non-first message trigger extraction? |
| `extract_customer_facts()` | `server.py` | Hermes call → `{name,dates,yachts,party_size}` (or `None` on any failure). |
| `get_customer_facts()` | `server.py` | SELECT the row → dict or `None`. |
| `upsert_customer_facts()` | `server.py` | UPSERT the row; **atomic** `message_count` increment in SQL; `RETURNING message_count`. |
| `build_customer_header()` | `server.py` | Pure function — facts dict → the multi-line header string. |
| `POST /customer-facts` | `server.py` | Orchestrates the above; **fail-safe** — always 200 with a best-effort header. |
| `Customer Facts` node | n8n workflow | httpRequest → `/customer-facts`; `onError: continueRegularOutput`. |
| `Queue & Format` (modified) | n8n workflow | Prepends `customer_header` to the non-lead draft card; guarded. |
| system-prompt §5 line | `system-prompt.md` | Tells Maria to ask the name naturally after 2-3 exchanges. |

### 2.2 Data flow (per inbound customer message, approval mode)

```
WhatsApp inbound
  → Build Prompt  (customerPhone, customerName, userMessage, conversationHistory)
  → Claude AI     (draft, direct to Anthropic — unchanged)
  → Parse Response
  → Customer Facts  [NEW httpRequest → POST bridge /customer-facts]
        bridge: get_customer_facts → gate decision → (extract?) → upsert
                → build_customer_header → 200 {customer_header, message_count}
  → Queue & Format  [MODIFIED — prepends customer_header to the card]
  → Send Draft to Telegram   (card now shows the header)
  → Save Telegram MsgID
```

`Customer Facts` is inserted **only** on the `Parse Response → Queue & Format`
edge. The `/lead` path (`Parse Lead Response → Queue & Format`) is left
physically untouched; `Queue & Format` guards the `$('Customer Facts')`
reference so the lead path (where the node never ran) falls back cleanly.

### 2.3 Extraction gating

- **First message** (no `customer_facts` row) → **always extract**.
- **Subsequent message** → extract only if `_facts_extract_gate(message)` is
  true: the message contains a digit, a known yacht name, a date/time word, a
  self-introduction pattern, or a booking keyword. Otherwise **skip
  extraction** — just increment `message_count`.
- **Refine** (`is_refine=true`) → never extract, never increment; header is
  built from cached facts only (a refine re-renders, it is not a new inbound).

### 2.4 Fact merge rule

`extract_customer_facts` is given the **whole history + the new message**, so it
returns the *complete current picture* (it lists every yacht ever mentioned,
etc.). The merge into the stored row is: **a non-empty extracted field
overwrites; an empty extracted field keeps the cached value.** A failed or
partial extraction therefore never erases a known fact.

### 2.5 Fail-safe contract (hard requirement from the brief)

Header logic must **never** block draft posting.

- The bridge `_customer_facts` handler wraps everything in `try/except`; on any
  error it logs, falls back to cached facts (or an empty header), and **still
  returns HTTP 200** with a `degraded: true` flag.
- The `Customer Facts` workflow node is set `onError: continueRegularOutput` —
  a bridge timeout/5xx/network error still passes an item to `Queue & Format`.
- `Queue & Format` reads the header inside a `try/catch`; if `Customer Facts`
  did not run or returned nothing, it falls back to the **legacy mini-header**
  (`👤 name` + history line) and posts the card normally.

So there are three independent fallbacks; a draft card is always posted.

### 2.6 Why "don't touch" is satisfied

- **FR-5 improver:** untouched. `run_hermes` gains an optional `timeout` param
  with a default equal to today's value — every existing caller is byte-identical.
- **Autonomous mode:** no autonomous node is modified. `Customer Facts` does run
  on the shared pre-card path for autonomous conversations too, but that only
  maintains facts (harmless) — `Render Auto Card` composes its own card text, so
  autonomous cards are visually unchanged.
- **`/lead`, `/send`, `/caps`, `/manual`:** no node on those paths is modified.
  `Customer Facts` is spliced into the `Parse Response → Queue & Format` edge
  only; the `Parse Lead Response → Queue & Format` edge is left as-is.

---

## 3. Files touched

| File | Change |
|---|---|
| `hermes-bridge/server.py` | +6 functions, +1 endpoint, +1 route, `run_hermes` gains `timeout=` param, +constants. |
| `hermes-bridge/test_customer_facts.py` | **New** — assert-based unit tests for the two pure functions. |
| `system-prompt.md` | +1 line in §5 (ask name naturally). |
| `scripts/build_customer_header.py` | **New** — workflow build script (adds `Customer Facts` node, rewires, edits `Queue & Format`). |
| `workflows/phase-1b-telegram.json` | Re-synced after deploy (98 nodes). |
| `docs/feature-backlog.md` | Mark the feature shipped (post-build). |

DB: `customer_facts` table created out-of-band (Task 1).

---

## 4. Implementation tasks

### Task 1 — Create the `customer_facts` table

**Files:** none in-repo (DDL run against Postgres).

- [ ] **Step 1: Run the DDL as the Postgres superuser `n8n`.**

`_psql` connects as `hermes_rw`, which has no DDL rights — run this as `n8n`:

```bash
ssh dubriani-ec2 "docker exec n8n-postgres-1 psql -U n8n -d n8n -c \"
CREATE TABLE IF NOT EXISTS customer_facts (
  customer_id   text PRIMARY KEY,
  name          text,
  dates         text,
  yachts        text,
  party_size    text,
  message_count integer NOT NULL DEFAULT 0,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);
GRANT SELECT, INSERT, UPDATE ON customer_facts TO hermes_rw;\""
```

The four text fact columns are **nullable** (no `NOT NULL`): `_lit()` renders an
unknown fact (`""`/`None`) as SQL `NULL`, and `get_customer_facts` /
`build_customer_header` already treat `NULL`/`""` identically.

- [ ] **Step 2: Verify the grant works as `hermes_rw`.**

```bash
ssh dubriani-ec2 "docker exec n8n-postgres-1 psql -U hermes_rw -d n8n -tA -c \
  \"INSERT INTO customer_facts (customer_id,name) VALUES ('__t__','t')
    ON CONFLICT (customer_id) DO UPDATE SET name='t'
    RETURNING message_count; DELETE FROM customer_facts WHERE customer_id='__t__';\""
```
Expected: prints `0`, no permission error. (Note: `hermes_rw` has no DELETE
grant elsewhere — add `GRANT DELETE` for this table if the cleanup line errors,
or just leave the `__t__` row; it is harmless.)

### Task 2 — Bridge: pure helper functions + unit tests

**Files:** Modify `hermes-bridge/server.py` · Create `hermes-bridge/test_customer_facts.py`

- [ ] **Step 1: Add constants + regexes** after the cap constants (`server.py`
  ~line 60). `YACHT_NAMES` is curated from `system-prompt.md §7` — extend it to
  the full catalog during implementation:

```python
FACTS_EXTRACT_TIMEOUT = int(os.environ.get("BRIDGE_FACTS_TIMEOUT", "15"))
YACHT_NAMES = ("satoshi", "aurora", "oryx", "lotus", "majesty", "nomad",
               "serenity", "sunseeker", "azimut", "ferretti")  # extend per §7
_FACTS_DATE_RE = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|january|february|"
    r"march|april|june|july|august|september|october|november|december|"
    r"mon|tue|wed|thu|fri|sat|sun|monday|tuesday|wednesday|thursday|friday|"
    r"saturday|sunday|today|tomorrow|tonight|weekend|week|month)\b",
    re.IGNORECASE)
_FACTS_NAME_RE = re.compile(
    r"\b(i'?m |i am |my name|this is |call me |name'?s )", re.IGNORECASE)
_FACTS_BOOK_RE = re.compile(
    r"\b(book|booking|reserve|charter|deposit|confirm|pay|payment|guests?|"
    r"pax|people|persons?|birthday|proposal|anniversary|wedding|corporate)\b",
    re.IGNORECASE)
```

- [ ] **Step 2: Write the unit-test file** `hermes-bridge/test_customer_facts.py`
  (assert-based — the project uses no test framework):

```python
#!/usr/bin/env python3
"""Unit tests for the pure customer-facts helpers. Run: python3 this_file."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from server import _facts_extract_gate, build_customer_header


def test_gate():
    assert _facts_extract_gate("we are 6 people") is True          # digit
    assert _facts_extract_gate("interested in the Satoshi") is True  # yacht
    assert _facts_extract_gate("can we do Saturday?") is True        # date
    assert _facts_extract_gate("hi, I'm Mark") is True               # name
    assert _facts_extract_gate("we want to book") is True            # booking
    assert _facts_extract_gate("ok thanks!") is False                # nothing
    assert _facts_extract_gate("") is False
    assert _facts_extract_gate(None) is False


def test_header_full():
    h = build_customer_header({"name": "Mark Hassan", "dates": "Saturday Dec 14",
        "yachts": "Aurora II, Satoshi", "party_size": "6-8 guests",
        "message_count": 4})
    assert "👤 Mark Hassan" in h
    assert "📅 Interested in: Saturday Dec 14" in h
    assert "🛥️ Looking at: Aurora II, Satoshi" in h
    assert "👥 Party size: 6-8 guests" in h
    assert "🔢 Message #4 in conversation" in h
    assert h.rstrip().endswith("─" * 30)


def test_header_sparse():
    h = build_customer_header({"name": "", "dates": "", "yachts": "",
        "party_size": "", "message_count": 1})
    assert "👤 New contact" in h           # name fallback
    assert "🔢 Message #1 in conversation" in h
    assert "📅" not in h and "🛥️" not in h  # empty facts omit their lines


if __name__ == "__main__":
    for fn in (test_gate, test_header_full, test_header_sparse):
        fn(); print("PASS", fn.__name__)
    print("all customer-facts unit tests passed")
```

- [ ] **Step 3: Run the tests — confirm they FAIL** (functions not defined yet):

```bash
python3 hermes-bridge/test_customer_facts.py
```
Expected: `ImportError: cannot import name '_facts_extract_gate'`.

- [ ] **Step 4: Implement `_facts_extract_gate` + `build_customer_header`**
  (add to `server.py` after the regexes):

```python
def _facts_extract_gate(incoming_message):
    """Heuristic: should this (non-first) message trigger a fresh extraction?
    True if it plausibly carries a new fact."""
    m = (incoming_message or "").lower()
    if not m:
        return False
    if any(ch.isdigit() for ch in m):
        return True
    if any(y in m for y in YACHT_NAMES):
        return True
    return bool(_FACTS_DATE_RE.search(m) or _FACTS_NAME_RE.search(m)
                or _FACTS_BOOK_RE.search(m))


def build_customer_header(facts):
    """facts: {name,dates,yachts,party_size,message_count} -> header string.
    Each optional line is shown only when its fact is non-empty; the name and
    message-count lines and the divider are always present."""
    f = facts or {}
    name = str(f.get("name") or "").strip()
    lines = ["👤 " + (name or "New contact")]
    if str(f.get("dates") or "").strip():
        lines.append("📅 Interested in: " + str(f["dates"]).strip())
    if str(f.get("yachts") or "").strip():
        lines.append("🛥️ Looking at: " + str(f["yachts"]).strip())
    if str(f.get("party_size") or "").strip():
        lines.append("👥 Party size: " + str(f["party_size"]).strip())
    try:
        mc = int(f.get("message_count") or 0)
    except (TypeError, ValueError):
        mc = 0
    lines.append("🔢 Message #" + str(mc) + " in conversation")
    lines.append("─" * 30)
    return "\n".join(lines)
```

- [ ] **Step 5: Run the tests — confirm they PASS:**

```bash
python3 hermes-bridge/test_customer_facts.py
```
Expected: `PASS test_gate / test_header_full / test_header_sparse / all ... passed`.

- [ ] **Step 6: Commit.**

```bash
git add hermes-bridge/server.py hermes-bridge/test_customer_facts.py
git commit -m "feature-header: customer-facts gate + header builder (pure fns + tests)"
```

### Task 3 — Bridge: DB + Hermes helpers

**Files:** Modify `hermes-bridge/server.py`

- [ ] **Step 1: Make `run_hermes` accept an optional timeout.** Change the
  signature (line 557) — the default keeps every existing caller identical:

```python
def run_hermes(query, timeout=None):
    if timeout is None:
        timeout = HERMES_TIMEOUT
    cmd = [HERMES, "--profile", "default", "chat", "-q", query, "-Q",
           "--source", "tool", "--yolo", "-t", "memory"]
    log("hermes call:", " ".join(c for c in cmd if c != query))
    env = dict(os.environ)
    env["PATH"] = os.path.dirname(HERMES) + os.pathsep + env.get("PATH", "")
    t0 = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          timeout=timeout, env=env)
    return proc.returncode, proc.stdout, proc.stderr, int((time.time() - t0) * 1000)
```

- [ ] **Step 2: Add `get_customer_facts`** (model: `fetch_behavior_rules`;
  `_psql` uses `-tA`, so columns are `|`-separated):

```python
def get_customer_facts(customer_id):
    """The customer_facts row as a dict, or None if absent / on error."""
    cid = (customer_id or "").replace("'", "''")
    sql = ("SELECT name, dates, yachts, party_size, message_count "
           f"FROM customer_facts WHERE customer_id = '{cid}'")
    try:
        out, err = _psql(sql)
        if err:
            log("customer_facts fetch failed:", err)
            return None
        line = (out or "").strip()
        if not line:
            return None
        p = line.split("|")
        if len(p) < 5:
            return None
        return {"name": p[0], "dates": p[1], "yachts": p[2],
                "party_size": p[3],
                "message_count": int(p[4]) if p[4].strip().isdigit() else 0}
    except Exception as e:
        log("customer_facts fetch error:", repr(e))
        return None
```

- [ ] **Step 3: Add `upsert_customer_facts`** — the `message_count` increment
  is atomic in SQL (no read-modify-write race), returns the new count:

```python
def upsert_customer_facts(customer_id, name, facts):
    """UPSERT customer_facts. INSERT -> message_count 1; ON CONFLICT ->
    message_count = existing + 1 (atomic). Returns (new_count, None) or
    (None, error)."""
    sql = (
        "INSERT INTO customer_facts (customer_id, name, dates, yachts, "
        "party_size, message_count, updated_at) VALUES ("
        + ", ".join([_lit(customer_id), _lit(name), _lit(facts.get("dates")),
                     _lit(facts.get("yachts")), _lit(facts.get("party_size"))])
        + ", 1, now()) ON CONFLICT (customer_id) DO UPDATE SET "
        "name = EXCLUDED.name, dates = EXCLUDED.dates, "
        "yachts = EXCLUDED.yachts, party_size = EXCLUDED.party_size, "
        "message_count = customer_facts.message_count + 1, updated_at = now() "
        "RETURNING message_count"
    )
    try:
        out, err = _psql(sql)
        if err:
            return None, err
        v = (out or "").strip().splitlines()
        return (int(v[0]) if v and v[0].strip().isdigit() else None), None
    except Exception as e:
        return None, repr(e)
```

> Note: `_lit` renders `None`/`""` as SQL `NULL` — fine here, the four fact
> columns are nullable (Task 1 DDL). An unknown fact stores `NULL`;
> `get_customer_facts` reads it back as `""` (`psql -tA` renders `NULL` empty)
> and `build_customer_header` omits its line. No `or ""` coercion needed.

- [ ] **Step 4: Add `extract_customer_facts`** (Hermes call, short timeout):

```python
def extract_customer_facts(customer_id, customer_name, incoming_message, history):
    """Hermes call: extract {name,dates,yachts,party_size} from the message +
    history. Returns a dict, or None on timeout/error/bad output (caller then
    falls back to cached facts)."""
    q = (
        "TASK: From the WhatsApp conversation below, extract the customer's "
        "current yacht-charter booking facts for an internal CRM header. "
        "Return ONLY a JSON object with exactly these keys, using an empty "
        'string "" for anything not yet known (never guess):\n'
        '{"name":"","dates":"","yachts":"","party_size":""}\n'
        "- name: the customer's first/full name if they have given it\n"
        "- dates: charter date(s) of interest, short (e.g. \"Sat Dec 14\")\n"
        "- yachts: every yacht name discussed, comma-separated\n"
        "- party_size: group size (e.g. \"6-8 guests\")\n"
        "Consider the WHOLE conversation, not just the latest line. Output "
        "only the JSON object — no markdown, no commentary.\n\n"
        "--- CONVERSATION ---\n" + (history or "(no prior history)") +
        "\n\n--- LATEST MESSAGE ---\n" + (incoming_message or ""))
    try:
        rc, out, err, elapsed = run_hermes(q, timeout=FACTS_EXTRACT_TIMEOUT)
    except subprocess.TimeoutExpired:
        log(f"extract_customer_facts: hermes timeout {FACTS_EXTRACT_TIMEOUT}s")
        return None
    except Exception as e:
        log("extract_customer_facts: hermes error", repr(e))
        return None
    if rc != 0:
        log(f"extract_customer_facts: hermes rc={rc} err={err[:200]!r}")
        return None
    parsed, _ = extract_json(out)
    if not isinstance(parsed, dict):
        log("extract_customer_facts: no JSON in hermes output")
        return None
    return {k: str(parsed.get(k) or "").strip()
            for k in ("name", "dates", "yachts", "party_size")}
```

- [ ] **Step 5: Add `_merge_facts`** (the §2.4 merge rule):

```python
def _merge_facts(cached, extracted):
    """Non-empty extracted fields win; empty ones keep the cached value."""
    cached = cached or {}
    out = {}
    for k in ("name", "dates", "yachts", "party_size"):
        ev = str((extracted or {}).get(k, "") or "").strip()
        out[k] = ev or str(cached.get(k) or "")
    return out
```

- [ ] **Step 6: Compile-check + commit.**

```bash
python3 -c "import ast; ast.parse(open('hermes-bridge/server.py').read())"
git add hermes-bridge/server.py
git commit -m "feature-header: customer-facts DB + Hermes extraction helpers"
```

### Task 4 — Bridge: the `/customer-facts` endpoint

**Files:** Modify `hermes-bridge/server.py`

- [ ] **Step 1: Register the route.** In `do_POST` (line 611) add
  `/customer-facts` to the allowed-paths tuple, and add the dispatch branch
  after the `/caps` branch (line 638):

```python
        elif self.path == "/customer-facts":
            self._customer_facts(payload)
```

- [ ] **Step 2: Implement the handler** (add as a `Handler` method, e.g. after
  `_autosend_state`). Fail-safe per §2.5 — always 200:

```python
    def _customer_facts(self, payload):
        """feature-header: maintain customer_facts + return a context header.
        Fail-safe — ANY error degrades to cached facts and still returns 200."""
        cid = (payload.get("customer_id") or "").strip()
        cname = (payload.get("customer_name") or "").strip()
        msg = payload.get("incoming_message") or ""
        history = payload.get("history") or ""
        is_refine = bool(payload.get("is_refine"))
        try:
            cached = get_customer_facts(cid)
            if is_refine:
                facts = cached or {}
                mc = (cached or {}).get("message_count", 0)
                hdr = build_customer_header({
                    "name": facts.get("name") or cname,
                    "dates": facts.get("dates", ""),
                    "yachts": facts.get("yachts", ""),
                    "party_size": facts.get("party_size", ""),
                    "message_count": mc})
                self._send(200, {"ok": True, "extracted": False,
                                 "message_count": mc, "customer_header": hdr})
                return
            do_extract = (cached is None) or _facts_extract_gate(msg)
            if do_extract:
                merged = _merge_facts(cached, extract_customer_facts(
                    cid, cname, msg, history))
            else:
                merged = _merge_facts(cached, None)
            if not (merged.get("name") or "").strip():
                merged["name"] = cname
            new_count, err = upsert_customer_facts(cid, merged["name"], merged)
            if err:
                log("customer_facts upsert failed:", err)
            mc = new_count if isinstance(new_count, int) \
                else ((cached or {}).get("message_count", 0) + 1)
            hdr = build_customer_header({**merged, "message_count": mc})
            log(f"customer-facts cid={cid!r} extract={do_extract} msg#{mc}")
            self._send(200, {"ok": True, "extracted": bool(do_extract),
                             "message_count": mc, "customer_header": hdr})
        except Exception as e:
            log("customer_facts ERROR:", repr(e))
            cached = None
            try:
                cached = get_customer_facts(cid)
            except Exception:
                pass
            mc = (cached or {}).get("message_count", 0)
            hdr = build_customer_header({
                "name": (cached or {}).get("name") or cname,
                "dates": (cached or {}).get("dates", ""),
                "yachts": (cached or {}).get("yachts", ""),
                "party_size": (cached or {}).get("party_size", ""),
                "message_count": mc})
            self._send(200, {"ok": True, "extracted": False, "degraded": True,
                             "message_count": mc, "customer_header": hdr})
```

- [ ] **Step 3: Compile-check + commit.**

```bash
python3 -c "import ast; ast.parse(open('hermes-bridge/server.py').read())"
git add hermes-bridge/server.py
git commit -m "feature-header: POST /customer-facts endpoint (fail-safe)"
```

### Task 5 — Deploy the bridge + endpoint smoke test

**Files:** none (deploy only)

- [ ] **Step 1: Deploy.** `python3 scripts/deploy_bridge.py` (backs up the box
  copy, uploads `server.py` + `system-prompt.md`, compile-checks, restarts the
  `hermes-bridge` user service, verifies `/health`).

- [ ] **Step 2: Smoke-test the endpoint over SSH** (needs the bridge token —
  read it from the box's service environment). First message (no row → extract):

```bash
ssh dubriani-ec2 'curl -s -X POST localhost:8788/customer-facts \
  -H "X-Bridge-Token: $BRIDGE_TOKEN" -H "Content-Type: application/json" \
  -d "{\"customer_id\":\"__smoke__\",\"customer_name\":\"Test\",
       \"incoming_message\":\"hi we are 6 people for Saturday on the Satoshi\",
       \"history\":\"\"}"'
```
Expected: `{"ok":true,"extracted":true,"message_count":1,"customer_header":"👤 ..."}`.

- [ ] **Step 3: Second call, no keywords → skip extraction, count increments:**

```bash
ssh dubriani-ec2 'curl -s -X POST localhost:8788/customer-facts \
  -H "X-Bridge-Token: $BRIDGE_TOKEN" -H "Content-Type: application/json" \
  -d "{\"customer_id\":\"__smoke__\",\"incoming_message\":\"ok thanks\"}"'
```
Expected: `"extracted":false,"message_count":2`.

- [ ] **Step 4: Clean up the smoke row:**

```bash
ssh dubriani-ec2 "docker exec n8n-postgres-1 psql -U n8n -d n8n -c \
  \"DELETE FROM customer_facts WHERE customer_id='__smoke__';\""
```

### Task 6 — Workflow: build script for the `Customer Facts` node + `Queue & Format`

**Files:** Create `scripts/build_customer_header.py`

Model on `scripts/build_bug2_caps_command.py`. The script:
1. adds the `Customer Facts` httpRequest node,
2. rewires `Parse Response → Customer Facts → Queue & Format`,
3. replaces the `Queue & Format` jsCode to prepend the header.

- [ ] **Step 1: Write `scripts/build_customer_header.py`.** Node definition:

```python
def customer_facts_node():
    body = ('={ "customer_id": {{ JSON.stringify($(\'Parse Response\').item.json.customer_phone) }}, '
            '"customer_name": {{ JSON.stringify($(\'Parse Response\').item.json.customer_name) }}, '
            '"incoming_message": {{ JSON.stringify($(\'Parse Response\').item.json.user_message) }}, '
            '"history": {{ JSON.stringify($(\'Parse Response\').item.json.conversation_history) }} }')
    return {
        "parameters": {
            "method": "POST",
            "url": "http://172.18.0.1:8788/customer-facts",
            "authentication": "genericCredentialType",
            "genericAuthType": "httpHeaderAuth",
            "sendHeaders": True,
            "headerParameters": {"parameters": [
                {"name": "Content-Type", "value": "application/json"}]},
            "sendBody": True, "specifyBody": "json", "jsonBody": body,
            "options": {},
        },
        "credentials": {"httpHeaderAuth": {"id": "IgIcvPoibuAayVDx",
                                           "name": "Hermes Bridge"}},
        "id": str(uuid.uuid4()), "name": "Customer Facts",
        "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
        "position": position,   # see Step 1a
        "onError": "continueRegularOutput",
    }
```

- [ ] **Step 1a: Compute `position`.** Read the live `Parse Response` and
  `Queue & Format` node `position` arrays; set the new node's `position` to
  their midpoint (`[(x1+x2)//2, (y1+y2)//2]`). Position is cosmetic canvas
  layout only — it does not affect execution.

- [ ] **Step 2: Rewire** (same pattern as `build_bug2_caps_command.py`):
  re-point the `Parse Response [out 0] → Queue & Format` edge to
  `Customer Facts`, then add `Customer Facts → Queue & Format`. Assert the old
  edge exists first; abort if not.

- [ ] **Step 3: Replace the `Queue & Format` jsCode.** The only change is the
  non-lead branch — prepend the header, guard the reference. The new non-lead
  block (the rest of the node is byte-identical):

```javascript
} else {
  // feature-header: customer context header — guarded; never blocks the card
  let customerHeader = '';
  try { customerHeader = $('Customer Facts').item.json.customer_header || ''; }
  catch (e) { customerHeader = ''; }
  lines = ['📩 from ' + pending.customer_phone];
  if (customerHeader) {
    lines.push(customerHeader);            // header block (incl. its divider)
  } else {
    if (pending.customer_name) lines.push('👤 ' + pending.customer_name);
    lines.push(histLine);                  // legacy fallback
  }
  lines.push('', 'They said:', '"' + pending.customer_message + '"', '',
             '---', draftHeader, preview, '', '📝 Notes: ' + pending.notes);
}
```

  The build script does this as a guarded `jsCode.replace()` of the exact
  current `else { ... }` block (copy the current block verbatim from the live
  node as the anchor; abort if it does not match). The `is_lead` branch is
  untouched.

- [ ] **Step 4: Dry-run.** `python3 scripts/build_customer_header.py` — confirm
  anchors matched, node count `97 → 98`, nothing deployed.

### Task 7 — Deploy the workflow + sync

**Files:** Modify `workflows/phase-1b-telegram.json` (sync only)

- [ ] **Step 1: Deploy.** `python3 scripts/build_customer_header.py --deploy` —
  `safe_put` backs up to `PRE-CUSTOMERHEADER-*`, re-fetches staticData, PUTs,
  re-activates, verifies the draft queue did not shrink.
- [ ] **Step 2: Verify** the deploy printout: `Customer Facts` present,
  `nodes=98`, `active=True`.
- [ ] **Step 3: Re-sync `workflows/phase-1b-telegram.json`** (fetch live, key
  order `name,nodes,connections,pinData,active,settings,meta,tags`, exclude
  `staticData`) and secret-scan the diff.
- [ ] **Step 4: Commit.**

```bash
git add scripts/build_customer_header.py workflows/phase-1b-telegram.json
git commit -m "feature-header: Customer Facts node + header prepend in Queue & Format"
```

### Task 8 — system-prompt: ask the name naturally

**Files:** Modify `system-prompt.md`

- [ ] **Step 1: Add one line to §5 "Mandatory Qualification"** (after line 210,
  the rapport paragraph):

```markdown
If the customer has not given their name and you are 2-3 messages into the
conversation, ask for it naturally — e.g. "By the way, who am I speaking with?"
— woven into a normal reply, never as a standalone interrogation.
```

- [ ] **Step 2: Redeploy** `python3 scripts/deploy_bridge.py` (it uploads
  `system-prompt.md` too — the bridge reads it via `load_system_prompt`).

> Note: the live workflow's `Claude AI` node uses its own `systemPrompt` built
> by `Build Prompt` — confirm during implementation whether `Build Prompt`
> reads `system-prompt.md` or has its own copy. If `Build Prompt` carries a
> separate copy, the line must be added there too. **See Open Question Q4.**

- [ ] **Step 3: Commit.**

```bash
git add system-prompt.md
git commit -m "feature-header: prompt Maria to ask the customer's name naturally"
```

### Task 9 — End-to-end test + close out

- [ ] **Step 1: Live draft test.** From the test phone (+971509767187) send a
  fact-rich message ("Hi, I'm Mark, 6 of us, looking at the Satoshi for Dec 14").
  Inspect the n8n execution: `Customer Facts` ran (`out=1`), `Queue & Format`
  produced a `telegram_payload.text` starting with the header block. Confirm the
  Telegram draft card shows the 5-line header + divider.
- [ ] **Step 2: Skip-gate test.** Send a follow-up with no facts ("ok sounds
  good"). Confirm `Customer Facts` returned `extracted:false` and the header's
  `Message #` incremented.
- [ ] **Step 3: Regression — `/lead`.** `/lead Jane +971500000000 wants a sunset
  cruise` → confirm a lead card still posts, with no header and no error
  (`Queue & Format` lead branch + the `$('Customer Facts')` try/catch).
- [ ] **Step 4: Regression — refine + autonomous.** Reply-edit a draft (refine)
  → card still updates. Confirm an autonomous-mode draft card still renders via
  `Render Auto Card` unchanged.
- [ ] **Step 5: Fail-safe test.** Temporarily stop the bridge
  (`systemctl --user stop hermes-bridge`), send a test message → confirm the
  draft card still posts with the legacy fallback header, then restart the
  bridge.
- [ ] **Step 6: Update `docs/feature-backlog.md`** — record the feature shipped.
  Commit.

---

## 5. Risks

| # | Risk | Likelihood | Mitigation |
|---|---|---|---|
| R1 | **5s Hermes timeout is unrealistic** — the Hermes CLI cold-start + a Claude round-trip rarely finishes in 5s, so extraction would *always* time out and the feature would never populate facts. | High (if 5s is used) | Plan defaults `BRIDGE_FACTS_TIMEOUT=15`. See Q1 — get an explicit decision. Extraction is off the fail-safe path, so a longer timeout only costs latency, not safety. |
| R2 | **Extraction latency delays the draft card** — `Customer Facts` is in the linear chain before `Queue & Format`, so a gated-extract message adds the extraction time before the card appears. | Medium | Gating means most messages skip extraction (fast path). Accept for v1; parallelising `Customer Facts` with `Claude AI` is a possible v2 optimisation (adds n8n merge complexity — YAGNI for now). |
| R3 | **Wrong facts in the header** — Hermes mis-extracts a date/yacht/size. | Medium | The header is operator-facing and advisory only; the operator reviews every draft. Low harm. The merge rule never *erases* a good fact. |
| R4 | **`hermes_rw` missing the new grant** → every upsert fails. | Low | Task 1 Step 2 explicitly verifies the grant before any code ships. Degrades to a message-count-only header, not a crash. |
| R5 | **Lost-update on `message_count`** under rapid concurrent messages. | Low | The increment is atomic in SQL (`message_count = customer_facts.message_count + 1`). The count is advisory anyway. |
| R6 | **`Build Prompt` may hold its own system-prompt copy** — adding the line only to `system-prompt.md` would not reach the live draft. | Medium | Q4 — verify during Task 8; add the line in both places if so. |
| R7 | **n8n `$('Customer Facts')` reference throws on the lead path** (node never ran). | Low | `Queue & Format` reads it inside `try/catch`; the lead branch never reads it anyway. |

---

## 6. Rollback

Each piece is independently reversible; the pieces are additive.

- **Workflow:** `safe_put` saved `workflows/phase-1b-telegram.PRE-CUSTOMERHEADER-*.json`.
  Restore it with `scripts/rollback_workflow.py` (or re-PUT the backup). This
  removes the `Customer Facts` node and reverts `Queue & Format`.
- **Bridge:** `deploy_bridge.py` backed up the box's previous `server.py`.
  `git revert` the bridge commits and re-run `deploy_bridge.py`. `/customer-facts`
  is additive — nothing else calls it, so removal is clean.
- **system-prompt:** `git revert` the Task 8 commit, re-run `deploy_bridge.py`.
- **DB:** `customer_facts` is a standalone additive table — leaving it is
  harmless. Full revert: `DROP TABLE customer_facts;` as superuser `n8n`.
- **Order:** roll back workflow first (stops the calls), then bridge, then
  optionally the table.

---

## 7. Testing

| Layer | How | Covered in |
|---|---|---|
| Pure functions | `python3 hermes-bridge/test_customer_facts.py` — gate + header builder, full + sparse facts. | Task 2 |
| Bridge endpoint | `curl` over SSH: first-message extract, skip-gate, degraded path. | Task 5 |
| DB | `INSERT…ON CONFLICT…RETURNING` round-trip as `hermes_rw`; row inspection. | Tasks 1, 5 |
| Workflow wiring | Build-script dry-run (anchors, node count). | Task 6 |
| End-to-end | Live test-phone message → inspect the n8n execution + the Telegram card. | Task 9 |
| Regression | `/lead`, refine, autonomous card all still work. | Task 9 |
| Fail-safe | Bridge stopped → card still posts with the legacy header. | Task 9 |

No test framework is added — the project has none; tests follow the existing
idiom (assert-scripts + dry-runs + live execution inspection).

---

## 8. Time estimate

| Task | Estimate |
|---|---|
| 1 — DB table | 0.25 h (+ operator coordination for the superuser DDL) |
| 2 — pure helpers + unit tests | 1.5 h |
| 3 — DB + Hermes helpers | 1.5 h |
| 4 — `/customer-facts` endpoint | 0.75 h |
| 5 — bridge deploy + smoke test | 0.5 h |
| 6 — workflow build script | 1.5 h |
| 7 — workflow deploy + sync | 0.5 h |
| 8 — system-prompt line | 0.25 h |
| 9 — end-to-end testing | 1.25 h |
| **Total** | **~9.75 h focused work — roughly 1.5 working days** including review and the open-question round-trip. |

---

## 9. Open questions

**Q1 — Extraction timeout.** The brief says 5s. A Hermes CLI call realistically
needs 10-20s. **Recommendation:** set `BRIDGE_FACTS_TIMEOUT=15`. Alternative: do
extraction with a *direct* Anthropic API call to a fast model
(`claude-haiku-4-5`) instead of the Hermes CLI — predictable sub-5s latency, and
the workflow already calls Anthropic directly. Which does the operator want —
Hermes at 15s, or a direct haiku call kept near 5s?

**Q2 — Header vs. legacy mini-header.** `Queue & Format` already shows
`👤 name` + a history-count line. This plan replaces them with the new header
when facts are available. Confirm the operator wants the `📩 from <phone>` line
kept above the header (this plan keeps it).

**Q3 — `dates`/`yachts` accumulation.** Extraction sees the whole history and
returns the complete picture, so the latest extraction *is* the accumulated
list. Confirm that's the desired behaviour vs. an append-only history of dates.

**Q4 — Where does the live draft's system prompt come from?** Task 8 adds the
"ask name" line to `system-prompt.md`, but the live `Claude AI` node uses a
`systemPrompt` built by the `Build Prompt` node. If `Build Prompt` carries its
own copy of the prompt text (not read from `system-prompt.md`), the line must be
added there too. Must be resolved before Task 8.

**Q5 — Refine/regen cards.** This plan adds the header only to the initial draft
card (`Queue & Format`). Refined/regenerated cards are edited in place by
`Edit Telegram (Refine|Regen)` and will not gain the header. Confirm that is
acceptable, or add it as a follow-up.

**Q6 — Backfill.** Existing in-flight conversations have no `customer_facts`
row; their first message *after* deploy will be treated as "first message" and
extract from history (correct), but `message_count` will start at 1, not the
true conversation length. Acceptable? (This plan assumes yes — no backfill.)
