#!/usr/bin/env python3
"""
verify-supermemory.py

Probes Supermemory to verify:
  1. Auth works
  2. Whether the Google Drive ingestion has loaded content
  3. What containerTags (if any) are present on existing memories

Usage:
    export SUPERMEMORY_API_KEY=sm_...
    python3 verify-supermemory.py

If you don't have the key:
    - Open N8N → Credentials → "Bearer Auth account" → reveal token, OR
    - supermemory.ai dashboard → API Keys
"""
import os
import sys
import json
import urllib.request
import urllib.error

API_KEY = os.environ.get("SUPERMEMORY_API_KEY")
if not API_KEY:
    print("✗ Set SUPERMEMORY_API_KEY env var first.", file=sys.stderr)
    print("  export SUPERMEMORY_API_KEY=sm_...", file=sys.stderr)
    sys.exit(1)

BASE = "https://api.supermemory.ai/v3"

def post(path, body):
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8") or "{}")
    except Exception as e:
        return -1, {"error": str(e)}


def test_search(label, query, tags=None):
    body = {"q": query, "limit": 5}
    if tags:
        body["containerTags"] = tags
    status, data = post("/search", body)
    print(f"\n--- {label} ---")
    print(f"  Query: {query!r}")
    if tags:
        print(f"  Tags:  {tags}")
    print(f"  HTTP:  {status}")
    if status != 200:
        print(f"  Body:  {json.dumps(data, indent=2)[:500]}")
        return []

    results = data.get("results") or data.get("memories") or data.get("documents") or []
    print(f"  Hits:  {len(results)}")
    seen_tags = set()
    for i, r in enumerate(results[:3]):
        content = (r.get("content") or r.get("text") or "")[:120]
        tags_on = r.get("containerTags") or r.get("tags") or []
        for t in tags_on:
            seen_tags.add(t)
        print(f"   [{i+1}] tags={tags_on}")
        print(f"       text={content.strip()!r}")
    if len(results) > 3:
        print(f"   ... (+{len(results) - 3} more)")
    return list(seen_tags)


print("=" * 60)
print("Supermemory verification — Dubriani")
print("=" * 60)

# 1. Plain search, no tags — should hit anything in the account
all_tags_seen = set()
tags = test_search("1. Untagged search for 'dubriani yacht'", "dubriani yacht")
all_tags_seen.update(tags)

# 2. Plain search for a Satoshi-specific query
tags = test_search("2. Untagged search for 'satoshi pricing'", "satoshi pricing")
all_tags_seen.update(tags)

# 3. Try with the target tag
test_search("3. Tagged search with ['dubriani']", "yacht", ["dubriani"])

# 4. Try common alternative tags ingestion connectors might use
for alt_tag in ["google_drive", "drive", "knowledge", "dubriani_yachts"]:
    tags = test_search(f"4. Tagged search with ['{alt_tag}']", "yacht", [alt_tag])
    all_tags_seen.update(tags)

print("\n" + "=" * 60)
print("SUMMARY")
print("=" * 60)
if all_tags_seen:
    print(f"\nUnique tags observed on returned memories:")
    for t in sorted(all_tags_seen):
        print(f"  - {t}")
    print(f"\nDecision: update the workflow's Supermemory node body to include")
    print(f'  "containerTags": [<the tag that actually matched your Dubriani content>]')
else:
    print(f"\nNo tags surfaced.")
    print(f"Possible causes:")
    print(f"  a) Ingestion hasn't finished yet (Drive sync takes minutes on first load)")
    print(f"  b) Auth token is wrong")
    print(f"  c) Dashboard shows files but they're tagged with something obscure")
    print(f"\nNext step: open supermemory.ai dashboard, search for 'satoshi' manually,")
    print(f"and look at what containerTags the matching memories have.")
