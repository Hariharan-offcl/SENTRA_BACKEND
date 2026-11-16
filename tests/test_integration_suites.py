"""
SENTRA — integration-suite runner (pytest normalization).

Runs every standalone suite (tests/test_phase*.py) in its own interpreter and
maps the result onto pytest. This gives the project a real `pytest tests/`
entry point WITHOUT rewriting 27 working suites; each suite keeps its
documented direct-run mode (`python -X utf8 tests/test_phaseN.py`).

Notes
-----
* One pytest "test" per suite: the suite prints its own per-check detail,
  which pytest shows on failure (captured output).
* Each suite runs in a subprocess with an isolated interpreter, so the
  module-reload / fake-lgpio tricks some suites use cannot leak between them.
"""

import glob
import os
import subprocess
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)

_SUITES = sorted(glob.glob(os.path.join(_HERE, "test_phase*.py")))


@pytest.mark.parametrize("suite", _SUITES, ids=[os.path.basename(s) for s in _SUITES])
def test_standalone_suite(suite: str):
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    # encoding/errors are REQUIRED on Windows: the child writes UTF-8 (env
    # above) while the default pipe decoder is cp1252 — a charmap decode
    # error in the reader thread kills the pipe and the suite dies abnormally.
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", suite],
        cwd=_ROOT, env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=420,
    )
    if proc.returncode != 0:
        tail = "\n".join((proc.stdout or "").splitlines()[-25:])
        pytest.fail(
            f"suite exited {proc.returncode}\n--- last stdout ---\n{tail}\n"
            f"--- stderr tail ---\n{(proc.stderr or '')[-800:]}",
            pytrace=False,
        )
