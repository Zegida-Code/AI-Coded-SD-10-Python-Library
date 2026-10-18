#!/usr/bin/env python3
"""Backward-compatible entry point; the code now lives in the ``pysd10`` package next to this file.

    python sd10_ble.py measure      ==   python -m pysd10 measure   ==   pysd10 measure (after pip install)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pysd10.cli import main  # noqa: E402

if __name__ == "__main__":
    main()
