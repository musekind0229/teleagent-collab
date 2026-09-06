#!/usr/bin/env python3
"""Alias entry: same as bin/run-job.py (read charter → glue.run_job → reports)."""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

_bin = Path(__file__).resolve().parents[1] / "bin" / "run-job.py"
sys.argv[0] = str(_bin)
runpy.run_path(str(_bin), run_name="__main__")
