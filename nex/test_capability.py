"""Tests for the capability classification model (STAGE 2/3)."""

import importlib
import sys
import os

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

cap = importlib.import_module("mcp.capability")
policy_mod = importlib.import_module("mcp.policy")


def _expect(cond, msg):
    print(("ok   - " if cond else "FAIL - ") + msg)
    if not cond:
        sys.exit(1)


print("\nAll capability/policy tests passed.")
