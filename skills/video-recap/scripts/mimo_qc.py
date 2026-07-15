#!/usr/bin/env python3
"""Public API and CLI entrypoint for advisory MiMo multimodal QC."""

from mimo_qc_evidence import collect_evidence, safe_mimo_config
from mimo_qc_observations import normalize_observations
from mimo_qc_payload import build_payload
from mimo_qc_runner import clear_report, main, run

__all__ = [
    "build_payload",
    "clear_report",
    "collect_evidence",
    "main",
    "normalize_observations",
    "run",
    "safe_mimo_config",
]

if __name__ == "__main__":
    raise SystemExit(main())
