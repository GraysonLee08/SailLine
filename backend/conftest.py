# backend/conftest.py
#
# Adds backend/ to sys.path so tests can `from app.foo import ...` and
# `from workers.weather_ingest import ...` without the `python -m pytest`
# prefix.
#
# pytest.ini also sets `pythonpath = .` for the same purpose. On at
# least one Windows + Python 3.13 + pytest 8.3.3 combination that
# directive doesn't take effect even though the rest of the ini parses
# (configfile loads, other options apply). This conftest is the
# bulletproof fallback — pytest discovers it before collection so the
# path manipulation runs before any test module is imported.
#
# tests/conftest.py is separate and handles env-var setup; we leave it
# alone. Multiple conftest.py files at different scopes is normal —
# pytest composes them.

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
