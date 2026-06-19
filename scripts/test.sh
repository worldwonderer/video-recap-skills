#!/usr/bin/env bash
# Run the video-recap test suite.
#
# Each skill ships its OWN lib.py (the bundle has no shared code), so the suites must run
# in SEPARATE processes — one per skill. A single `pytest tests/` would import several
# skills' modules into one process and collide on the `lib` (and `narration`) module names.
#
# Usage: bash scripts/test.sh            # run every skill group
#        bash scripts/test.sh script     # run one or more named groups
set -e
cd "$(dirname "$0")/.."

if [ "$#" -gt 0 ]; then
  test_groups="$*"
else
  test_groups="understanding cut voiceover assemble script orchestrator inspect"
fi

for skill_group in $test_groups; do
  echo "== ${skill_group} =="
  python3 -m pytest "tests/${skill_group}" -q
done
