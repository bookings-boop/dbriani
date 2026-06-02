import json, sys, hashlib

WF_IN, NEW_SP, OUT_WF, OUT_PUT = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]

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
EDITS = [
 ("E1", "### B2B partner pricing (~50% off retail)\n\nWhen customer represents an agency / asks for B2B pricing:\n1. Request company license.\n2. Quote B2B prices (~50% off retail):\n - Satoshi: AED 1,500/hr B2B vs AED 3,000 B2C\n - Eclipse: AED 2,000/hr B2B vs AED 4,000 B2C\n - Add-ons also halved: e-foil 500 vs 1,000; slide 500 vs 1,000; shisha 250 vs 500\n3. Be patient — B2B partner relationships take weeks/months to convert.\n\n", "", 1),
 ("E2", '"customer asked for B2B pricing — request license before quoting", ', "", 1),
 ("E3", "| B2B partner price | AED 1,000 + VAT | AED 12,000 + VAT |\n", "", 1),
 ("E4", "**B2B add-on prices (Satoshi only):**\n| Add-on | B2B | B2C |\n|---|---:|---:|\n| E-foil 1hr | AED 500 | AED 1,000 |\n| Electronic Shisha | AED 250 | AED 500 |\n| Water slide (min 4hr) | AED 500 | AED 1,000 |\n| 8-hr booking | — | **free water slide** |", "**Add-on prices (Satoshi only):**\n| Add-on | Price |\n|---|---:|\n| E-foil 1hr | AED 1,500 |\n| Electronic Shisha | AED 500 |\n| Water slide (min 4hr) | AED 1,000 |\n| 8-hr booking | free water slide |", 1),
 ("E5", "30 min AED 1190", "30 min AED 1200", 2),
 ("E6", "| Azimut 62 | 21 | 1,500 | azimut-62 |\n", "| Azimut 62 | 21 | 1,500 | azimut-62 |\n| Eva 60 | 12 | 1,500 | eva-60 |\n", 1),
 ("E7", "## 10. Special Occasions", WATERSPORTS + "## 10. Special Occasions", 1),
]
PROMPT_NODES = ["Build Prompt","Build Regen Prompt","Build Refine Prompt","Build Lead Prompt"]

def cat_block(t):
    i=t.find("## 7. Yacht Catalog"); j=t.find("## 11. Multi-day", i)
    return t[i:j] if i>=0 and j>=0 else None
def sha(s): return hashlib.sha256(s.encode()).hexdigest()[:12]

wf=json.load(open(WF_IN))
new_sp=open(NEW_SP).read()
target_block=cat_block(new_sp)
nodes=wf["nodes"]
fail=False

def biggest_path(params):
    # returns (container_list_or_dict, key, value) for the biggest string
    best=None
    def w(o):
        nonlocal best
        if isinstance(o,dict):
            for k,v in o.items():
                if isinstance(v,str) and (best is None or len(v)>len(best[2])): best=(o,k,v)
                else: w(v)
        elif isinstance(o,list):
            for idx,v in enumerate(o):
                if isinstance(v,str) and (best is None or len(v)>len(best[2])): best=(o,idx,v)
                else: w(v)
    w(params); return best

for n in nodes:
    if n.get("name") in PROMPT_NODES:
        cont,key,val=biggest_path(n["parameters"])
        s=val
        for lbl,find,repl,exp in EDITS:
            c=s.count(find)
            if c!=exp:
                print(f"  FAIL {n['name']} {lbl}: count={c} expected={exp}"); fail=True
            s=s.replace(find,repl)
        cont[key]=s
        cb=cat_block(s)
        match = (cb==target_block)
        print(f"  {n['name']}: catalog-block sha={sha(cb) if cb else 'NONE'} matches_system_prompt={match}")
        if not match: fail=True

if fail:
    print("VERIFY: FAIL — not writing outputs"); sys.exit(1)

# all 4 nodes' catalog blocks identical + == system-prompt
blocks=[cat_block(biggest_path(n['parameters'])[2]) for n in nodes if n.get('name') in PROMPT_NODES]
print(f"VERIFY: 4 n8n catalog blocks identical: {len(set(map(sha,blocks)))==1}; == system-prompt: {sha(blocks[0])==sha(target_block)}")

json.dump(wf, open(OUT_WF,"w"))
# PUT body = whitelist
put={"name":wf["name"],"nodes":wf["nodes"],"connections":wf["connections"],"settings":wf.get("settings",{}) or {}}
json.dump(put, open(OUT_PUT,"w"))
print(f"VERIFY: PASS — wrote {OUT_WF} and PUT body {OUT_PUT} (settings keys: {list(put['settings'].keys())})")
