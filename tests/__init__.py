"""Test package.

The path bootstrap lives here (not only in ``support.py``) so that test modules
may import ``dotdeployer`` in any order - ``unittest`` imports this package
before any test module.
"""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
