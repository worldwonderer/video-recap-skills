"""Provide subprocess, manifest, review, and preflight runtime helpers."""

import hashlib

import json

import math

import os

import shlex

import subprocess

import sys

from pathlib import Path


from doctor import ffmpeg_has_subtitles_filter

import materials as material_lib

BUNDLE = Path(__file__).resolve().parents[2]  # the skills/ directory

RUN_MANIFEST = "recap_run_manifest.json"

MULTI_SOURCE_MANIFEST = "multi_source_manifest.json"


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
        "ffprobe",
        "-v",
        "quiet",
        "-show_entries",
        "format=duration",
        "-of",
        "csv=p=0",
        str(path),
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
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height,sample_aspect_ratio:stream_tags=rotate:stream_side_data=rotation",
        "-of",
        "json",
        str(path),
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
    if require_square_pixels and (
        not math.isfinite(sar_ratio) or abs(sar_ratio - 1.0) >= 1e-9
    ):
        raise SystemExit(
            f"subtitle Y coordinates currently require square-pixel video (SAR 1:1); got {sar}"
        )
    rotation_values = [
        (stream.get("tags") or {}).get("rotate"),
        *(
            item.get("rotation")
            for item in stream.get("side_data_list") or []
            if isinstance(item, dict)
        ),
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
    (work_dir / RUN_MANIFEST).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _build_multi_source_records(videos, args):
    records = []
    for video in _coerce_videos(videos):
        fp = _file_fingerprint(video)
        records.append(
            {
                "source_path": str(video),
                "source_name": video.name,
                "source_video_fingerprint": fp,
                "settings_fingerprint": _material_settings_fingerprint(args),
                "material_id": material_lib.material_id_for(video, fp),
            }
        )
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
    (work_dir / RUN_MANIFEST).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


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
            f"  自检：python3 {shlex.quote(str(_entry('video-recap', 'doctor.py')))}"
        )


def _entry(skill, script):
    return BUNDLE / skill / "scripts" / script
