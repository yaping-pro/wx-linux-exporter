"""Ensure repo-root imports work when tests are run directly (python3 tests/x.py)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
for p in (ROOT, ROOT / "bin"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
