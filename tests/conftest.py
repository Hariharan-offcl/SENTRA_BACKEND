"""
SENTRA — pytest configuration.

The `tests/test_phase*.py` files are STANDALONE smoke suites authored as
scripts (module-level `check()` flow ending in `sys.exit(1 if FAIL else 0)`;
diagnosed in SENTRA_INTEGRATION_AUDIT.md Phase 0). Importing one under pytest
raises SystemExit during collection — the original "21 collection errors" —
so they are excluded from direct collection here, and
`tests/test_integration_suites.py` runs each of them in a subprocess and maps
the exit code onto a pytest result.

    pytest tests/                      # everything, one result per suite
    python -X utf8 tests/test_phaseN.py   # single suite, still works as before
"""

import glob
import os

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))

# Collect only the runner; every test_phase*.py script is excluded.
# (conftest.py itself is not a test module, but collect_ignore also keeps
# helper files out if any are added later.)
_collect = [p for p in glob.glob(os.path.join(_HERE, "test_phase*.py"))]

collect_ignore = [os.path.basename(p) for p in _collect]
