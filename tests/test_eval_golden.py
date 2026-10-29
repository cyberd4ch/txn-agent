"""CI wrapper: run the golden-set eval harness as part of pytest.

Keeps the evals wired into the existing pytest + coverage CI job; the standalone
report (`python -m evals.run_evals`) is the same suite with a human-readable table.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root, so `evals` imports

from evals.run_evals import main as run_evals  # noqa: E402


def test_golden_set_no_regression():
    exit_code = run_evals([])
    assert exit_code == 0, "golden-set eval regression: see python -m evals.run_evals"
