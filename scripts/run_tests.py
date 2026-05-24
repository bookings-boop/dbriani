#!/usr/bin/env python3
"""Master test runner — invokes every hermes-bridge/test_*.py module
in subprocesses so a hung or crashing test doesn't take the rest with
it. Reports pass/fail counts + total wall time.

Usage:  python3 scripts/run_tests.py
        python3 scripts/run_tests.py --verbose   # show stdout/stderr
"""
import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = ROOT / "hermes-bridge"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    tests = sorted(TESTS_DIR.glob("test_*.py"))
    if not tests:
        print("no tests found in", TESTS_DIR)
        return 1

    print(f"=== running {len(tests)} test module(s) ===\n")
    passed = 0
    failed = []
    t0 = time.time()
    for t in tests:
        print(f"--- {t.name} ---")
        proc = subprocess.run(
            [sys.executable, str(t)],
            capture_output=not args.verbose, text=True, timeout=60)
        if proc.returncode == 0:
            if not args.verbose:
                last = (proc.stdout or "").strip().splitlines()[-1:]
                if last:
                    print(f"  {last[0]}")
            passed += 1
        else:
            failed.append(t.name)
            print(f"  FAIL — rc={proc.returncode}")
            if not args.verbose:
                # Surface the last 20 lines of output so the failure
                # is diagnosable without re-running.
                tail = (proc.stdout or "").splitlines()[-20:]
                for ln in tail:
                    print(f"    {ln}")
                if proc.stderr:
                    print("    --stderr--")
                    for ln in (proc.stderr or "").splitlines()[-10:]:
                        print(f"    {ln}")

    elapsed = time.time() - t0
    print()
    print(f"=== {passed}/{len(tests)} module(s) passed "
          f"in {elapsed:.1f}s ===")
    if failed:
        print("FAILED:", ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
