#!/usr/bin/env python3
"""
diagnose_hermes.py - Phase 1 of the Hermes revival: diagnose drafting latency.

Runs entirely off the live path. SSHes to the box, locates the hermes CLI,
inspects the accumulated profile state, and runs timed `hermes chat` test
drafts WITH and WITHOUT the memory tool (-t memory) to isolate the slowdown
that caused the 2026-05-21 rollback.

Read-mostly: it does run `hermes chat` test drafts (Anthropic API, a few
cents; with -t memory it writes a couple of memory entries to the `default`
profile). It does NOT touch the live n8n workflow or any customer.

Usage:  python3 scripts/diagnose_hermes.py
"""
import re
import subprocess
import sys
import time

SSH = ["ssh", "-o", "ConnectTimeout=30", "dubriani-ec2"]

QUERY = ("Customer asked: do you have a yacht available saturday afternoon "
         "for 10 guests and what is the price. Draft a short friendly WhatsApp "
         "reply as Maria from Dubriani Yachts.")


def ssh(script, label, timeout=60):
    """Run a remote command; retry only on SSH connection failure (exit 255)."""
    last = ""
    for attempt in range(1, 6):
        r = None
        try:
            r = subprocess.run(SSH + [script], capture_output=True, text=True,
                               timeout=timeout)
        except subprocess.TimeoutExpired:
            last = f"timed out after {timeout}s"
        if r is not None:
            if r.returncode != 255:          # 0 or a real remote rc — accept
                return r.stdout, r.stderr, r.returncode
            last = "ssh exit 255 (connect timeout)"
        if attempt < 5:
            print(f"  [{label}] connect attempt {attempt} failed ({last}) — retry 5s",
                  flush=True)
            time.sleep(5)
    print(f"  [{label}] FAILED after 5 attempts — {last}", flush=True)
    return "", last, -1


def section(t):
    print("\n" + "=" * 64 + f"\n{t}\n" + "=" * 64, flush=True)


# --- 1. locate the hermes CLI -------------------------------------------
section("1. locate hermes CLI")
out, err, rc = ssh(
    'for p in "$HOME/.local/bin/hermes" "$HOME/bin/hermes" '
    '/usr/local/bin/hermes /usr/bin/hermes '
    '"$(bash -lc "command -v hermes 2>/dev/null")"; do '
    '[ -x "$p" ] && echo "HERMES:$p" && break; done', "locate")
m = re.search(r"HERMES:(\S+)", out)
if not m:
    print(f"x hermes CLI not found.\nstdout: {out}\nstderr: {err}")
    sys.exit(1)
HERMES = m.group(1)
print(f"hermes binary: {HERMES}")
out, _, _ = ssh(f'{HERMES} --version 2>&1 | head -1', "version")
print(f"version: {out.strip()}")

# --- 2. profile / accumulated state -------------------------------------
section("2. ~/.hermes state (profile bloat hypothesis)")
out, err, rc = ssh(
    'echo "total:"; du -sh "$HOME/.hermes" 2>/dev/null; '
    'echo "by entry:"; du -sh "$HOME/.hermes"/* 2>/dev/null | sort -rh | head -15; '
    'echo "file count:"; find "$HOME/.hermes" -type f 2>/dev/null | wc -l; '
    'echo "largest files:"; find "$HOME/.hermes" -type f -printf "%s\\t%p\\n" '
    '2>/dev/null | sort -rn | head -10', "state")
print(out.strip() or f"(no output) stderr: {err}")

# --- 3. hermes-bridge service status ------------------------------------
section("3. hermes-bridge service (should be stopped/disabled post-rollback)")
out, _, _ = ssh(
    'export XDG_RUNTIME_DIR=/run/user/$(id -u); '
    'echo "active: $(systemctl --user is-active hermes-bridge 2>&1)"; '
    'echo "enabled: $(systemctl --user is-enabled hermes-bridge 2>&1)"', "bridge")
print(out.strip())

# --- 4. timed test drafts -----------------------------------------------
section("4. timed `hermes chat` drafts — WITH vs WITHOUT -t memory")
print("(3 runs each; first run of each group may include cold-start)\n", flush=True)


def timed_draft(mem_flag, n):
    label = f"draft#{n} {'mem' if mem_flag else 'no-mem'}"
    flag = "-t memory" if mem_flag else ""
    cmd = (f'S=$(date +%s%N); '
           f'{HERMES} --profile default chat -q "{QUERY}" -Q --source tool '
           f'--yolo {flag} > /tmp/hd_out.txt 2>&1; RC=$?; '
           f'E=$(date +%s%N); '
           f'echo "ELAPSED_MS:$(( (E - S) / 1000000 ))"; '
           f'echo "RC:$RC"; echo "OUTLEN:$(wc -c < /tmp/hd_out.txt)"; '
           f'echo "HEAD:$(head -c 200 /tmp/hd_out.txt | tr "\\n" " ")"')
    out, err, rc = ssh(cmd, label, timeout=220)
    el = re.search(r"ELAPSED_MS:(\d+)", out)
    hrc = re.search(r"RC:(-?\d+)", out)
    head = re.search(r"HEAD:(.*)", out)
    secs = (int(el.group(1)) / 1000.0) if el else None
    print(f"  {label:18}  "
          f"{(str(round(secs, 1)) + 's') if secs is not None else 'NO TIMING':>10}  "
          f"hermes_rc={hrc.group(1) if hrc else '?'}", flush=True)
    if head:
        print(f"      out: {head.group(1)[:160]}", flush=True)
    return secs


results = {"mem": [], "no-mem": []}
for i in range(1, 4):
    s = timed_draft(True, i)
    if s is not None:
        results["mem"].append(s)
for i in range(1, 4):
    s = timed_draft(False, i)
    if s is not None:
        results["no-mem"].append(s)

# --- 5. verdict ----------------------------------------------------------
section("5. summary")


def stat(xs):
    return f"avg {sum(xs)/len(xs):.1f}s  (min {min(xs):.1f}  max {max(xs):.1f})" if xs else "no data"


print(f"WITH -t memory   : {stat(results['mem'])}")
print(f"WITHOUT -t memory: {stat(results['no-mem'])}")
if results["mem"] and results["no-mem"]:
    a, b = sum(results["mem"]) / len(results["mem"]), sum(results["no-mem"]) / len(results["no-mem"])
    print(f"\nmemory-tool overhead: {a - b:+.1f}s  ({a / b:.1f}x)" if b else "")
    if a > b * 1.8:
        print("=> -t memory is a major latency contributor — drop it for "
              "single-shot drafting (the FR-5 background pass can do memory "
              "separately).")
    elif max(results["mem"] + results["no-mem"]) > 25:
        print("=> drafting is slow even without the memory tool — investigate "
              "the model/config, not just -t memory.")
    else:
        print("=> latency looks acceptable in isolation — the rollback "
              "slowdown may have been load/concurrency on the live path.")
