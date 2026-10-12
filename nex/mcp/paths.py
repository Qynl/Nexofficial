"""Canonical location for Nex's operator-controlled persistent state."""
from __future__ import annotations

import os


def nex_home(create: bool = True) -> str:
    """Return the state directory, creating it privately by default."""
    base = os.environ.get("NEX_HOME") or os.path.join(
        os.path.expanduser("~"), ".nex")
    if create:
        os.makedirs(base, mode=0o700, exist_ok=True)
        try:
            os.chmod(base, 0o700)
        except OSError:
            pass
    return base
