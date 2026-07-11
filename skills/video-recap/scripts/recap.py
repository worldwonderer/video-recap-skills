#!/usr/bin/env python3
"""video-recap orchestrator.

Chains the independent video-* stage skills into a full narrated recap:

  video-understanding  ->  (agent writes narration.json per video-script)  ->
  [video-cut]  ->  video-voiceover  ->  video-assemble

Each stage is a self-contained sibling skill invoked as a subprocess; they communicate
only through JSON/MP4 artifacts in the shared work_dir. Resume by rerunning the same
command after writing narration.json; Phase B verifies a run manifest before reusing
work_dir artifacts.
"""
import argparse
import hashlib
import json
import math
import os
import shlex
import subprocess
import sys
from pathlib import Path

import qc_contract
# Same-skill local QC helper exception: final_qc is imported directly so recap can
# write report-only post-render QC without subprocess overhead or credential/env
# expansion. Sibling skills still communicate through subprocess artifacts.
import final_qc
from doctor import ffmpeg_has_subtitles_filter
import materials as material_lib

BUNDLE = Path(__file__).resolve().parents[2]  # the skills/ directory
RUN_MANIFEST = "recap_run_manifest.json"
ASSEMBLY_MANIFEST = "assembly_manifest.json"
PHASE_LEDGER = "recap_phase.json"
MULTI_SOURCE_MANIFEST = "multi_source_manifest.json"


CUT_TIMELINE_CRAFT_BULLETS = [
    "- Build one story spine, not unordered highlights: 0–1 optional cold-open/high-impact clip may come first, then return to setup → turn → payoff in a coherent arc.",
    "- Use clip `reason` as craft intent where useful: `cold_open`, `setup`, `turn`, or `payoff` plus a concrete why.",
    "- Prefer causal/reveal/decision/emotional beats; skip credits, ads, repeated static filler, and watermark/无法描述 stretches.",
    "- End clips on complete spoken lines or quiet windows so original audio does not enter/exit mid-sentence.",
]

MULTI_SOURCE_NARRATION_CRAFT_BULLETS = [
    "- Write against the OUTPUT timeline of edited_source.mp4 only; do not use original source timestamps for narration.json.",
    "- Recap in BLOCKS of 2–4 complete sentences, not isolated caption fragments; each block should advance the same story spine.",
    "- Leave deliberate original-audio gaps between blocks for strong source moments; tee up the gap before it plays and react to it after it plays.",
    "- Use the kept-clip map and each source work_dir to retrieve source facts, names, ASR, and per-source brief context when a beat is unclear.",
]




def _json_safe(value):
    """Return a JSON-serializable, secret-redacted copy for local QC metadata."""
    def convert(item):
        if isinstance(item, Path):
            return str(item)
        if isinstance(item, dict):
            return {str(k): convert(v) for k, v in item.items()}
        if isinstance(item, (list, tuple)):
            return [convert(v) for v in item]
        return item

    return qc_contract.redact_secrets(convert(value))


def _load_preflight_stage_reports(work_dir):
    path = Path(work_dir) / "preflight_qc.json"
    if not path.exists():
        return {}
    data = _load_json(path)
    if not isinstance(data, dict):
        raise qc_contract.QCContractError("preflight_qc.json must be a JSON object")
    qc_contract.validate_report(data)
    stages = None
    metadata = data.get("metadata")
    if isinstance(metadata, dict) and isinstance(metadata.get("stages"), dict):
        stages = metadata.get("stages")
    elif isinstance(data.get("stages"), dict):
        stages = data.get("stages")
    if stages is None:
        return {data["stage"]: data}
    stage_reports = {}
    for stage, report in stages.items():
        if not isinstance(report, dict):
            raise qc_contract.QCContractError(f"preflight stage report must be an object: {stage}")
        qc_contract.validate_report(report)
        if report.get("stage") != stage:
            raise qc_contract.QCContractError(f"preflight stage key does not match report stage: {stage}")
        stage_reports[stage] = report
    return stage_reports


