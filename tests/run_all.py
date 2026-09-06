"""Run all local tests without pytest (stdlib only).

  python tests/run_all.py
  # or: python -m pytest tests -q   (if pytest installed)
"""

from __future__ import annotations

import importlib
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

MODULES = (
    "tests.test_verify",
    "tests.test_catalog",
    "tests.test_attempts",
    "tests.test_challenge_card",
    "tests.test_congrats",
    "tests.test_progress_status",
    "tests.test_shame",
    "tests.test_monthly_digest",
)


def main() -> int:
    failed = 0
    passed = 0
    for mod_name in MODULES:
        mod = importlib.import_module(mod_name)
        tests = [
            (n, getattr(mod, n))
            for n in sorted(dir(mod))
            if n.startswith("test_") and callable(getattr(mod, n))
        ]
        for name, fn in tests:
            label = f"{mod_name}.{name}"
            try:
                fn()
                print(f"PASS {label}")
                passed += 1
            except Exception:
                failed += 1
                print(f"FAIL {label}")
                traceback.print_exc()
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
