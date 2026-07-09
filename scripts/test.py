#!/usr/bin/env python3
"""Cross-platform test runner: one isolated pytest process per skill group.

Each skill ships its OWN lib.py (the bundle has no shared code), so a single
`pytest tests/` would import several skills' modules into one process and collide
on the `lib` (and `narration`) module names. Run one group per subprocess instead.

Works on macOS, Linux, and Windows (the bash equivalent is scripts/test.sh).

Usage: python scripts/test.py            # run every skill group
       python scripts/test.py script     # run one or more named groups
"""
import subprocess
import sys
from pathlib import Path

GROUPS = ["understanding", "cut", "voiceover", "assemble", "script", "orchestrator", "inspect"]


def main(argv):
    root = Path(__file__).resolve().parent.parent
    groups = argv or GROUPS
    failed = []
    for group in groups:
        print(f"== {group} ==", flush=True)
        result = subprocess.run(
            # -rs prints skip reasons so a silently-skipped real-render test (e.g. ffmpeg
            # missing) is visible in CI output instead of a bare "s".
            [sys.executable, "-m", "pytest", str(root / "tests" / group), "-q", "-rs"],
            cwd=str(root),
        )
        if result.returncode != 0:
            failed.append(group)
    if failed:
        print(f"FAILED groups: {', '.join(failed)}")
        return 1
    print("All skill groups passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
