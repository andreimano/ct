"""Read host aliases out of ~/.ssh/config."""

from __future__ import annotations

import os
from pathlib import Path

WILDCARDS = "*?!"


def aliases(path=None):
    """Concrete Host aliases in file order. Patterns and Include are ignored."""
    p = Path(path or os.environ.get("CT_SSH_CONFIG") or Path.home() / ".ssh" / "config")
    if not p.exists():
        return []
    found = []
    for raw in p.read_text().splitlines():
        line = raw.strip().replace("=", " ", 1)
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if parts[0].lower() != "host":
            continue
        for name in parts[1:]:
            if not any(c in name for c in WILDCARDS) and name not in found:
                found.append(name)
    return found
