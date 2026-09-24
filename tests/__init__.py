"""Make ``python -m unittest tests.test_*`` able to import siblings.

``desktop_lock_isolation`` lives next to the test modules. Discovery adds
this directory to ``sys.path``; importing ``tests.test_*`` does not.
"""
from __future__ import annotations

import sys
from pathlib import Path

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