def _write_shift_left_stage_qc(work_dir, stage, metadata=None, findings=None):
    """Write/roll up local shift-left QC for one pipeline stage.

    This is a local contract artifact only: no MiMo/deep eval calls, no repair, and
    no credential persistence. Any validation/write failure is allowed to raise.
    """
    work_dir = Path(work_dir)
    path = work_dir / "preflight_qc.json"
    stage_report = qc_contract.build_report(
        artifact="preflight_qc.json",
        stage=stage,
        findings=findings or [],
        metadata=_json_safe(metadata or {}),
    )
    stage_reports = _load_preflight_stage_reports(work_dir)
    stage_reports[stage] = stage_report
    aggregate_findings = []
    for report in stage_reports.values():
        aggregate_findings.extend(dict(f) for f in report.get("findings", []))
    top_metadata = dict(stage_report.get("metadata") or {})
    top_metadata["latest_stage"] = stage
    top_metadata["stages"] = stage_reports
    top_report = qc_contract.build_report(
        artifact="preflight_qc.json",
        stage=stage,
        findings=aggregate_findings,
        metadata=_json_safe(top_metadata),
    )
    qc_contract.validate_report(top_report)
    path.write_text(json.dumps(top_report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    qc_contract.validate_report(_load_json(path))
    return top_report


def _tts_qc_metadata(work_dir):
    work_dir = Path(work_dir)
    metadata = {}
    tts_meta = _load_json(work_dir / "tts_meta.json")
    if tts_meta is not None:
        metadata["tts_meta"] = tts_meta
    tts_dir = work_dir / "tts_segments"
    if tts_dir.exists() and tts_dir.is_dir():
        metadata["tts_segments"] = [p.relative_to(work_dir).as_posix() for p in sorted(tts_dir.iterdir()) if p.is_file()]
    return metadata


def _post_render_qc_metadata(work_dir, final_output):
    work_dir = Path(work_dir)
    metadata = {"final_output": str(final_output) if final_output is not None else None}
    assembly_manifest = _load_json(work_dir / ASSEMBLY_MANIFEST)
    if assembly_manifest is not None:
        metadata["assembly_manifest"] = assembly_manifest
    return metadata


def _write_final_qc_reports(work_dir, final_output):
    """Write report-only final QC artifacts after render.

    final_qc.run converts ffprobe unavailability/failure into deterministic
    blockers; only unexpected schema/write errors propagate.
    """
    return final_qc.run(work_dir, final_output=final_output)


def _print_final_qc_pointer(result):
    """Surface a report-only final_qc/golden_eval FAIL so the shift-left QC is
    not a silent no-op. Advisory only: it never changes the exit status."""
    if not isinstance(result, dict):
        return
    problems = []
    for key in ("final_qc", "golden_eval"):
        section = result.get(key)
        if isinstance(section, dict) and section.get("ok") is False:
            problems.append(f"{key} blocker_count={section.get('blocker_count', '?')}")
    if problems:
        print(
            "[video-recap] ⚠️  最终 QC 未通过（仅报告，不阻断）: "
            + "; ".join(problems)
            + "；详见 final_qc.json / golden_eval.json"
        )


def _entry(skill, script):
    return BUNDLE / skill / "scripts" / script


def _run(skill, script, *cli_args):
    cmd = [sys.executable, str(_entry(skill, script)), *map(str, cli_args)]
    print(f"[video-recap] ▶ {skill}/{script}", flush=True)
    res = subprocess.run(cmd)
    if res.returncode != 0:
        raise SystemExit(f"{skill}/{script} 失败 (exit {res.returncode})")


def _env_bool(name, default=False):
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _optional_env_int(name):
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer; got {raw!r}") from exc
    return None if value == -1 else value


def _read_video_duration_or_raise(path):
    """Return media duration via ffprobe, or hard-fail before downstream TTS/render."""
    path = Path(path)
    cmd = [
        "ffprobe", "-v", "quiet", "-show_entries", "format=duration",
        "-of", "csv=p=0", str(path),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        detail = (res.stderr or res.stdout or "ffprobe failed").strip()
        raise SystemExit(f"无法读取成片时长: {path} ({detail})")
    try:
        duration = float(res.stdout.strip())
    except (TypeError, ValueError):
        raise SystemExit(f"无法读取成片时长: {path} (ffprobe 输出无效: {res.stdout!r})")
    if not math.isfinite(duration) or duration <= 0:
        raise SystemExit(f"无法读取成片时长: {path} (duration={duration:.3f})")
    return duration


def _probe_display_height_or_raise(path, *, require_square_pixels=False):
    """Return ffmpeg's display-coordinate height, accounting for rotation and SAR."""
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries",
        "stream=width,height,sample_aspect_ratio:stream_tags=rotate:stream_side_data=rotation",
        "-of", "json", str(path),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    try:
        stream = (json.loads(res.stdout or "{}").get("streams") or [])[0]
        width, height = int(stream["width"]), int(stream["height"])
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        detail = (res.stderr or res.stdout or "ffprobe failed").strip()
        raise SystemExit(f"无法读取视频画布: {path} ({detail})")
    sar = str(stream.get("sample_aspect_ratio") or "1:1")
    try:
        num, den = sar.split(":", 1)
        sar_ratio = float(num) / float(den)
        display_width = max(1, int(round(width * sar_ratio)))
    except (TypeError, ValueError, ZeroDivisionError):
        sar_ratio = math.nan
        display_width = width
    if require_square_pixels and (not math.isfinite(sar_ratio) or abs(sar_ratio - 1.0) >= 1e-9):
        raise SystemExit(
            f"subtitle Y coordinates currently require square-pixel video (SAR 1:1); got {sar}"
        )
    rotation_values = [
        (stream.get("tags") or {}).get("rotate"),
        *(item.get("rotation") for item in stream.get("side_data_list") or [] if isinstance(item, dict)),
    ]
    rotation = 0
    for value in rotation_values:
        if value not in (None, ""):
            try:
                rotation = int(round(float(value))) % 360
                break
            except (TypeError, ValueError):
                pass
    return display_width if rotation in {90, 270} else height


def _review_narration_enabled(args):
    if getattr(args, "review_narration", None) is not None:
        return bool(args.review_narration)
    return _env_bool("REVIEW_NARRATION", True)


def _require_narration_review(args):
    if getattr(args, "require_narration_review", False):
        return True
    return _env_bool("REQUIRE_NARRATION_REVIEW", False)


def _review_result_status(work_dir):
    data = _load_json(Path(work_dir) / "narration_review.json")
    if not isinstance(data, dict):
        return {"ok": False, "reason": "missing or invalid narration_review.json"}
    findings = [f for f in (data.get("findings") or []) if isinstance(f, dict)]
    n_err = sum(1 for f in findings if f.get("severity") == "error")
    if data.get("parse_error"):
        return {"ok": False, "reason": "parse_error", "review": data, "errors": n_err}
    if n_err:
        return {"ok": False, "reason": f"error {n_err}", "review": data, "errors": n_err}
    # Strict mode gates ONLY on parse_error or factual `error` findings — never on the model's
    # holistic verdict. review.py deliberately clamps craft-class severities to `warning`, so a
    # bare REVISE/FAIL with no error finding would otherwise smuggle subjective judgment back
    # through the gate. The verdict stays an advisory signal in narration_review.*.
    return {"ok": True, "reason": "ok", "review": data, "errors": n_err}


def _clear_narration_review_artifacts(work_dir):
    """Remove prior review artifacts before a fresh pre-TTS review run.

    The review is allowed to fail open in advisory mode, but completion output and
    strict gating must never accidentally trust a stale narration_review.* from an
    earlier run.
    """
    for name in ("narration_review.json", "narration_review.md"):
        try:
            (Path(work_dir) / name).unlink()
        except FileNotFoundError:
            pass


def _run_narration_review(work_dir, args, *, timeline="source"):
    """Run quality review before TTS.

    Default mode remains advisory/fail-open. Strict mode
    (`--require-narration-review` or REQUIRE_NARRATION_REVIEW) hard-fails before
    TTS when review is unavailable, unparsable, or reports error findings.
    Returns True only when review.py completed, so completion messages do not
    point at stale review artifacts after opt-out/fail-open runs.
    """
    strict = _require_narration_review(args)
    if not _review_narration_enabled(args) and not strict:
        return False
    try:
        _clear_narration_review_artifacts(work_dir)
        # Always pin the grounding timeline explicitly so the orchestrated review never falls
        # through to review.py's auto-detect (which could flip on stale cut artifacts left in a
        # reused full-mode work_dir).
        rargs = ["--work-dir", work_dir, "--timeline", timeline]
        if strict and timeline == "cut_output":
            rargs.append("--strict-evidence")
        _run("video-script", "review.py", *rargs)
    except SystemExit as exc:
        if strict:
            raise SystemExit(f"严格解说评审失败，已阻止 TTS: {exc}")
        print(f"[video-recap] ⚠️ 建议性评审失败，继续执行 TTS: {exc}", flush=True)
        return False

    status = _review_result_status(work_dir)
    if strict and not status["ok"]:
        raise SystemExit(f"严格解说评审未通过，已阻止 TTS: {status['reason']}")
    if strict:
        print("[video-recap] ✅ 严格解说评审通过，继续 TTS", flush=True)
    return True


def _file_fingerprint(path, chunk_size=1024 * 1024):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


def _analysis_settings(args):
    return {
        "context": args.context,
        "scene_threshold": args.scene_threshold,
        "style": args.style,
        "edit_mode": args.edit_mode,
        "target_duration": args.target_duration,
        "skip_asr": bool(args.skip_asr),
        "mimo_video_overview": bool(args.mimo_video_overview),
        "consolidate": bool(args.consolidate),
        "consolidate_asr": bool(args.consolidate_asr),
    }


def _material_settings_fingerprint(args):
    return material_lib.settings_fingerprint(_analysis_settings(args))


def _coerce_videos(video_or_videos):
    if isinstance(video_or_videos, (list, tuple)):
        return [Path(v).resolve() for v in video_or_videos]
    return [Path(video_or_videos).resolve()]


def _run_manifest_payload(video, args):
    return {
        "schema_version": 1,
        "source_video": str(Path(video).resolve()),
        "source_video_fingerprint": _file_fingerprint(video),
        "settings": _analysis_settings(args),
    }


def _write_run_manifest(work_dir, video, args):
    payload = _run_manifest_payload(video, args)
    (work_dir / RUN_MANIFEST).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _build_multi_source_records(videos, work_dir, args):
    records = []
    for video in _coerce_videos(videos):
        fp = _file_fingerprint(video)
        records.append({
            "source_path": str(video),
            "source_name": video.name,
            "source_video_fingerprint": fp,
            "settings_fingerprint": _material_settings_fingerprint(args),
            "material_id": material_lib.material_id_for(video, fp),
        })
    records = material_lib.assign_source_ids(records)
    for record in records:
        record["source_work_dir"] = f"sources/{record['source_id']}"
    return records


def _multi_run_manifest_payload(videos, args, source_records):
    return {
        "schema_version": 2,
        "mode": "multi_source",
        "sources": [
            {
                "source_id": s.get("source_id"),
                "source_path": s.get("source_path"),
                "source_video_fingerprint": s.get("source_video_fingerprint"),
                "source_work_dir": s.get("source_work_dir"),
                "material_id": s.get("material_id"),
            }
            for s in source_records
        ],
        "source_videos": [str(v) for v in _coerce_videos(videos)],
        "settings": _analysis_settings(args),
    }


def _write_project_run_manifest(work_dir, videos, args, source_records):
    payload = _multi_run_manifest_payload(videos, args, source_records)
    (work_dir / RUN_MANIFEST).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_multi_source_manifest(work_dir, source_records):
    path = Path(work_dir) / MULTI_SOURCE_MANIFEST
    payload = {
        "schema_version": 1,
        "sources": [
            {
                "source_id": s["source_id"],
                "source_path": s["source_path"],
                "source_name": s["source_name"],
                "source_video_fingerprint": s["source_video_fingerprint"],
                "source_work_dir": s["source_work_dir"],
                "material_id": s.get("material_id"),
            }
            for s in source_records
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _load_multi_source_manifest(work_dir):
    data = _load_json(Path(work_dir) / MULTI_SOURCE_MANIFEST)
    if isinstance(data, dict) and isinstance(data.get("sources"), list):
        return data
    return None


def _load_run_manifest(work_dir):
    path = Path(work_dir) / RUN_MANIFEST
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None


def _load_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None


def _burn_subtitles_intended(args):
    """Effective burn-subtitles state at orchestrator level. Mirrors video-assemble's
    CONFIG default `env_bool("BURN_SUBTITLES", True)` (burn is ON by default); an explicit
    CLI flag (--burn-subtitles / --no-burn-subtitles) overrides the env."""
    if getattr(args, "burn_subtitles", None) is not None:
        return bool(args.burn_subtitles)
    raw = os.environ.get("BURN_SUBTITLES")
    if raw is None or raw == "":
        return True
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _ffmpeg_present_but_cannot_burn():
    """True only when ffmpeg EXISTS but lacks the libass `subtitles` filter — the specific
    "subtitle-burn environment unsupported" case. Returns False when ffmpeg is absent
    entirely: that is a more fundamental problem that surfaces at the first stage (understand
    calls ffprobe/ffmpeg) and is reported by doctor, so this guard stays narrow — and does
    not fire in mocked, ffmpeg-less test environments."""
    import shutil
    if shutil.which("ffmpeg") is None:
        return False
    return not ffmpeg_has_subtitles_filter()


def _preflight_burn_subtitles(args):
    """Fail fast BEFORE any understanding/VLM/ASR/TTS spend when subtitle burn-in is on but
    this ffmpeg can't burn it. Without it the run only dies at the final assemble
    `-vf subtitles=` step — after the whole expensive pipeline has run."""
    if not _burn_subtitles_intended(args):
        return
    if _ffmpeg_present_but_cannot_burn():
        raise SystemExit(
            "字幕烧录已开启，但当前 ffmpeg 不支持 subtitles/libass 滤镜，整条流程会跑到最后渲染才失败。\n"
            "  解决其一：(1) 安装带 libass 的 ffmpeg；(2) 加 --no-burn-subtitles 关闭烧录"
            "（仍输出 .srt 外挂字幕）。\n"
            f"  自检：python3 {shlex.quote(str(_entry('video-recap', 'doctor.py')))}")




_ALLOWED_VISUAL_OVERLAY_TYPES = {"top_title", "inline_label_or_callout"}
_VISUAL_OVERLAYS = "visual_overlays.json"


def _finite_number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _iter_narration_segments(narration_data):
    if isinstance(narration_data, list):
        yield from (item for item in narration_data if isinstance(item, dict))
        return
    if not isinstance(narration_data, dict):
        return
    for key in ("segments", "narration", "items"):
        value = narration_data.get(key)
        if isinstance(value, list):
            yield from (item for item in value if isinstance(item, dict))
            return


def _canonical_visual_overlay(overlay, segment=None):
    """Return the first-release assemble overlay contract, or None.

    Recap owns the handoff artifact but does not invent richer overlay semantics. Only the
    two overlay types implemented by assemble are allowed through; all unknown platform or
    future types stay out of the canonical first-release file.
    """
    if not isinstance(overlay, dict):
        return None
    typ = overlay.get("type")
    if typ not in _ALLOWED_VISUAL_OVERLAY_TYPES:
        return None
    text = overlay.get("text")
    if text is None or str(text) == "":
        return None
    seg = segment if isinstance(segment, dict) else {}
    start = _finite_number(overlay.get("start"))
    end = _finite_number(overlay.get("end"))
    if start is None:
        start = _finite_number(seg.get("start"))
    if end is None:
        end = _finite_number(seg.get("end"))
    if start is None or end is None or end <= start:
        return None

    item = {"type": typ, "text": str(text), "start": start, "end": end}
    # Preserve renderer-supported placement hints supplied by upstream facts; do not add new
    # semantic overlay kinds or infer platform-specific cards/chapters.
    for key in ("anchor", "x", "y", "max_width", "style"):
        if key in overlay:
            item[key] = overlay[key]
    return item


def _extract_visual_overlays_from_narration(narration_path):
    data = _load_json(narration_path)
    overlays = []
    for segment in _iter_narration_segments(data):
        raw = segment.get("visual_overlays")
        if not isinstance(raw, list):
            continue
        for overlay in raw:
            item = _canonical_visual_overlay(overlay, segment)
            if item is not None:
                overlays.append(item)
    return overlays


def _write_canonical_visual_overlays(work_dir, narration_path):
    """Write assemble's canonical work_dir/visual_overlays.json recap handoff.

    Direct assemble still supports user-authored/manual visual_overlays.json files. Once
    recap owns the handoff for a narration, however, the canonical artifact must be a
    deterministic reflection of the current narration so reused work_dirs cannot render
    stale overlays from a previous run. Unsupported/future overlay types are filtered out
    and represented as an explicit empty overlay list.
    """
    work_dir = Path(work_dir)
    path = work_dir / _VISUAL_OVERLAYS
    overlays = _extract_visual_overlays_from_narration(narration_path)
    payload = {"schema_version": 1, "overlays": overlays}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[video-recap] 🧩 visual overlays: {len(overlays)} → {path}", flush=True)
    return path


def _print_grounding_qc_pointer(work_dir):
    qc_path = Path(work_dir) / "grounding_qc.json"
    if not qc_path.exists():
        return
    data = _load_json(qc_path)
    if isinstance(data, dict):
        verdict = data.get("verdict", "unknown")
        ranges = (data.get("review_coverage") or {}).get("time_ranges") or []
        warnings = data.get("warnings") or []
        suffix = f" · warnings {len(warnings)}" if warnings else ""
        print(f"[video-recap] 🧭 Grounding QC: {verdict} · ranges {len(ranges)}{suffix} → {qc_path}", flush=True)
    else:
        print(f"[video-recap] 🧭 Grounding QC → {qc_path}", flush=True)


def _print_narration_review_pointer(work_dir, *, review_ran=True):
    """Surface the advisory narration review produced by this run, if any.

    Review is optional/fail-open. Avoid surfacing a stale narration_review.md from an
    older run when review was disabled or failed before producing fresh artifacts.
    """
    _print_grounding_qc_pointer(work_dir)
    if not review_ran:
        return
    review_md = Path(work_dir) / "narration_review.md"
    if not review_md.exists():
        return
    data = _load_json(Path(work_dir) / "narration_review.json")
    if isinstance(data, dict):
        findings = [f for f in (data.get("findings") or []) if isinstance(f, dict)]
        n_err = sum(1 for f in findings if f.get("severity") == "error")
        tag = str(data.get("verdict") or "见文件")
        print(f"[video-recap] 📋 解说评审（建议性，不拦截）: {tag} · "
              f"{len(findings)} 条意见（error {n_err}）→ {review_md}")
    else:
        print(f"[video-recap] 📋 解说评审意见 → {review_md}")


def _settings_for_compare(settings):
    """Settings that, if changed, invalidate reusing an existing work_dir on resume.

    `consolidate`/`consolidate_asr` are EXCLUDED: they only ADD an optional understanding
    artifact and never re-run Phase A on a Phase-B resume, so a stored manifest carrying the
    old default (or missing the key entirely, pre-dating it) must still resume — otherwise
    flipping `--consolidate`'s default ON would hard-fail every in-flight work_dir.
    """
    s = dict(settings or {})
    s.pop("consolidate", None)
    s.pop("consolidate_asr", None)
    return s


def _manifest_mismatches(work_dir, video, args):
    expected = _run_manifest_payload(video, args)
    actual = _load_run_manifest(work_dir)
    if not actual:
        return ["缺少或无法读取 recap_run_manifest.json；不能证明 work_dir 属于当前视频/参数"]
    mismatches = []
    for key in ("source_video", "source_video_fingerprint"):
        if actual.get(key) != expected.get(key):
            mismatches.append(f"{key}: expected {expected.get(key)!r}, got {actual.get(key)!r}")
    if _settings_for_compare(actual.get("settings")) != _settings_for_compare(expected.get("settings")):
        mismatches.append("settings: 当前 CLI/env 参数与 Phase A manifest 不匹配")
    return mismatches


def _multi_manifest_mismatches(work_dir, videos, args, source_records):
    expected = _multi_run_manifest_payload(videos, args, source_records)
    actual = _load_run_manifest(work_dir)
    if not actual:
        return ["缺少或无法读取 recap_run_manifest.json；不能证明 work_dir 属于当前多视频/参数"]
    mismatches = []
    if actual.get("mode") != "multi_source":
        mismatches.append(f"mode: expected 'multi_source', got {actual.get('mode')!r}")
    expected_sources = [
        {
            "source_id": s.get("source_id"),
            "source_path": s.get("source_path"),
            "source_video_fingerprint": s.get("source_video_fingerprint"),
        }
        for s in expected.get("sources", [])
    ]
    actual_sources = [
        {
            "source_id": s.get("source_id"),
            "source_path": s.get("source_path"),
            "source_video_fingerprint": s.get("source_video_fingerprint"),
        }
        for s in actual.get("sources", [])
        if isinstance(s, dict)
    ]
    if actual_sources != expected_sources:
        mismatches.append("sources: 当前输入视频列表/顺序/source_id/fingerprint 与 Phase A manifest 不匹配")
    if _settings_for_compare(actual.get("settings")) != _settings_for_compare(expected.get("settings")):
        mismatches.append("settings: 当前 CLI/env 参数与 Phase A manifest 不匹配")
    return mismatches


def _read_assembly_output(work_dir):
    manifest = _load_json(Path(work_dir) / ASSEMBLY_MANIFEST)
    if isinstance(manifest, dict) and manifest.get("final_output"):
        return Path(manifest["final_output"])
    return None


def _file_md5(path):
    path = Path(path)
    return hashlib.md5(path.read_bytes()).hexdigest() if path.exists() else None


def _read_phase_ledger(work_dir):
    """Phase ledger (cut mode): which artifacts exist and the clip_plan/narration they match.

    Lets resume be driven by recorded phase state rather than bare file existence — the
    prerequisite for the cut-first/narrate-second two-pause flow, and the guard that keeps a
    narration written for one clip_plan from silently driving a different cut into TTS.
    """
    ledger = _load_json(Path(work_dir) / PHASE_LEDGER)
    return ledger if isinstance(ledger, dict) else None


def _write_phase_ledger(work_dir, **fields):
    ledger = _read_phase_ledger(work_dir) or {}
    ledger.update(fields)
    (Path(work_dir) / PHASE_LEDGER).write_text(
        json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8")
    return ledger


def _cut_narration_is_stale(ledger, current_clip_plan_fp):
    """Two-pass cut: the narration is authored against the rendered cut shown at the A2 pause,
    i.e. against the clip_plan recorded in the ledger. If clip_plan changed since (a re-cut)
    while that narration is still present, it describes the OLD cut — stale."""
    if not ledger:
        return False
    recorded_cp = ledger.get("clip_plan_fingerprint")
    return bool(recorded_cp is not None and recorded_cp != current_clip_plan_fp)


def _continuation_command(video, work_dir, args):
    videos = _coerce_videos(video)
    parts = [sys.executable, str(_entry("video-recap", "recap.py")), *[str(v) for v in videos], "--work-dir", str(work_dir)]
    if args.context:
        parts += ["--context", args.context]
    if args.scene_threshold is not None:
        parts += ["--scene-threshold", str(args.scene_threshold)]
    if args.style != "纪录片":
        parts += ["--style", args.style]
    if args.edit_mode != "full":
        parts += ["--edit-mode", args.edit_mode]
    if args.target_duration:
        parts += ["--target-duration", args.target_duration]
    if getattr(args, "allow_duration_drift", False):
        parts.append("--allow-duration-drift")
    if getattr(args, "allow_sparse_cut", False):
        parts.append("--allow-sparse-cut")
    if args.skip_asr:
        parts.append("--skip-asr")
    if args.mimo_video_overview:
        parts.append("--mimo-video-overview")
    if not args.consolidate:  # default is ON now; only the opt-out needs to round-trip
        parts.append("--no-consolidate")
    if args.consolidate_asr:
        parts.append("--consolidate-asr")
    if getattr(args, "mimo_tts_voice", None):
        parts += ["--mimo-tts-voice", args.mimo_tts_voice]
    if getattr(args, "voice_ref", None):
        parts += ["--voice-ref", args.voice_ref]
    if getattr(args, "allow_partial_tts", False):
        parts.append("--allow-partial-tts")
    if getattr(args, "burn_subtitles", None) is not None:
        parts.append("--burn-subtitles" if args.burn_subtitles else "--no-burn-subtitles")
    if getattr(args, "subtitle_y_top", None) is not None:
        parts += ["--subtitle-y-top", str(args.subtitle_y_top)]
    if getattr(args, "subtitle_y_bot", None) is not None:
        parts += ["--subtitle-y-bot", str(args.subtitle_y_bot)]
    if getattr(args, "output_dir", None):
        parts += ["--output-dir", args.output_dir]
    if getattr(args, "export_jianying", False):
        parts.append("--export-jianying")
    if getattr(args, "jianying_bundle_media", False):
        parts.append("--jianying-bundle-media")
    if getattr(args, "jianying_no_bundle_media", False):
        parts.append("--jianying-no-bundle-media")
    if getattr(args, "review_narration", None) is not None:
        parts.append("--review-narration" if args.review_narration else "--no-review-narration")
    if getattr(args, "require_narration_review", False):
        parts.append("--require-narration-review")
    if getattr(args, "material_library_dir", None):
        parts += ["--material-library-dir", args.material_library_dir]
    if getattr(args, "use_materials", False):
        parts.append("--use-materials")
    if getattr(args, "save_materials", False):
        parts.append("--save-materials")
    return " ".join(shlex.quote(part) for part in parts)


def _source_work_dir(project_work_dir, source_record):
    return Path(project_work_dir) / source_record["source_work_dir"]


def _understand_args_for_source(source_record, source_work_dir, args):
    uargs = [source_record["source_path"], "--work-dir", str(source_work_dir), "--style", args.style]
    if args.context:
        uargs += ["--context", args.context]
    if args.scene_threshold is not None:
        uargs += ["--scene-threshold", str(args.scene_threshold)]
    if args.edit_mode:
        uargs += ["--edit-mode", args.edit_mode]
    if args.target_duration:
        uargs += ["--target-duration", args.target_duration]
    if args.skip_asr:
        uargs.append("--skip-asr")
    if args.mimo_video_overview:
        uargs.append("--mimo-video-overview")
    uargs.append("--consolidate" if args.consolidate else "--no-consolidate")
    if args.consolidate_asr:
        uargs.append("--consolidate-asr")
    return uargs


def _brief_excerpt(path, limit=1200):
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return ""
    return text[:limit]


def _write_multi_source_clip_brief(work_dir, source_records, args):
    lines = [
        "# Multi-source Clip Plan Brief",
        "",
        "你正在做多视频剪辑复盘。当前 MVP 只支持 `--edit-mode cut`：先写 `clip_plan.json`，下一步会剪出 `edited_source.mp4`，再对 OUTPUT 时间轴写 `narration.json`。",
        "",
        "## 必须写入的格式",
        "",
        "```json",
        "{\"target_duration\":\"10m\",\"clips\":[{\"source_id\":\"src_xxx\",\"start\":12.0,\"end\":38.0,\"reason\":\"hook\"}]}",
        "```",
        "",
        "- 每个 clip 必须带 `source_id`。",
        "- `start`/`end` 是对应 source 原视频时间（秒）。",
        "- 不同 `source_id` 的相同时间段不算重叠；同一 `source_id` 内不要重复/重叠，除非你明确接受稀疏/重复剪辑风险。",
        "- 素材库是文件系统 JSON/MD/JSONL；需要找历史素材时直接 `grep -R \"关键词\" <material-library-dir>`。",
        "",
        "## Cut craft",
        *CUT_TIMELINE_CRAFT_BULLETS,
        "- For multi-source, choose clips across sources to serve the shared story spine; do not let one source become a disconnected mini-recap unless that is the intended setup/turn/payoff.",
        "- Use each source work_dir below (`sources/<source_id>`) to retrieve scenes.json, ASR, indexes, and per-source brief context before selecting clips.",
    ]
    if args.target_duration:
        lines.append(f"- 目标时长：`{args.target_duration}`。")
    lines += ["", "## Sources", ""]
    for s in source_records:
        swd = _source_work_dir(work_dir, s)
        lines += [
            f"### {s['source_id']} — {s['source_name']}",
            f"- path: `{s['source_path']}`",
            f"- work_dir: `{swd}`",
            f"- fingerprint: `{s['source_video_fingerprint']}`",
            f"- material_id: `{s.get('material_id')}`",
        ]
        excerpt = _brief_excerpt(swd / "agent_narration_brief.md")
        if excerpt:
            lines += ["", "#### per-source brief excerpt", "", excerpt, ""]
    (Path(work_dir) / "agent_narration_brief.md").write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _write_multi_source_output_brief(work_dir, source_records, validated_plan_path):
    plan = _load_json(validated_plan_path)
    clips = plan.get("clips", []) if isinstance(plan, dict) else []
    source_by_id = {s["source_id"]: s for s in source_records}
    lines = [
        "# Multi-source Output Narration Brief",
        "",
        "现在 `edited_source.mp4` 已经由多个源视频剪好。请对剪后成片的 OUTPUT 时间轴写 `narration.json`。",
        "",
        "## narration.json 格式",
        "",
        "```json",
        "[{\"start\":0.0,\"end\":4.0,\"narration\":\"...\"}]",
        "```",
        "",
        "注意：`start`/`end` 是剪后成片时间，不是原视频时间。",
        "",
        "## Output narration craft",
        *MULTI_SOURCE_NARRATION_CRAFT_BULLETS,
        "- Use BLOCK recap style: explain intent, stakes, subtext, relationships, and consequences; do not merely describe pixels.",
        "",
        "## Kept clips (output → source)",
    ]
    for c in clips:
        sid = c.get("source_id")
        src = source_by_id.get(sid, {})
        lines.append(
            f"- output {_fmt_range(c.get('output_start'), c.get('output_end'))} → "
            f"{sid} `{src.get('source_path', c.get('source_path', ''))}` "
            f"source {_fmt_range(c.get('source_start'), c.get('source_end'))} "
            f"{('— ' + str(c.get('reason'))) if c.get('reason') else ''}"
        )
    lines += ["", "## Source work dirs"]
    for s in source_records:
        lines.append(f"- {s['source_id']}: `{_source_work_dir(work_dir, s)}`")
    (Path(work_dir) / "agent_narration_brief.md").write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _qc_status_is_blocking(value):
    if isinstance(value, str):
        return value.strip().lower() in {"blocking", "blocked", "fail", "failed", "error"}
    return bool(value) if isinstance(value, bool) else False


def _cut_qc_blocking_reasons(qc):
    if not isinstance(qc, dict):
        return []
    reasons = []
    for key in ("status", "target_duration_status", "duration_status", "boundary_status"):
        if _qc_status_is_blocking(qc.get(key)):
            reasons.append(f"{key}={qc.get(key)}")
    for key in ("blocking", "blocked", "failed", "fail", "errors"):
        value = qc.get(key)
        if value:
            reasons.append(f"{key}={value}")
    checks = qc.get("checks") if isinstance(qc.get("checks"), list) else []
    for item in checks:
        if isinstance(item, dict) and _qc_status_is_blocking(item.get("status")):
            reasons.append(f"{item.get('name', 'check')}={item.get('status')}")
    return reasons


def _cut_qc_summary_lines(qc):
    if not isinstance(qc, dict) or not qc:
        return []
    parts = []
    for key in ("target_duration_status", "total_duration", "clip_count", "join_fade_ms"):
        if key in qc:
            parts.append(f"{key}={qc.get(key)}")
    geometry = qc.get("output_geometry")
    if isinstance(geometry, dict):
        dims = "x".join(str(geometry.get(k)) for k in ("width", "height") if geometry.get(k) is not None)
        fps = geometry.get("fps")
        reason = geometry.get("reason") or qc.get("output_geometry_reason")
        fps_suffix = f"@{fps}fps" if fps else ""
        reason_suffix = f" reason={reason}" if reason else ""
        parts.append(f"output_geometry={dims or geometry}{fps_suffix}{reason_suffix}")
    warnings = qc.get("warnings")
    if warnings:
        parts.append(f"warnings={len(warnings) if isinstance(warnings, list) else warnings}")
    return ["[video-recap] cut QC: " + "; ".join(parts)] if parts else []


def _surface_cut_qc(work_dir):
    plan = _load_json(Path(work_dir) / "clip_plan_validated.json")
    qc = plan.get("qc") if isinstance(plan, dict) else None
    for line in _cut_qc_summary_lines(qc):
        print(line, flush=True)
    blocking = _cut_qc_blocking_reasons(qc)
    if blocking:
        raise SystemExit("video-cut QC blocking/fail status: " + "; ".join(blocking))
    return qc


def _fmt_range(start, end):
    try:
        return f"{float(start):.3f}-{float(end):.3f}s"
    except (TypeError, ValueError):
        return f"{start}-{end}s"


def _material_library_dir(args):
    return args.material_library_dir or os.environ.get("VIDEO_RECAP_MATERIAL_LIBRARY_DIR") or None


def _materials_enabled(args):
    return bool(_material_library_dir(args) and args.use_materials)


def _save_materials_enabled(args):
    return bool(_material_library_dir(args) and args.save_materials)


def _pause_for_agent(work_dir, need_text, cont, inspect_hint=None):
    brief = Path(work_dir) / "agent_narration_brief.md"
    print("=" * 50)
    if brief.exists() and "Research the story FIRST" in brief.read_text(encoding="utf-8"):
        print("[video-recap] ⚑ 理解素材偏薄：先按 brief 顶部「Research the story FIRST」调研并写 "
              "background_research.json，再写解说，避免看图说话。")
    print(f"[video-recap] ⏸  阅读 {brief}（按 video-script 规则）后写入 {need_text}")
    if inspect_hint:
        print(f"[video-recap]    先核对状态/时间轴（建议性）: {inspect_hint}")
    print(f"[video-recap]    写完后重跑继续: {cont}")
    print("=" * 50)


def _run_or_restore_understanding(source_record, source_work_dir, args):
    """Run video-understanding for one source, or restore it from the material library."""
    source_work_dir = Path(source_work_dir)
    source_work_dir.mkdir(parents=True, exist_ok=True)
    source_fp = source_record["source_video_fingerprint"]
    settings_fp = source_record.get("settings_fingerprint") or _material_settings_fingerprint(args)
    lib_dir = _material_library_dir(args)
    restored = False
    if lib_dir and _materials_enabled(args):
        result = material_lib.restore_material(
            lib_dir,
            source_work_dir,
            source_fingerprint=source_fp,
            settings_fp=settings_fp,
        )
        restored = bool(result.get("restored"))
        if restored:
            print(f"[video-recap] ♻️  复用素材库: {result.get('material_id')} → {source_work_dir}", flush=True)
        elif result.get("reason"):
            print(f"[video-recap] 素材库未复用 {source_record['source_name']}: {result['reason']}", flush=True)

    if not restored:
        _run("video-understanding", "understand.py", *_understand_args_for_source(source_record, source_work_dir, args))

    _write_run_manifest(source_work_dir, source_record["source_path"], args)
    if lib_dir and _save_materials_enabled(args):
        meta = material_lib.save_material(
            lib_dir,
            source_work_dir,
            source_record["source_path"],
            source_fp,
            settings_fp,
            source_id=source_record.get("source_id"),
            material_id=source_record.get("material_id"),
        )
        source_record["material_id"] = meta.get("material_id")
        print(f"[video-recap] 💾 已沉淀素材: {meta.get('material_id')} → {lib_dir}", flush=True)
    return restored


def _rebuild_understanding_brief(source_record, source_work_dir, args):
    """Rebuild agent_narration_brief.md from cached/restored analysis only.

    Cut pass 2 needs an OUTPUT-time brief after edited_source.mp4 exists. A
    material restore may have supplied pass-1 analysis artifacts (and even a
    source-time brief), but it must not skip this phase-specific brief rebuild.
    """
    _run(
        "video-understanding",
        "understand.py",
        *_understand_args_for_source(source_record, source_work_dir, args),
        "--brief-only",
    )


def _reject_stale_multi_manifest(work_dir, videos, args, source_records):
    mismatches = _multi_manifest_mismatches(work_dir, videos, args, source_records)
    if mismatches:
        details = "\n  - ".join(mismatches)
        raise SystemExit(
            "work_dir 与当前多视频 recap 输入不匹配，拒绝复用既有 narration/clip_plan；"
            "请使用新的 --work-dir，或删除旧产物后重新运行 Phase A。\n"
            f"  - {details}")


def _run_multi_cut(videos, work_dir, args):
    """Multi-video MVP: cut-first/narrate-second over a project work_dir."""
    videos = _coerce_videos(videos)
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    source_records = _build_multi_source_records(videos, work_dir, args)
    narration_json = work_dir / "narration.json"
    clip_plan_json = work_dir / "clip_plan.json"
    edited_source = work_dir / "edited_source.mp4"
    inspect_py = _entry("video-recap", "recap_inspect.py")

    # If a project manifest already exists, it must match before any Phase-B reuse.
    if (work_dir / RUN_MANIFEST).exists():
        _reject_stale_multi_manifest(work_dir, videos, args, source_records)
    manifest_path = _write_multi_source_manifest(work_dir, source_records)

    if not clip_plan_json.exists():
        for record in source_records:
            _run_or_restore_understanding(record, _source_work_dir(work_dir, record), args)
        manifest_path = _write_multi_source_manifest(work_dir, source_records)
        _write_project_run_manifest(work_dir, videos, args, source_records)
        _write_multi_source_clip_brief(work_dir, source_records, args)
        _pause_for_agent(
            work_dir,
            f"{clip_plan_json}（多视频剪辑计划；每个 clip 必须带 source_id）",
            _continuation_command(videos, work_dir, args),
            inspect_hint=f"python3 {inspect_py} --work-dir {work_dir} state",
        )
        return

    _reject_stale_multi_manifest(work_dir, videos, args, source_records)
    cp_fp = _file_md5(clip_plan_json)
    crender = [str(videos[0]), "--work-dir", str(work_dir), "--sources-manifest", str(manifest_path), "--no-narration-map"]
    if args.target_duration:
        crender += ["--target-duration", args.target_duration]
    if getattr(args, "allow_duration_drift", False):
        crender.append("--allow-duration-drift")
    if getattr(args, "allow_sparse_cut", False):
        crender.append("--allow-sparse-cut")
    _run("video-cut", "cut.py", *crender)
    cut_qc = _surface_cut_qc(work_dir)
    _write_shift_left_stage_qc(work_dir, "post_cut", metadata={"cut_qc": cut_qc})
    if not narration_json.exists():
        _write_multi_source_output_brief(work_dir, source_records, work_dir / "clip_plan_validated.json")
        _write_phase_ledger(work_dir, clip_plan_fingerprint=cp_fp, edited_source_rendered=True, multi_source=True)
        _pause_for_agent(
            work_dir,
            f"{narration_json}（用成片 OUTPUT 时间轴写解说，对着 {edited_source}）",
            _continuation_command(videos, work_dir, args),
            inspect_hint=(f"python3 {inspect_py} --work-dir {work_dir} "
                          "clip-map --output-start <s> --output-end <e>"),
        )
        return
    if _cut_narration_is_stale(_read_phase_ledger(work_dir), cp_fp):
        raise SystemExit(
            "clip_plan.json 已改变，但 narration.json 仍是对旧剪辑写的，会与剪后画面对不上。"
            "请删除 narration.json，重跑后按新成片重新写解说。")
    _write_phase_ledger(work_dir, clip_plan_fingerprint=cp_fp,
                        narration_fingerprint=_file_md5(narration_json), narration_written=True, multi_source=True)
    output_duration = _read_video_duration_or_raise(edited_source)
    _run("video-script", "validate.py", "--work-dir", work_dir, "--mode", "cut_output",
         "--output-duration", f"{output_duration:.3f}")
    review_ran = _run_narration_review(work_dir, args, timeline="cut_output")
    _write_shift_left_stage_qc(work_dir, "pre_tts", metadata={"review_ran": review_ran, "timeline": "cut_output"})
    vargs = ["--work-dir", str(work_dir), "--narration", str(narration_json)]
    if args.mimo_tts_voice:
        vargs += ["--mimo-voice", args.mimo_tts_voice]
    if args.voice_ref:
        vargs += ["--voice-ref", args.voice_ref]
    if args.allow_partial_tts:
        vargs.append("--allow-partial-tts")
    _run("video-voiceover", "voiceover.py", *vargs)
    _write_shift_left_stage_qc(work_dir, "post_tts", metadata=_tts_qc_metadata(work_dir))
    overlays_path = _write_canonical_visual_overlays(work_dir, narration_json)
    _write_shift_left_stage_qc(work_dir, "pre_assemble", metadata={"visual_overlays": str(overlays_path)})

    recap_stem = f"multi_{videos[0].stem}"
    aargs = [str(edited_source), "--work-dir", str(work_dir), "--recap-stem", recap_stem]
    if args.output_dir:
        aargs += ["--output-dir", args.output_dir]
    if args.burn_subtitles is not None:
        aargs.append("--burn-subtitles" if args.burn_subtitles else "--no-burn-subtitles")
    if args.export_jianying:
        aargs.append("--export-jianying")
    if args.jianying_bundle_media:
        aargs.append("--jianying-bundle-media")
    if args.jianying_no_bundle_media:
        aargs.append("--jianying-no-bundle-media")
    _run("video-assemble", "assemble.py", *aargs)

    final_dir = Path(args.output_dir) if args.output_dir else work_dir.parent
    final_output = _read_assembly_output(work_dir) or (final_dir / ("recap_" + recap_stem + ".mp4"))
    _write_shift_left_stage_qc(work_dir, "post_render", metadata=_post_render_qc_metadata(work_dir, final_output))
    final_qc_result = _write_final_qc_reports(work_dir, final_output)
    print(f"[video-recap] ✅ 完成: {final_output}")
    _print_final_qc_pointer(final_qc_result)
    _print_narration_review_pointer(work_dir, review_ran=review_ran)


def main():
    ap = argparse.ArgumentParser(description="Full video recap orchestrator (video-* skill bundle).")
    ap.add_argument("video", nargs="*")
    ap.add_argument("--work-dir", default=None)
    ap.add_argument("--context", default="")
    ap.add_argument("--scene-threshold", type=float, default=None)
    ap.add_argument("--style", default="纪录片")
    ap.add_argument("--edit-mode", default=os.environ.get("EDIT_MODE", "full"), choices=["full", "cut", "dub"])
    ap.add_argument("--target-duration", default=os.environ.get("TARGET_DURATION") or None)
    ap.add_argument("--allow-duration-drift", action="store_true",
                    help="cut mode: accept clip duration drift from --target-duration (primary override)")
    ap.add_argument("--allow-sparse-cut", action="store_true",
                    help="compatibility: accept sparse cut mapping and legacy duration drift override")
    ap.add_argument("--skip-asr", action="store_true")
    ap.add_argument("--mimo-video-overview", action="store_true")
    ap.add_argument("--consolidate", action=argparse.BooleanOptionalAction, default=True,
                    help="build the understanding story index (Pass B); default ON, --no-consolidate to skip")
    ap.add_argument("--consolidate-asr", action="store_true", help="also clean ASR (Pass A)")
    ap.add_argument("--mimo-tts-voice", default=None, help="MiMo TTS voice")
    ap.add_argument("--voice-ref", default=None,
                    help="reference audio for cloned narration voice (mimo-v2.5-tts-voiceclone)")
    ap.add_argument("--allow-partial-tts", action="store_true",
                    help="allow video-voiceover to continue when some narration segments fail TTS")
    ap.add_argument("--burn-subtitles", action=argparse.BooleanOptionalAction, default=None,
                    help="burn narration subtitles into the video (default on; --no-burn-subtitles to disable)")
    ap.add_argument("--subtitle-y-top", type=int, default=None,
                    help="auto-rotated display-frame Y at the top of the measured subtitle band")
    ap.add_argument("--subtitle-y-bot", type=int, default=None,
                    help="auto-rotated display-frame Y at the bottom of the measured subtitle band")
    ap.add_argument("--review-narration", action=argparse.BooleanOptionalAction, default=None,
                    help="run advisory narration quality review before TTS (default on; fail-open)")
    ap.add_argument("--require-narration-review", action="store_true",
                    help="make narration review a strict pre-TTS gate (also REQUIRE_NARRATION_REVIEW=1)")
    ap.add_argument("--output-dir", default=None)
    ap.add_argument("--export-jianying", action="store_true",
                    help="also export an OPTIONAL 剪映/JianYing draft (decoupled; never required)")
    ap.add_argument("--jianying-bundle-media", action="store_true",
                    help="copy media into the 剪映 draft (default on; portable to another machine)")
    ap.add_argument("--jianying-no-bundle-media", action="store_true",
                    help="reference media in place instead of copying it into the draft")
    ap.add_argument("--material-library-dir", default=None,
                    help="filesystem material library dir (or VIDEO_RECAP_MATERIAL_LIBRARY_DIR)")
    ap.add_argument("--use-materials", action=argparse.BooleanOptionalAction, default=False,
                    help="restore compatible analyzed artifacts from the material library")
    ap.add_argument("--save-materials", action="store_true",
                    help="save analyzed JSON/MD artifacts into the material library")
    ap.add_argument("--doctor", action="store_true")
    args = ap.parse_args()

    if args.doctor:
        _run("video-recap", "doctor.py")
        return
    if not args.video:
        ap.error("video is required (unless --doctor)")

    if args.voice_ref is None:
        args.voice_ref = os.environ.get("VOICE_REF", "").strip() or None
    try:
        if args.subtitle_y_top is None:
            args.subtitle_y_top = _optional_env_int("SUBTITLE_Y_TOP")
        if args.subtitle_y_bot is None:
            args.subtitle_y_bot = _optional_env_int("SUBTITLE_Y_BOT")
    except ValueError as exc:
        ap.error(str(exc))

    explicit_mimo_voice = args.mimo_tts_voice or os.environ.get("MIMO_TTS_VOICE", "").strip()
    if explicit_mimo_voice and args.voice_ref:
        ap.error("--mimo-tts-voice and --voice-ref are mutually exclusive")
    if args.edit_mode == "dub" and args.voice_ref:
        ap.error("--voice-ref is only supported in full/cut modes; dub clones the source voice automatically")
    if args.edit_mode == "dub" and args.subtitle_y_top is not None:
        ap.error("--subtitle-y-top/--subtitle-y-bot are only supported in full/cut modes")
    if (args.subtitle_y_top is None) != (args.subtitle_y_bot is None):
        ap.error("--subtitle-y-top and --subtitle-y-bot must be provided together")
    if args.subtitle_y_top is not None:
        if args.subtitle_y_top < 0 or args.subtitle_y_bot <= args.subtitle_y_top:
            ap.error("subtitle Y coordinates must satisfy 0 <= top < bot")

    videos = _coerce_videos(args.video)
    if len(videos) > 1 and args.edit_mode != "cut":
        raise SystemExit("多视频输入当前 MVP 只支持 --edit-mode cut；full/dub 请一次输入一个视频。")
    if len(videos) > 1 and args.subtitle_y_top is not None:
        ap.error("多视频 cut 暂不支持全局 subtitle Y 坐标；各源字幕带可能不同")
    if args.subtitle_y_top is not None:
        canvas_height = _probe_display_height_or_raise(videos[0], require_square_pixels=True)
        if args.subtitle_y_bot > canvas_height:
            ap.error(
                f"subtitle Y coordinates exceed display canvas height {canvas_height}: "
                f"bot={args.subtitle_y_bot}"
            )
        os.environ["SUBTITLE_Y_TOP"] = str(args.subtitle_y_top)
        os.environ["SUBTITLE_Y_BOT"] = str(args.subtitle_y_bot)
        # Supplying a measured source-subtitle band is itself an explicit opt-in to mask
        # that band. This keeps the visual safety policy explicit without extra CLI ceremony.
        os.environ["MASK_SOURCE_SUBTITLES"] = "1"
        os.environ["SOURCE_SUBTITLE_MASK_POLICY"] = "opt_in"
    if args.voice_ref:
        voice_ref = Path(args.voice_ref).expanduser().resolve()
        if not voice_ref.is_file():
            ap.error(f"reference audio does not exist or is not a file: {voice_ref}")
        args.voice_ref = str(voice_ref)

    # Fail fast before any expensive understanding/VLM/ASR/TTS work if the run will burn
    # subtitles but this ffmpeg can't (otherwise it only blows up at the final render).
    _preflight_burn_subtitles(args)

    if len(videos) > 1:
        work_dir = (
            Path(args.work_dir).resolve()
            if args.work_dir
            else videos[0].parent / f"work_dir_multi_{videos[0].stem}"
        )
        _run_multi_cut(videos, work_dir, args)
        return

    video = videos[0]
    work_dir = Path(args.work_dir).resolve() if args.work_dir else video.parent / f"work_dir_{video.stem}"
    work_dir.mkdir(parents=True, exist_ok=True)
    cut = args.edit_mode == "cut"
    narration_json = work_dir / "narration.json"
    clip_plan_json = work_dir / "clip_plan.json"

    edited_source = work_dir / "edited_source.mp4"

    def _understand():
        fp = _file_fingerprint(video)
        source_record = {
            "source_id": material_lib.source_id_from_fingerprint(fp),
            "source_path": str(video),
            "source_name": video.name,
            "source_video_fingerprint": fp,
            "settings_fingerprint": _material_settings_fingerprint(args),
            "material_id": material_lib.material_id_for(video, fp),
        }
        _run_or_restore_understanding(source_record, work_dir, args)
        return source_record

    def _rebuild_output_brief():
        fp = _file_fingerprint(video)
        source_record = {
            "source_id": material_lib.source_id_from_fingerprint(fp),
            "source_path": str(video),
            "source_name": video.name,
            "source_video_fingerprint": fp,
            "settings_fingerprint": _material_settings_fingerprint(args),
            "material_id": material_lib.material_id_for(video, fp),
        }
        _rebuild_understanding_brief(source_record, work_dir, args)

    inspect_py = _entry("video-recap", "recap_inspect.py")

    def _pause(need_text, inspect_hint=None):
        brief = work_dir / "agent_narration_brief.md"
        cont = _continuation_command(video, work_dir, args)
        print("=" * 50)
        # The brief fires a research directive only when the substrate is thin/empty and no
        # background_research.json exists yet; amplify it so the agent researches BEFORE writing.
        if brief.exists() and "Research the story FIRST" in brief.read_text(encoding="utf-8"):
            print("[video-recap] ⚑ 理解素材偏薄：先按 brief 顶部「Research the story FIRST」调研并写 "
                  "background_research.json，再写解说，避免看图说话。")
        print(f"[video-recap] ⏸  阅读 {brief}（按 video-script 规则）后写入 {need_text}")
        if inspect_hint:
            print(f"[video-recap]    先核对状态/时间轴（建议性）: {inspect_hint}")
        print(f"[video-recap]    写完后重跑继续: {cont}")
        print("=" * 50)

    def _reject_stale_manifest():
        mismatches = _manifest_mismatches(work_dir, video, args)
        if mismatches:
            details = "\n  - ".join(mismatches)
            raise SystemExit(
                "work_dir 与当前 recap 输入不匹配，拒绝复用既有 narration/clip_plan；"
                "请使用新的 --work-dir，或删除旧产物后重新运行 Phase A。\n"
                f"  - {details}")

    if args.edit_mode == "dub":
        # Dub mode: EN→ZH translation-dub in the original cloned voice (replaces speech, not
        # overlay). One pause: prepare (ASR + sentence-seg + reference) -> agent writes the
        # Chinese translation (dub_script.json) -> render (clone TTS + full-replace mux).
        dub_script = work_dir / "dub_script.json"
        if not dub_script.exists():
            _run("video-voiceover", "dub.py", "--stage", "prepare",
                 "--video", str(video), "--work-dir", str(work_dir))
            _write_run_manifest(work_dir, video, args)
            cont = _continuation_command(video, work_dir, args)
            print("=" * 50)
            print(f"[video-recap] ⏸  阅读 {work_dir / 'dub_brief.md'}，把英文原声转写切分并翻译成中文，写入 {dub_script}")
            print('[video-recap]    格式 [{"start": 起秒, "end": 止秒, "zh": "译文"}]（按 start 升序）；逐句忠实、跟原声节奏一致、保留原音色')
            print(f"[video-recap]    写完后重跑继续: {cont}")
            print("=" * 50)
            return
        _reject_stale_manifest()
        _run("video-voiceover", "dub.py", "--stage", "render",
             "--video", str(video), "--work-dir", str(work_dir))
        print(f"[video-recap] ✅ 配音完成: {work_dir / ('dub_' + video.stem + '.mp4')}")
        return

    if not cut:
        # Full mode: a single pause (understand -> agent writes narration.json -> produce).
        if not narration_json.exists():
            _understand()
            _write_run_manifest(work_dir, video, args)
            _pause(f"{narration_json}",
                   inspect_hint=f"python3 {inspect_py} --work-dir {work_dir} state")
            return
        _reject_stale_manifest()
        _run("video-script", "validate.py", "--work-dir", work_dir, "--mode", "full")
        narration_for_tts = narration_json
        assemble_video_path = video
    else:
        # Cut mode: cut-first / narrate-second (two pauses), so narration is authored against the
        # REAL output timeline — map_narration_to_clips is never used and cannot drop/clamp/desync.
        if not clip_plan_json.exists():
            # PASS 1: understand -> agent writes clip_plan.json ONLY.
            _understand()
            _write_run_manifest(work_dir, video, args)
            _pause(f"{clip_plan_json}（只写剪辑计划；解说下一步对着剪好的成片写）",
                   inspect_hint=f"python3 {inspect_py} --work-dir {work_dir} state")
            return
        _reject_stale_manifest()
        cp_fp = _file_md5(clip_plan_json)
        # Render the cut from clip_plan (no narration mapping — narration is OUTPUT-time).
        crender = [str(video), "--work-dir", str(work_dir), "--no-narration-map"]
        if args.target_duration:
            crender += ["--target-duration", args.target_duration]
        if getattr(args, "allow_duration_drift", False):
            crender.append("--allow-duration-drift")
        if getattr(args, "allow_sparse_cut", False):
            crender.append("--allow-sparse-cut")
        _run("video-cut", "cut.py", *crender)
        cut_qc = _surface_cut_qc(work_dir)
        _write_shift_left_stage_qc(work_dir, "post_cut", metadata={"cut_qc": cut_qc})
        if not narration_json.exists():
            # PASS 2: rebuild the brief (now an OUTPUT-timeline variant) and pause for narration.
            _rebuild_output_brief()
            _write_phase_ledger(work_dir, clip_plan_fingerprint=cp_fp, edited_source_rendered=True)
            _pause(f"{narration_json}（用成片 OUTPUT 时间轴写解说，对着 {edited_source}）",
                   inspect_hint=(f"python3 {inspect_py} --work-dir {work_dir} "
                                 "clip-map --output-start <s> --output-end <e>  # 核对输出↔原片时间轴"))
            return
        if _cut_narration_is_stale(_read_phase_ledger(work_dir), cp_fp):
            raise SystemExit(
                "clip_plan.json 已改变，但 narration.json 仍是对旧剪辑写的，会与剪后画面对不上。"
                "请删除 narration.json，重跑后按新成片重新写解说。")
        _write_phase_ledger(work_dir, clip_plan_fingerprint=cp_fp,
                            narration_fingerprint=_file_md5(narration_json), narration_written=True)
        output_duration = _read_video_duration_or_raise(edited_source)
        _run("video-script", "validate.py", "--work-dir", work_dir, "--mode", "cut_output",
             "--output-duration", f"{output_duration:.3f}")
        narration_for_tts = narration_json
        assemble_video_path = edited_source
    review_ran = _run_narration_review(work_dir, args, timeline="cut_output" if cut else "source")
    _write_shift_left_stage_qc(
        work_dir,
        "pre_tts",
        metadata={"review_ran": review_ran, "timeline": "cut_output" if cut else "source"},
    )
    vargs = ["--work-dir", str(work_dir), "--narration", str(narration_for_tts)]
    if args.mimo_tts_voice:
        vargs += ["--mimo-voice", args.mimo_tts_voice]
    if args.voice_ref:
        vargs += ["--voice-ref", args.voice_ref]
    if args.allow_partial_tts:
        vargs.append("--allow-partial-tts")
    _run("video-voiceover", "voiceover.py", *vargs)
    _write_shift_left_stage_qc(work_dir, "post_tts", metadata=_tts_qc_metadata(work_dir))
    overlays_path = _write_canonical_visual_overlays(work_dir, narration_for_tts)
    _write_shift_left_stage_qc(work_dir, "pre_assemble", metadata={"visual_overlays": str(overlays_path)})

    aargs = [str(assemble_video_path), "--work-dir", str(work_dir), "--recap-stem", video.stem]
    if args.output_dir:
        aargs += ["--output-dir", args.output_dir]
    if args.burn_subtitles is not None:
        aargs.append("--burn-subtitles" if args.burn_subtitles else "--no-burn-subtitles")
    # env-only burn intent (BURN_SUBTITLES) is propagated implicitly: assemble re-derives it
    # via the same env_bool default the preflight used, so the two agree by shared env.
    if cut:
        # let the timeline / 剪映 export reference the original clips, not edited_source.mp4
        aargs += ["--source-video", str(video)]
    if args.export_jianying:  # env EXPORT_JIANYING is honored by assemble.py itself
        aargs.append("--export-jianying")
    if args.jianying_bundle_media:
        aargs.append("--jianying-bundle-media")
    if args.jianying_no_bundle_media:
        aargs.append("--jianying-no-bundle-media")
    _run("video-assemble", "assemble.py", *aargs)

    final_dir = Path(args.output_dir) if args.output_dir else work_dir.parent
    final_output = _read_assembly_output(work_dir) or (final_dir / ("recap_" + video.stem + ".mp4"))
    _write_shift_left_stage_qc(work_dir, "post_render", metadata=_post_render_qc_metadata(work_dir, final_output))
    final_qc_result = _write_final_qc_reports(work_dir, final_output)
    print(f"[video-recap] ✅ 完成: {final_output}")
    _print_final_qc_pointer(final_qc_result)
    _print_narration_review_pointer(work_dir, review_ran=review_ran)


if __name__ == "__main__":
    main()
