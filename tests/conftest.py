"""
Pytest configuration: add shared/ to sys.path so tests can import runtime directly.
"""

from __future__ import annotations

import sys
from pathlib import Path

# shared/ must be on sys.path before any test module imports runtime
_SHARED = str(Path(__file__).parent.parent / 'shared')
if _SHARED not in sys.path:
    sys.path.insert(0, _SHARED)

# books_status/ must be on sys.path so tests can import run.py as a module
_BOOKS_STATUS = str(Path(__file__).parent.parent / 'books_status')
if _BOOKS_STATUS not in sys.path:
    sys.path.insert(0, _BOOKS_STATUS)
