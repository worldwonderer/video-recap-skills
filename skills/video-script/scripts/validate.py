#!/usr/bin/env python3
"""video-script validation entrypoint.

Validate (and, in full mode, time-align) an agent-written narration.json against the
understanding index produced by video-understanding. Writes narration_lint.json and,
in full mode, rewrites narration.json with quiet-window alignment applied.
"""
import argparse
import json
from pathlib import Path

from lib import CONFIG, log
from narration import (
    validate_narration_or_raise,
    _validate_narration_budget,
    _align_narration_to_quiet,
)


def _load(path):
    path = Path(path)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def main():
    ap = argparse.ArgumentParser(description="Validate + align agent-written narration.json.")
    ap.add_argument("--work-dir", required=True)
    ap.add_argument("--mode", default="full", choices=["full", "cut"])
    args = ap.parse_args()

    work_dir = Path(args.work_dir)
    CONFIG["edit_mode"] = args.mode
    narration_path = work_dir / "narration.json"
    narration = _load(narration_path)
    if narration is None:
        raise SystemExit(f"缺少 {narration_path}；请先按 video-script 规则写解说词")
    vlm_analysis = _load(work_dir / "vlm_analysis.json")
    silence_periods = _load(work_dir / "silence_periods.json") or []
    clip_plan = None
    if args.mode == "cut":
        clip_plan = _load(work_dir / "clip_plan_validated.json") or _load(work_dir / "clip_plan.json")

    validate_narration_or_raise(narration, vlm_analysis, clip_plan=clip_plan,
                                mode=args.mode, work_dir=work_dir)
    if args.mode == "cut":
        narration = _validate_narration_budget(narration, vlm_analysis)
    else:
        narration = _align_narration_to_quiet(narration, vlm_analysis, silence_periods)
        narration_path.write_text(json.dumps(narration, ensure_ascii=False, indent=2), encoding="utf-8")

    log(f"解说词验证完成: {len(narration)} 段")
    print(json.dumps({"status": "validated", "segments": len(narration),
                      "lint": str(work_dir / "narration_lint.json")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
