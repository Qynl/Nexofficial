#!/usr/bin/env python3
"""Run the whole Nex test suite.

Each suite runs in its own process: several of them configure NEX_HOME
and module-level singletons differently, and isolation keeps that honest
(a green suite must never depend on import order).

Usage:  python3 tests/run_all.py [suite ...]
"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))

SUITES = [
    "test_architecture.py",   # dependency direction, no local tools, no MC
    "test_capability.py",     # tool classification + policy gates
    "test_transport.py",      # MCP framing, stdio children, HTTP upstreams
    "test_manager.py",        # server lifecycle, calls, approvals, audit
    "test_agent_loop.py",     # plan → act → observe → evaluate → adapt
    "test_quality.py",        # game production gates + bounded polish pass
    "test_production.py",     # multi-stage studio program + MCP readiness
    "test_engine_profiles.py", # Unreal 5.8 + Roblox Studio contracts/readiness
    "test_store.py",          # conversation persistence
    "test_server_api.py",     # HTTP API: auth, CSRF, chat, SSE
    "test_escape.py",         # adversarial boundary: no hidden PC route
    "test_providers.py",      # model providers: routing, failover, roles
]


def main() -> int:
    picks = sys.argv[1:] or SUITES
    failures = []
    t0 = time.time()
    for suite in picks:
        if suite not in SUITES:
            print("?? unknown suite %r (known: %s)" % (suite, ", ".join(SUITES)))
            return 2
        print("══ %s" % suite)
        r = subprocess.run(
            [sys.executable, os.path.join(HERE, suite)],
            cwd=os.path.dirname(HERE),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            timeout=300)
        out = r.stdout
        # compact report: keep FAIL/ERROR lines and the verdict
        interesting = [l for l in out.splitlines()
                       if l.startswith(("FAIL", "ERROR", "OK"))
                       and "ResourceWarning" not in l]
        verdict = "PASS"
        if r.returncode != 0 or "FAILED" in out:
            verdict = "FAIL"
            failures.append(suite)
        for l in interesting[-8:]:
            print("   " + l)
        print("   → %s\n" % verdict)
    dt = time.time() - t0
    if failures:
        print("✗ %d/%d suites failed (%s) in %.1fs"
              % (len(failures), len(picks), ", ".join(failures), dt))
        return 1
    print("✓ all %d suites passed in %.1fs" % (len(picks), dt))
    return 0


if __name__ == "__main__":
    sys.exit(main())
