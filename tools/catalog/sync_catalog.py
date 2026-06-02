import json, re, hashlib, difflib, os

# ---- load the 5 live-extracted targets ----
nodes = json.loads(open('/tmp/dubriani_wf_nodes.json').read().strip())
def biggest_string(params):
    best=""
    def w(o):
        nonlocal best
        if isinstance(o,str):
            if len(o)>len(best): best=o
        elif isinstance(o,dict): [w(v) for v in o.values()]
        elif isinstance(o,list): [w(v) for v in o]
    w(params); return best
targets = {"system-prompt.md": open('/tmp/dubriani_system-prompt.md').read()}
for n in nodes:
    if n.get('name') in ["Build Prompt","Build Regen Prompt","Build Refine Prompt","Build Lead Prompt"]:
        targets[n['name']] = biggest_string(n['parameters'])

WATERSPORTS = """## 9.5 Watersports & Water Toys (B2C, +5% VAT)
Standalone water-toy rentals — delivered to your yacht. Quote ONE number, never a range; close with a question.

| Activity | Option | Price |
|---|---|---:|
| Jetski — Normal (Yamaha Standard / GP Supercharged / Sea-Doo) | 1 hr | AED 600 |
| Jetski — Supercharged GP | 1 hr | AED 1,000 |
| Jetcar (up to 2 adults; 4 on 1 hr) | 20 min / 30 min / 1 hr | AED 790 / 1,200 / 1,690 |
| eFoil | 1 hr / 2 hr | AED 1,500 / 2,500 |
| Seabob F5 | 1 hr | AED 1,200 |
| Seabob F9S (Sunseeker Satoshi only) | 1 hr | AED 2,000 |
| Flyfish | 1 hr / 2 hr | AED 1,200 / 2,200 |
| Flyboard | 20 min / 1 hr | AED 1,000 / 1,800 |
| Banana ride | 30 min / 1 hr | AED 800 / 1,200 |
| Donut ride | per session (2 packages) | AED 800 / 1,200 |
| Wakeboard | 1 hr | AED 1,200 |
| Deep-sea fishing (boat + crew + gear, private) | 4 hr | AED 2,500 |
| Self-driving boat | — | quote on request — flag for Zayn |

---

"""

# ---- the edit-set: (label, find, replace, expected_count) ----
EDITS = [
 ("E1 remove §1 B2B-partner section",
  "### B2B partner pricing (~50% off retail)\n\nWhen customer represents an agency / asks for B2B pricing:\n1. Request company license.\n2. Quote B2B prices (~50% off retail):\n - Satoshi: AED 1,500/hr B2B vs AED 3,000 B2C\n - Eclipse: AED 2,000/hr B2B vs AED 4,000 B2C\n - Add-ons also halved: e-foil 500 vs 1,000; slide 500 vs 1,000; shisha 250 vs 500\n3. Be patient — B2B partner relationships take weeks/months to convert.\n\n",
  "", 1),
 ("E2 remove notes_for_zayn B2B example",
  '"customer asked for B2B pricing — request license before quoting", ',
  "", 1),
 ("E3 remove Satoshi B2B rate row",
  "| B2B partner price | AED 1,000 + VAT | AED 12,000 + VAT |\n",
  "", 1),
 ("E4 Satoshi add-on table -> B2C only (eFoil 1,500)",
  "**B2B add-on prices (Satoshi only):**\n| Add-on | B2B | B2C |\n|---|---:|---:|\n| E-foil 1hr | AED 500 | AED 1,000 |\n| Electronic Shisha | AED 250 | AED 500 |\n| Water slide (min 4hr) | AED 500 | AED 1,000 |\n| 8-hr booking | — | **free water slide** |",
  "**Add-on prices (Satoshi only):**\n| Add-on | Price |\n|---|---:|\n| E-foil 1hr | AED 1,500 |\n| Electronic Shisha | AED 500 |\n| Water slide (min 4hr) | AED 1,000 |\n| 8-hr booking | free water slide |", 1),
 ("E5 Jetcar 30-min 1190->1200",
  "30 min AED 1190", "30 min AED 1200", 2),
 ("E6 add Eva 60 to Essential tier",
  "| Azimut 62 | 21 | 1,500 | azimut-62 |\n",
  "| Azimut 62 | 21 | 1,500 | azimut-62 |\n| Eva 60 | 12 | 1,500 | eva-60 |\n", 1),
 ("E7 insert Watersports §9.5",
  "## 10. Special Occasions",
  WATERSPORTS + "## 10. Special Occasions", 1),
]

def cat_block(t):
    i=t.find("## 7. Yacht Catalog"); j=t.find("## 11. Multi-day", i)
    return t[i:j] if i>=0 and j>=0 else None
def sha(s): return hashlib.sha256(s.encode()).hexdigest()[:12]

print("=== PER-EDIT MATCH COUNTS (must equal expected in ALL 5) ===")
edited={}
ok_all=True
for name,t in targets.items(): edited[name]=t
for label,find,repl,exp in EDITS:
    counts={}
    for name in targets:
        c=edited[name].count(find)
        counts[name]=c
        if c==exp: edited[name]=edited[name].replace(find,repl)
    line=" ".join(f"{n.split()[0][:6]}={counts[n]}" for n in targets)
    bad = any(c!=exp for c in counts.values())
    if bad: ok_all=False
    print(f"  [{'FAIL' if bad else 'ok'}] {label}: expect {exp} | {line}")

print("\n=== POST-EDIT VERIFICATION ===")
blocks={n:cat_block(t) for n,t in edited.items()}
uniq=set(sha(b) for b in blocks.values() if b)
print(f"  catalog block byte-identical across all 5: {len(uniq)==1} (sha {uniq})")
for kw in ["B2B","AED 1,000 + VAT","request license before","~50% off"]:
    resid={n:edited[n].count(kw) for n in edited}
    print(f"  B2B residue '{kw}': {sum(resid.values())} total  -> {'CLEAN' if sum(resid.values())==0 else resid}")
for kw in ["Eva 60","## 9.5 Watersports","AED 1,500 / 2,500","30 min AED 1200","AED 600"]:
    pres=all(kw in edited[n] for n in edited)
    print(f"  new content '{kw}' present in all 5: {pres}")

os.makedirs('/tmp/synced',exist_ok=True)
for n,t in edited.items(): open(f"/tmp/synced/{n}.txt","w").write(t)
print(f"\n  wrote edited copies -> /tmp/synced/  (overall edits OK: {ok_all})")

# unified diff for the operator (system-prompt.md only; n8n catalog edits are identical)
old=targets["system-prompt.md"].splitlines(); new=edited["system-prompt.md"].splitlines()
diff=[l for l in difflib.unified_diff(old,new,"system-prompt.md (live)","system-prompt.md (proposed)",lineterm="",n=1)]
print("\n=== UNIFIED DIFF (system-prompt.md) ===")
print("\n".join(diff))
