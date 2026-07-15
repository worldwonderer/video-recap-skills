#!/usr/bin/env python3
"""video-script validation entrypoint.

Validate an agent-written narration.json against the local understanding index.
Writes narration_lint.json and, in full mode, rewrites only normalized fields plus
the measured overlaps_speech flag; authored start/end timing is preserved exactly.
"""

import argparse
import json
import math
from pathlib import Path

from lib import CONFIG, log, stable_hash
from narration_lint import (
    _validate_narration_budget,
    validate_narration_or_raise,
)
from speech_ownership import measure_narration_speech_ownership
from timeline_fusion import _align_narration_to_quiet


def _load(path):
    path = Path(path)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _load_cut_clip_plan(work_dir):
    raw_plan = Path(work_dir) / "clip_plan.json"
    validated_plan = Path(work_dir) / "clip_plan_validated.json"
    if not validated_plan.exists():
        return _load(raw_plan)
    if not raw_plan.exists():
        return _load(validated_plan)

    raw = _load(raw_plan)
    validated = _load(validated_plan)
    if isinstance(validated, dict) and validated.get(
        "raw_plan_fingerprint"
    ) == stable_hash(raw):
        return validated
    # Validation may run before the cut stage refreshes clip_plan_validated.json.
    # Without a matching raw-plan provenance fingerprint, lint against the current
    # raw plan even when mtimes are equal or misleading.
    return raw


def _validate_output_timeline_bounds(narration, output_duration, tolerance=0.05):
    """Hard-gate cut_output narration against the rendered output timeline.

    cut_output narration is authored in edited_source.mp4 time. If any segment falls outside
    that media duration, fail before TTS/render instead of spending time on unusable audio.
    """
    try:
        duration = float(output_duration)
    except (TypeError, ValueError):
        raise SystemExit(f"output_duration must be numeric, got {output_duration!r}")
    if not math.isfinite(duration) or duration <= 0:
        raise SystemExit(
            f"output_duration must be finite and positive, got output_duration={duration:.3f}"
        )
    if not isinstance(narration, list):
        return

    problems = []
    for idx, seg in enumerate(narration):
        if not isinstance(seg, dict):
            continue
        try:
            start = float(seg.get("start"))
            end = float(seg.get("end"))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(start) or not math.isfinite(end):
            problems.append(f"segment {idx} has non-finite time [{start!r},{end!r}]")
            continue
        if end <= -tolerance or start >= duration + tolerance:
            problems.append(
                f"segment {idx} [{start:.3f},{end:.3f}] fully outside output_duration={duration:.3f}"
            )
            continue
        if start < -tolerance:
            problems.append(
                f"segment {idx} start={start:.3f} before output timeline (output_duration={duration:.3f})"
            )
        if end > duration + tolerance:
            problems.append(
                f"segment {idx} end={end:.3f} exceeds output_duration={duration:.3f}"
            )
    if problems:
        raise SystemExit(
            "cut_output narration exceeds rendered output timeline: "
            + "; ".join(problems)
        )


def main():
    ap = argparse.ArgumentParser(
        description="Validate + align agent-written narration.json."
    )
    ap.add_argument("--work-dir", required=True)
    ap.add_argument("--mode", default="full", choices=["full", "cut", "cut_output"])
    ap.add_argument(
        "--output-duration",
        type=float,
        default=None,
        help="cut_output: rendered edited_source.mp4 duration in seconds",
    )
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
        # no source-time clip membership check. Derive speech ownership from the mapped output
        # evidence, then persist that measured flag for voiceover/assemble instead of trusting JSON.
        narration = measure_narration_speech_ownership(
            narration, work_dir, mode="cut_output"
        )
        validate_narration_or_raise(
            narration, None, clip_plan=None, mode="cut_output", work_dir=work_dir
        )
        if args.output_duration is None:
            raise SystemExit("--output-duration is required when --mode cut_output")
        _validate_output_timeline_bounds(narration, args.output_duration)
        narration_path.write_text(
            json.dumps(narration, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    elif args.mode == "cut":
        clip_plan = _load_cut_clip_plan(work_dir)
        validate_narration_or_raise(
            narration, vlm_analysis, clip_plan=clip_plan, mode="cut", work_dir=work_dir
        )
        narration = _validate_narration_budget(narration, vlm_analysis)
    else:
        validate_narration_or_raise(
            narration, vlm_analysis, clip_plan=None, mode="full", work_dir=work_dir
        )
        narration = _align_narration_to_quiet(narration, vlm_analysis, silence_periods)
        narration_path.write_text(
            json.dumps(narration, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    log(f"解说词验证完成: {len(narration)} 段")
    print(
        json.dumps(
            {
                "status": "validated",
                "segments": len(narration),
                "lint": str(work_dir / "narration_lint.json"),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
