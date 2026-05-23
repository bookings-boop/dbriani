#!/usr/bin/env python3
"""
fix_split_wiring.py — rewire the Pipeline Review chain so Split Lead Cards
receives Hermes Review's output directly.

Bug: chain was sequential Hermes Review → Send Review → Split Lead Cards,
so the splitOut node saw {ok, result} (Telegram API response) instead of
the {per_lead_messages: [...]} shape from Hermes Review.

Fix: Hermes Review fans out to two parallel arms:
    Hermes Review ─┬─► Send Review (header)
                   └─► Split Lead Cards ─► Send Lead Card (loops per lead)
And reset Split Lead Cards fieldToSplitOut to the plain field name
'per_lead_messages' (the expression form crashes splitOut).

Applied to both the main workflow and the cron workflow.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from n8n_deploy import N8N, DeployError  # noqa: E402

CRON_WF_NAME = "Dubriani Pipeline Review Cron"


def _patch(wf, label):
    conns = wf.setdefault("connections", {})
    # 1) Reset fieldToSplitOut to plain field name
    split = next((x for x in wf["nodes"] if x["name"] == "Split Lead Cards"), None)
    if split is None:
        print(f"   [{label}] Split Lead Cards not found — skip")
        return False
    cur = split["parameters"].get("fieldToSplitOut", "")
    if cur != "per_lead_messages":
        split["parameters"]["fieldToSplitOut"] = "per_lead_messages"
        print(f"   [{label}] fieldToSplitOut → 'per_lead_messages'")

    # 2) Rewire: Hermes Review must connect to BOTH Send Review AND Split Lead Cards.
    #    Send Review must NOT connect to Split Lead Cards anymore.
    hr_conns = conns.setdefault("Hermes Review", {"main": [[]]})
    hr_mains = hr_conns["main"]
    if not hr_mains:
        hr_mains.append([])
    targets = {d.get("node") for d in hr_mains[0]}
    changed = False
    if "Send Review" not in targets:
        hr_mains[0].append({"node": "Send Review", "type": "main", "index": 0})
        changed = True
    if "Split Lead Cards" not in targets:
        hr_mains[0].append({"node": "Split Lead Cards", "type": "main", "index": 0})
        changed = True
    if changed:
        print(f"   [{label}] Hermes Review now fans out to both arms")

    # Remove Send Review → Split Lead Cards if it exists.
    sr_conns = conns.get("Send Review", {})
    sr_mains = sr_conns.get("main", [])
    if sr_mains:
        before = len(sr_mains[0]) if sr_mains[0] else 0
        sr_mains[0] = [d for d in (sr_mains[0] or []) if d.get("node") != "Split Lead Cards"]
        if before != len(sr_mains[0]):
            print(f"   [{label}] removed Send Review → Split Lead Cards edge")

    # 3) Sanity check: Split Lead Cards → Send Lead Card (this was set already, idempotent).
    sl_conns = conns.setdefault("Split Lead Cards", {"main": [[]]})
    sl_mains = sl_conns["main"]
    if not sl_mains:
        sl_mains.append([])
    if not any(d.get("node") == "Send Lead Card" for d in sl_mains[0]):
        sl_mains[0].append({"node": "Send Lead Card", "type": "main", "index": 0})
        print(f"   [{label}] added Split Lead Cards → Send Lead Card")
    return True


def main():
    deploy = "--deploy" in sys.argv
    print("=== fix_split_wiring.py ===")
    n = N8N()
    main_wf = n.get_workflow()
    if _patch(main_wf, "main") and deploy:
        n.safe_put(main_wf, tag="SPLITWIRING")
        print("   [main] deployed")

    items = (n._api_get("/workflows", "list").get("data") or [])
    cron = next((w for w in items if w.get("name") == CRON_WF_NAME), None)
    if cron:
        cron_wf = n._api_get(f"/workflows/{cron['id']}", "get-cron")
        if _patch(cron_wf, "cron") and deploy:
            put_body = {
                "name": cron_wf.get("name"),
                "nodes": cron_wf.get("nodes", []),
                "connections": cron_wf.get("connections", {}),
                "settings": cron_wf.get("settings", {}),
            }
            body = json.dumps(put_body).encode("utf-8")
            n.upload(body, "/tmp/cron_swfix.json", label="upload")
            n.ssh(
                f'read -r K; curl -s -X PUT '
                f'-H "X-N8N-API-KEY: $K" -H "Content-Type: application/json" '
                f'--data-binary @/tmp/cron_swfix.json {n.api}/workflows/{cron["id"]}',
                stdin=n.key + "\n", label="cron-put", timeout=90)
            n.ssh("rm -f /tmp/cron_swfix.json", label="rm", timeout=30)
            print("   [cron] deployed")


if __name__ == "__main__":
    try:
        main()
    except DeployError as e:
        sys.exit(f"x {e}")
