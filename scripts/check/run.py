"""Run repository checks using the selected Python interpreter."""

import subprocess
import sys
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    for command in (
        [sys.executable, "-m", "ruff", "check", "goodhands", "tests", "scripts"],
        [sys.executable, "-m", "ruff", "format", "--check", "goodhands", "tests", "scripts"],
        [sys.executable, "-m", "pytest", "-q", "--tb=short"],
    ):
        result = subprocess.run(command, cwd=root)
        if result.returncode:
            return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
