"""Run and persist advisory MiMo QC stages."""

from __future__ import annotations

import argparse


import json


from pathlib import Path

from typing import Any, Callable, Mapping, Sequence

import qc_contract

from mimo_qc_report import _stage_reports, build_report, write_report
from mimo_qc_contract import ARTIFACT_NAME, DEFAULT_STAGE

JudgeCallable = Callable[
    [Mapping[str, Any]], Mapping[str, Any] | Sequence[Mapping[str, Any]]
]

FrameSampler = Callable[..., Sequence[Mapping[str, Any]]]


def run(
    work_dir: str | Path,
    *,
    stage: str = DEFAULT_STAGE,
    fixture: Any | None = None,
    dry_run: bool = False,
    judge: JudgeCallable | None = None,
    config: Mapping[str, Any] | None = None,
    final_output: str | Path | None = None,
    output: str | Path | None = None,
    live: bool = False,
    refresh: bool = False,
    frame_sampler: FrameSampler | None = None,
    api_call: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    path = Path(output) if output else Path(work_dir) / ARTIFACT_NAME
    existing = _stage_reports(path).get(stage)
    report = build_report(
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
        api_call=api_call,
    )
    written_path, aggregate = write_report(work_dir, report, output=output)
    return {"path": str(written_path), "report": aggregate}


def clear_report(work_dir: str | Path, *, output: str | Path | None = None) -> bool:
    path = Path(output) if output else Path(work_dir) / ARTIFACT_NAME
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


def _load_fixture(path: str | Path | None) -> Any | None:
    if not path:
        return None
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def main(
    argv: Sequence[str] | None = None,
    *,
    run_callable: Callable[..., dict[str, Any]] | None = None,
) -> int:
    parser = argparse.ArgumentParser(
        description="Write advisory MiMo QC from local recap evidence."
    )
    parser.add_argument("--work-dir", required=True)
    parser.add_argument(
        "--stage", default=DEFAULT_STAGE, choices=sorted(qc_contract.STAGES)
    )
    parser.add_argument("--fixture", help="offline model-response fixture JSON")
    parser.add_argument(
        "--live", action="store_true", help="make one live MiMo request for this stage"
    )
    parser.add_argument(
        "--refresh", action="store_true", help="ignore a matching stage cache"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="write evidence only; never access the network",
    )
    parser.add_argument("--model", help="override MIMO_QC_MODEL for this call")
    parser.add_argument("--final-output")
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    fixture = _load_fixture(args.fixture)
    config = {"mimo_qc_model": args.model} if args.model else None
    result = (run_callable or run)(
        args.work_dir,
        stage=args.stage,
        fixture=fixture,
        live=args.live and fixture is None,
        refresh=args.refresh,
        dry_run=args.dry_run or (not args.live and fixture is None),
        config=config,
        final_output=args.final_output,
        output=args.output,
    )
    print(
        json.dumps(
            {
                "ok": result["report"]["ok"],
                "status": result["report"]["metadata"]["status"],
                "path": result["path"],
            },
            ensure_ascii=False,
        )
    )
    return 0
