#!/usr/bin/env python3
"""
deploy_bridge.py - deploy the hermes-bridge service to the EC2 box.

Backs up the box's copies, uploads hermes-bridge/server.py and
system-prompt.md to ~/hermes-bridge/, compile-checks server.py on the box,
restarts the hermes-bridge user service, and verifies /health + that the
/improve route is live.

The bridge is NOT wired to the live workflow, so a restart has zero
customer impact. Every SSH call retries (the path to the box is flaky).

Usage:  python3 scripts/deploy_bridge.py
"""
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SSH = ["ssh", "-o", "ConnectTimeout=30", "dubriani-ec2"]
SYSCTL = "export XDG_RUNTIME_DIR=/run/user/$(id -u); systemctl --user"


def die(m):
    sys.exit(f"x {m}")


def ssh_run(script, label, timeout=60):
    last = ""
    for a in range(1, 11):
        r = None
        try:
            r = subprocess.run(SSH + [script], capture_output=True, text=True,
                               timeout=timeout)
        except subprocess.TimeoutExpired:
            last = f"timed out after {timeout}s"
        if r is not None:
            if r.returncode != 255:
                return r.stdout, r.stderr, r.returncode
            last = "ssh exit 255 (connect timeout)"
        if a < 10:
            print(f"  [{label}] connect attempt {a} failed ({last}) — retry 5s",
                  flush=True)
            time.sleep(5)
    die(f"{label}: connection failed after 10 attempts — {last}")


def ssh_upload(data, remote, label):
    last = ""
    for a in range(1, 11):
        r = None
        try:
            r = subprocess.run(SSH + [f"cat > {remote}"], input=data,
                               capture_output=True, timeout=90)
        except subprocess.TimeoutExpired:
            last = "timed out"
        if r is not None:
            if r.returncode == 0:
                return
            last = f"exit {r.returncode}"
        if a < 10:
            print(f"  [{label}] upload attempt {a} failed ({last}) — retry 5s",
                  flush=True)
            time.sleep(5)
    die(f"{label}: upload failed after 10 attempts — {last}")


def main():
    ts = time.strftime("%Y%m%d_%H%M%S")
    print("=== deploy_bridge.py ===")

    # 1. back up the box's current copies
    ssh_run(f'cd ~/hermes-bridge && cp server.py server.py.bak.{ts} && '
            f'{{ [ -f system-prompt.md ] && cp system-prompt.md '
            f'system-prompt.md.bak.{ts} || true; }} && echo OK', "backup")
    print(f"1. backed up box server.py + system-prompt.md (.bak.{ts})")

    # 2. upload server.py + system-prompt.md
    ssh_upload((ROOT / "hermes-bridge" / "server.py").read_bytes(),
               "~/hermes-bridge/server.py", "upload-server")
    print("2. uploaded server.py")
    ssh_upload((ROOT / "system-prompt.md").read_bytes(),
               "~/hermes-bridge/system-prompt.md", "upload-prompt")
    print("3. uploaded system-prompt.md (current — incl. the lowercase rule)")

    # 3. compile-check on the box
    out, err, rc = ssh_run(
        "python3 -m py_compile ~/hermes-bridge/server.py && echo COMPILE_OK", "compile")
    if "COMPILE_OK" not in out:
        die(f"server.py failed to compile on the box:\n{err[:400]}")
    print("4. server.py compiles on the box")

    # 4. restart the bridge
    ssh_run(f'{SYSCTL} restart hermes-bridge && echo RESTARTED', "restart")
    print("5. hermes-bridge restarted")
    time.sleep(3)

    # 5. verify
    active, _, _ = ssh_run(f'{SYSCTL} is-active hermes-bridge', "is-active")
    health, _, _ = ssh_run(
        'curl -s -m10 -o /dev/null -w "%{http_code}" localhost:8788/health', "health")
    route, _, _ = ssh_run(
        'curl -s -m10 -o /dev/null -w "%{http_code}" -X POST localhost:8788/improve',
        "improve-route")
    active, health, route = active.strip(), health.strip(), route.strip()
    print(f"6. VERIFY: service={active}  /health=HTTP {health}  "
          f"/improve=HTTP {route}  (401 = route live + token-gated)")
    ok = active == "active" and health == "200" and route == "401"
    print("BRIDGE DEPLOYED — /improve endpoint is live." if ok
          else f"x VERIFY FAILED — inspect; box backups saved as .bak.{ts}")


if __name__ == "__main__":
    main()
