#!/usr/bin/env python3
"""Public API and CLI entrypoint for narration review."""

from evidence_bundle import (
    build_evidence_bundle,
    build_review_coverage_metadata,
    coverage_policy_v1,
    filter_evidence_by_ranges,
    render_evidence_bundle,
    validate_public_evidence_contract,
)
from review_grounding import remap_grounding_to_output_timeline
from review_response import (
    build_grounding_qc,
    build_review_messages,
    format_review_md,
    merge_review_findings,
    parse_review_response,
    write_grounding_qc,
)
from review_runner import main, review_narration

__all__ = [
    "build_evidence_bundle",
    "build_grounding_qc",
    "build_review_coverage_metadata",
    "build_review_messages",
    "coverage_policy_v1",
    "filter_evidence_by_ranges",
    "format_review_md",
    "main",
    "merge_review_findings",
    "parse_review_response",
    "remap_grounding_to_output_timeline",
    "render_evidence_bundle",
    "review_narration",
    "validate_public_evidence_contract",
    "write_grounding_qc",
]

if __name__ == "__main__":
    main()
