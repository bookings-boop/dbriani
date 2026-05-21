#!/usr/bin/env python3
"""
n8n_deploy.py — the ONE safe way to deploy workflow changes to the live n8n.

═══════════════════════════════════════════════════════════════════════════
THE BUG THIS EXISTS TO KILL  (docs/STATUS.md issue #2)
═══════════════════════════════════════════════════════════════════════════
Every old build_*.py did this:

    1. GET /workflows/{id}        <- staticData snapshot taken HERE  (time T0)
    2. modify nodes in memory     <- SLOW: flaky SSH, retries, MINUTES pass
    3. PUT /workflows/{id}  including that T0 staticData snapshot

n8n keeps the live draft queue ("pendingQueue") inside staticData. Any
customer draft created between T0 and the PUT was silently rolled back to
the minutes-old T0 copy — orphaning the Telegram draft card. Pressing Send
or 🤖 Auto on an orphaned card then crashes on a null draft.

═══════════════════════════════════════════════════════════════════════════
THE FIX
═══════════════════════════════════════════════════════════════════════════
safe_put() re-fetches the workflow's CURRENT staticData in the instant
before the PUT and sends THAT — never the stale snapshot. The window in
which a draft can be lost shrinks from "minutes" to the ~1-3s of the PUT
itself. It also prints the pendingQueue size before and after, and warns
loudly if the queue shrank (an execution wrote across our PUT).

It does NOT eliminate the race 100% — a message landing during the PUT
itself can still be clipped. That residual is handled separately by making
the Send / Auto handlers fail gracefully on a missing draft
(docs/STATUS.md issue #3). For zero risk, deploy during a quiet window.

safe_put() FAILS CLOSED: if the pre-PUT re-fetch fails, it ABORTS rather
than fall back to the stale snapshot.

═══════════════════════════════════════════════════════════════════════════
USAGE  (every future deploy script should look like this)
═══════════════════════════════════════════════════════════════════════════
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from n8n_deploy import N8N

    n  = N8N()                       # resolves the n8n API base over SSH
    wf = n.get_workflow()            # GET the live workflow
    #  ... modify wf["nodes"] / wf["connections"] in memory ...
    n.safe_put(wf, tag="MYFEATURE")  # re-fetches staticData, backs up, PUTs

Run this file directly for a read-only self-test (no PUT):
    python3 scripts/n8n_deploy.py
"""
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SSH = ["ssh", "-o", "ConnectTimeout=30", "dubriani-ec2"]
WF_NAME = "Dubriani Phase 1B — WhatsApp + Telegram Approval"

# Only these workflow settings are valid on a PUT; anything else is rejected.
ALLOWED_SETTINGS = {"saveExecutionProgress", "saveManualExecutions",
                    "saveDataErrorExecution", "saveDataSuccessExecution",
                    "executionTimeout", "errorWorkflow", "timezone",
                    "executionOrder"}


class DeployError(RuntimeError):
    """Raised on any unrecoverable deploy problem — callers should let it
    propagate so the operator sees a hard stop, never a silent half-deploy."""


