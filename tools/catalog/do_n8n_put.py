import json, sys, os, urllib.request, urllib.error, hashlib
key = os.environ["N8N_KEY"]; ip = sys.argv[1]; wfid = "azPIy9OcDwiPV5uY"
home = os.path.expanduser("~")

p = json.load(open(f"{home}/wf-put.json"))
allowed = {"executionOrder","saveManualExecutions","saveDataSuccessExecution","saveDataErrorExecution",
           "saveExecutionProgress","executionTimeout","errorWorkflow","timezone","callerPolicy"}
dropped = [k for k in (p.get("settings") or {}) if k not in allowed]
p["settings"] = {k: v for k, v in (p.get("settings") or {}).items() if k in allowed}
print("settings kept:", p["settings"], "| dropped:", dropped, "| nodes:", len(p["nodes"]))

body = json.dumps(p).encode()
req = urllib.request.Request(f"http://{ip}:5678/api/v1/workflows/{wfid}", data=body, method="PUT",
                             headers={"X-N8N-API-KEY": key, "Content-Type": "application/json"})
try:
    r = urllib.request.urlopen(req, timeout=30); print("PUT status:", r.status)
except urllib.error.HTTPError as e:
    print("PUT FAILED:", e.code, e.read()[:500].decode("utf-8","replace")); sys.exit(1)

# verify by reading it back
d = json.load(urllib.request.urlopen(
    urllib.request.Request(f"http://{ip}:5678/api/v1/workflows/{wfid}", headers={"X-N8N-API-KEY": key}), timeout=30))
def big(pa):
    b = ""
    def w(o):
        nonlocal b
        if isinstance(o, str):
            if len(o) > len(b): b = o
        elif isinstance(o, dict):
            [w(v) for v in o.values()]
        elif isinstance(o, list):
            [w(v) for v in o]
    w(pa); return b
def cb(t):
    i = t.find("## 7. Yacht Catalog"); j = t.find("## 11. Multi-day", i)
    return t[i:j] if i >= 0 and j >= 0 else ""
ok = True
for n in d["nodes"]:
    if n["name"] in ["Build Prompt","Build Regen Prompt","Build Refine Prompt","Build Lead Prompt"]:
        s = big(n["parameters"]); b = cb(s)
        checks = {"sha": hashlib.sha256(b.encode()).hexdigest()[:12],
                  "Eva60": "Eva 60" in s, "WS": "## 9.5 Watersports" in s,
                  "jc1200": "30 min AED 1200" in s, "noB2Brate": "AED 1,000 + VAT" not in s,
                  "noB2Bsection": "~50% off" not in s}
        if not (checks["Eva60"] and checks["WS"] and checks["jc1200"] and checks["noB2Brate"] and checks["noB2Bsection"]):
            ok = False
        print(n["name"], checks)
print("LIVE_VERIFY:", "PASS" if ok else "FAIL")
