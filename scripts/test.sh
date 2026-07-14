#!/usr/bin/env bash
# Thin compatibility wrapper. scripts/test.py is the single source of truth for
# group discovery, ordering, isolation, reporting, and exit status.
set -euo pipefail
cd "$(dirname "$0")/.."
exec "${PYTHON:-python3}" scripts/test.py "$@"
