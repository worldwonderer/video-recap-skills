#!/usr/bin/env python3
"""Public API and CLI entrypoint for advisory MiMo multimodal QC."""

import mimo_qc_report
import mimo_qc_runner
from mimo_qc_evidence import collect_evidence, safe_mimo_config
from mimo_qc_observations import normalize_observations
from mimo_qc_payload import build_payload
from mimo_qc_client import mimo_qc_api_call
from mimo_qc_contract import DEFAULT_STAGE

sample_video_frames = mimo_qc_report.sample_video_frames
write_report = mimo_qc_report.write_report
clear_report = mimo_qc_runner.clear_report


def build_report(
    work_dir,
    *,
    stage=DEFAULT_STAGE,
    fixture=None,
    dry_run=False,
    judge=None,
    config=None,
    final_output=None,
    live=False,
    refresh=False,
    frame_sampler=None,
    existing=None,
):
    return mimo_qc_report.build_report(
        work_dir,
        stage=stage,
        fixture=fixture,
        dry_run=dry_run,
        judge=judge,
        config=config,
        final_output=final_output,
        live=live,
        refresh=refresh,
        frame_sampler=frame_sampler,
        existing=existing,
        api_call=mimo_qc_api_call,
    )


def run(
    work_dir,
    *,
    stage=DEFAULT_STAGE,
    fixture=None,
    dry_run=False,
    judge=None,
    config=None,
    final_output=None,
    output=None,
    live=False,
    refresh=False,
    frame_sampler=None,
):
    return mimo_qc_runner.run(
        work_dir,
        stage=stage,
        fixture=fixture,
        dry_run=dry_run,
        judge=judge,
        config=config,
        final_output=final_output,
        output=output,
        live=live,
        refresh=refresh,
        frame_sampler=frame_sampler,
        api_call=mimo_qc_api_call,
    )


def main(argv=None):
    return mimo_qc_runner.main(argv, run_callable=run)

__all__ = [
    "build_payload",
    "build_report",
    "clear_report",
    "collect_evidence",
    "main",
    "mimo_qc_api_call",
    "normalize_observations",
    "run",
    "sample_video_frames",
    "safe_mimo_config",
    "write_report",
]

if __name__ == "__main__":
    raise SystemExit(main())
