"""Make the package importable from tests/ without installing it."""
from __future__ import annotations

import sys
from pathlib import Path


REPO_ROOT: Path = Path(__file__).resolve().parent.parent
for p in (REPO_ROOT, REPO_ROOT / "commands" / "spotify"):
    sp: str = str(p)
    if sp not in sys.path:
        sys.path.insert(0, sp)
