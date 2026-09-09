"""Keep the backend's known type-checking debt from growing.

This repository adopted mypy after application development had started. The script runs
mypy over the complete application, counts today's known errors, and fails when a change
adds another error. Existing errors remain visible in the output so they can be removed
gradually. A separate strict check protects the small modules that are already clean.
"""

from __future__ import annotations

import re
import subprocess
import sys

BASELINE_ERROR_COUNT = 133
ERROR_PATTERN = re.compile(r"^.+:\d+: error:", re.MULTILINE)


def main() -> int:
    """Run mypy, report the current debt, and reject any increase."""

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            "app",
            "--no-error-summary",
            "--no-pretty",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    output = result.stdout + result.stderr
    if output:
        print(output, end="" if output.endswith("\n") else "\n")
    if result.returncode not in {0, 1}:
        print(f"mypy could not complete (exit {result.returncode}).", file=sys.stderr)
        return result.returncode

    error_count = len(ERROR_PATTERN.findall(output))
    print(f"mypy ratchet: {error_count} current errors; maximum {BASELINE_ERROR_COUNT}")
    if error_count > BASELINE_ERROR_COUNT:
        print("mypy error count increased; fix the new errors before merging.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
