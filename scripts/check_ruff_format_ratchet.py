"""Keep the backend's pre-existing formatting debt from growing.

Ruff currently identifies 39 tracked Python files that predate the formatter. Reformatting
those application files belongs in a separate mechanical change. This check fails if the
number grows, while allowing later pull requests to reduce the baseline safely.
"""

from __future__ import annotations

import re
import subprocess
import sys

BASELINE_UNFORMATTED_FILES = 39
SUMMARY_PATTERN = re.compile(r"(\d+) files? would be reformatted")
TARGETS = ("app", "tests", "scripts", "migrations")


def main() -> int:
    """Run Ruff's formatter in check mode and reject new formatting debt."""

    result = subprocess.run(
        [sys.executable, "-m", "ruff", "format", "--check", *TARGETS],
        check=False,
        capture_output=True,
        text=True,
    )
    output = result.stdout + result.stderr
    if output:
        print(output, end="" if output.endswith("\n") else "\n")
    if result.returncode == 0:
        unformatted_count = 0
    elif match := SUMMARY_PATTERN.search(output):
        unformatted_count = int(match.group(1))
    else:
        print("Ruff format check did not produce a recognized result.", file=sys.stderr)
        return result.returncode or 1

    print(
        "Ruff format ratchet: "
        f"{unformatted_count} current files; maximum {BASELINE_UNFORMATTED_FILES}"
    )
    if unformatted_count > BASELINE_UNFORMATTED_FILES:
        print("Ruff formatting debt increased; format the changed files.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