class N8N:
    """A thin, safe client for the live n8n REST API, reached over SSH."""

    def __init__(self):
        self.key = self._load_key()
        self.api = self._resolve_api()

    # ---- bootstrap --------------------------------------------------------
    @staticmethod
    def _load_key():
        env = ROOT / ".env"
        if not env.exists():
            raise DeployError(".env not found — cannot load N8N_API_KEY")
        for ln in env.read_text().splitlines():
            if ln.startswith("N8N_API_KEY="):
                v = ln.split("=", 1)[1].strip().strip("'\"")
                if v:
                    return v
        raise DeployError("N8N_API_KEY not set in .env")

    def _resolve_api(self):
        ip = self.ssh(
            "docker inspect n8n-n8n-1 --format "
            "'{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}'",
            label="resolve-ip", timeout=45).strip()
        if not ip:
            raise DeployError("could not resolve the n8n container IP")
        return f"http://{ip}:5678/api/v1"

    # ---- SSH transport (10x retry, tolerant of flaky connectivity) --------
    def ssh(self, script, stdin="", label="ssh", timeout=70):
        last = ""
        for attempt in range(1, 11):
            r = None
            try:
                r = subprocess.run(SSH + [script], input=stdin,
                                   capture_output=True, text=True,
                                   timeout=timeout)
            except subprocess.TimeoutExpired:
                last = f"timed out after {timeout}s"
            if r is not None:
                if r.returncode == 0:
                    return r.stdout
                if r.returncode != 255:
                    raise DeployError(
                        f"{label}: ssh exit {r.returncode}\n{r.stderr[:400]}")
                last = f"ssh exit 255 ({(r.stderr or '').strip()[:100]})"
            if attempt < 10:
                print(f"   [{label}] connect attempt {attempt} failed: "
                      f"{last} — retry in 5s")
                time.sleep(5)
        raise DeployError(f"{label}: SSH unreachable after 10 attempts — {last}")

    def upload(self, data: bytes, remote: str, label="upload"):
        for attempt in range(1, 11):
            r = None
            try:
                r = subprocess.run(SSH + [f"cat > {remote}"], input=data,
                                   capture_output=True, timeout=90)
            except subprocess.TimeoutExpired:
                r = None
            if r is not None and r.returncode == 0:
                return
            if attempt < 10:
                print(f"   [{label}] upload attempt {attempt} failed — retry 5s")
                time.sleep(5)
        raise DeployError(f"{label}: upload failed after 10 attempts")

    # ---- API helpers ------------------------------------------------------
    def _api_get(self, path, label="GET"):
        out = self.ssh(
            f'read -r K; curl -s -m30 -H "X-N8N-API-KEY: $K" {self.api}{path}',
            stdin=self.key + "\n", label=label, timeout=70)
        try:
            return json.loads(out)
        except Exception:
            raise DeployError(f"{label}: non-JSON response:\n{out[:400]}")

    def get_workflow(self, name=WF_NAME):
        """GET the live workflow by name. The returned dict is yours to
        modify in memory; pass it back to safe_put()."""
        items = (self._api_get("/workflows", "list").get("data") or [])
        target = next((w for w in items if w.get("name") == name), None)
        if not target:
            raise DeployError(f"workflow {name!r} not found")
        wf = self._api_get(f"/workflows/{target['id']}", "get-workflow")
        print(f"   live workflow {wf['id']}: {len(wf.get('nodes', []))} nodes, "
              f"active={wf.get('active')}")
        return wf

    @staticmethod
    def _queue_size(static_data):
        """pendingQueue length from a staticData blob, or None if unknown.

        n8n nests global static data under a 'global' key — the workflow
        reads it via $getWorkflowStaticData('global'), so the live draft
        queue is at staticData.global.pendingQueue."""
        if isinstance(static_data, str):
            try:
                static_data = json.loads(static_data)
            except Exception:
                return None
        if isinstance(static_data, dict):
            scope = static_data.get("global")
            if not isinstance(scope, dict):
                scope = static_data  # tolerate a flat blob
            q = scope.get("pendingQueue")
            if isinstance(q, list):
                return len(q)
        return None

    # ---- THE SAFE DEPLOY --------------------------------------------------
    def safe_put(self, wf: dict, tag="DEPLOY"):
        """Deploy wf's (modified) nodes/connections WITHOUT clobbering the
        live draft queue.

        Steps:
          1. back up the modified workflow to workflows/phase-1b-*.PRE-{tag}*
          2. RE-FETCH the live staticData  <-- the fix; this is the live queue
          3. PUT modified nodes/connections + the FRESH staticData
          4. re-activate if it was active
          5. verify, and warn if the draft queue shrank
        """
        wf_id = wf.get("id")
        if not wf_id:
            raise DeployError("safe_put: workflow dict has no 'id'")
        was_active = bool(wf.get("active"))

        ts = time.strftime("%Y%m%d_%H%M%S")
        bk = ROOT / "workflows" / f"phase-1b-telegram.PRE-{tag}-{ts}.json"
        bk.write_text(json.dumps(wf, indent=2, ensure_ascii=False) + "\n")
        print(f"   backed up modified workflow -> {bk.name}")

        # --- THE FIX: re-fetch staticData in the instant before the PUT ----
        print("   re-fetching live staticData (fresh, immediately pre-PUT)...")
        try:
            fresh = self._api_get(f"/workflows/{wf_id}", "refetch-staticdata")
        except DeployError as e:
            raise DeployError(
                "ABORTED — could not re-fetch live staticData before the "
                f"PUT, so the live draft queue cannot be preserved: {e}")
        live_static = fresh.get("staticData")
        pre_q = self._queue_size(live_static)
        print(f"   live pendingQueue right now: "
              f"{pre_q if pre_q is not None else 'unknown'} draft(s) "
              "— this exact copy will be preserved")

        put = {
            "name": wf.get("name", WF_NAME),
            "nodes": wf["nodes"],
            "connections": wf["connections"],
            "settings": {k: v for k, v in (wf.get("settings") or {}).items()
                         if k in ALLOWED_SETTINGS},
        }
        if isinstance(live_static, (dict, str)) and live_static:
            put["staticData"] = live_static  # <-- FRESH, not wf's stale copy

        body = (json.dumps(put, ensure_ascii=False) + "\n").encode("utf-8")
        self.upload(body, "/tmp/n8n_deploy_put.json")
        res = None
        for attempt in range(1, 13):
            out = self.ssh(
                'read -r K; curl -s -m90 -X PUT -H "X-N8N-API-KEY: $K" '
                '-H "Content-Type: application/json" '
                f'--data-binary @/tmp/n8n_deploy_put.json '
                f'{self.api}/workflows/{wf_id}',
                stdin=self.key + "\n", label="PUT", timeout=120)
            try:
                res = json.loads(out)
            except Exception:
                res = {}
            if res.get("id") == wf_id:
                break
            if "unauthorized" in out.lower() and attempt < 12:
                print(f"   PUT attempt {attempt}: transient unauthorized "
                      "— retry in 12s")
                time.sleep(12)
                continue
            self.ssh("rm -f /tmp/n8n_deploy_put.json", label="cleanup")
            raise DeployError(f"PUT failed: {out[:300]}")
        self.ssh("rm -f /tmp/n8n_deploy_put.json", label="cleanup")
        print(f"   PUT OK ({len(res.get('nodes', []))} nodes)")

        if was_active:
            self.ssh(
                'read -r K; curl -s -m25 -X POST -H "X-N8N-API-KEY: $K" '
                f'{self.api}/workflows/{wf_id}/activate',
                stdin=self.key + "\n", label="activate")
            print("   re-activated")

        # --- verify, and check the queue did not shrink --------------------
        final = self._api_get(f"/workflows/{wf_id}", "verify")
        post_q = self._queue_size(final.get("staticData"))
        print(f"   verify: pendingQueue now {post_q if post_q is not None else 'unknown'} "
              f"draft(s)")
        if (isinstance(pre_q, int) and isinstance(post_q, int)
                and post_q < pre_q):
            print(f"   ⚠️  WARNING: pendingQueue dropped {pre_q} -> {post_q}. "
                  "An execution likely wrote across the PUT. Inspect the "
                  f"backup {bk.name} and the live queue.")
        return final


def _self_test():
    """Read-only proof the SSH path, API, and parsing all work — no PUT."""
    print("=== n8n_deploy.py self-test (read-only, no PUT) ===")
    n = N8N()
    print(f"1. n8n API resolved: {n.api}")
    wf = n.get_workflow()
    q = N8N._queue_size(wf.get("staticData"))
    print(f"2. live draft queue (pendingQueue): "
          f"{q if q is not None else 'unknown'} draft(s)")
    print("3. OK — safe_put() will re-fetch this queue immediately before "
          "any PUT.\nself-test passed.")


if __name__ == "__main__":
    try:
        _self_test()
    except DeployError as e:
        sys.exit(f"x self-test failed: {e}")
