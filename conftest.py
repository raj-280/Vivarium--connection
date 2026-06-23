"""
conftest.py — Repo-root pytest configuration.

Adds pi/ and server/ to sys.path so tests in either subtree can import
their respective modules without needing to cd into a subfolder first.

Run all tests from repo root:
    pytest -v
"""
import sys
from pathlib import Path

REPO = Path(__file__).parent
for sub in ("pi", "server"):
    p = str(REPO / sub)
    if p not in sys.path:
        sys.path.insert(0, p)
