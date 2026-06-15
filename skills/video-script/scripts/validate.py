#!/usr/bin/env python3
"""video-script validation entrypoint.

Validate (and, in full mode, time-align) an agent-written narration.json against the
understanding index produced by video-understanding. Writes narration_lint.json and,
in full mode, rewrites narration.json with quiet-window alignment applied.
"""
import argparse
import hashlib
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


def _value_fingerprint(value):
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.md5(payload.encode("utf-8")).hexdigest()


def _load_cut_clip_plan(work_dir):
    raw_plan = Path(work_dir) / "clip_plan.json"
    validated_plan = Path(work_dir) / "clip_plan_validated.json"
    if not validated_plan.exists():
        return _load(raw_plan)
    if not raw_plan.exists():
        return _load(validated_plan)

    raw = _load(raw_plan)
    validated = _load(validated_plan)
    if (
        isinstance(validated, dict)
        and validated.get("raw_plan_fingerprint") == _value_fingerprint(raw)
    ):
        return validated
    # The orchestrator validates before video-cut refreshes clip_plan_validated.json.
    # Without a matching raw-plan provenance fingerprint, lint against the current
    # raw plan even when mtimes are equal or misleading.
    return raw


def main():
    ap = argparse.ArgumentParser(description="Validate + align agent-written narration.json.")
    ap.add_argument("--work-dir", required=True)
    ap.add_argument("--mode", default="full", choices=["full", "cut", "cut_output"])
    args = ap.parse_args()

    work_dir = Path(args.work_dir)
    CONFIG["edit_mode"] = args.mode
    narration_path = work_dir / "narration.json"
    narration = _load(narration_path)
    if narration is None:
        raise SystemExit(f"缺少 {narration_path}；请先按 video-script 规则写解说词")
    vlm_analysis = _load(work_dir / "vlm_analysis.json")
    silence_periods = _load(work_dir / "silence_periods.json") or []
    if args.mode == "cut_output":
        # Two-pass cut: narration is authored in OUTPUT time against edited_source.mp4 — there is
        # no clip_plan to fall into and no source-time scene/quiet data to align to. Lint timing /
        # budget / overlap / density on the output timeline only; never realign or rewrite it.
        validate_narration_or_raise(narration, None, clip_plan=None, mode="full", work_dir=work_dir)
    elif args.mode == "cut":
        clip_plan = _load_cut_clip_plan(work_dir)
        validate_narration_or_raise(narration, vlm_analysis, clip_plan=clip_plan, mode="cut", work_dir=work_dir)
        narration = _validate_narration_budget(narration, vlm_analysis)
    else:
        validate_narration_or_raise(narration, vlm_analysis, clip_plan=None, mode="full", work_dir=work_dir)
        narration = _align_narration_to_quiet(narration, vlm_analysis, silence_periods)
        narration_path.write_text(json.dumps(narration, ensure_ascii=False, indent=2), encoding="utf-8")

    log(f"解说词验证完成: {len(narration)} 段")
    print(json.dumps({"status": "validated", "segments": len(narration),
                      "lint": str(work_dir / "narration_lint.json")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
