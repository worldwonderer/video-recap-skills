"""Render edited source media and write delivery QC."""

import json


from pathlib import Path

from lib import CONFIG, get_video_duration, log, run_cmd

from cut_contract import _write_edited_source_meta
from media_geometry import _has_audio_stream, _select_output_geometry
from sentence_boundaries import _continuous_source_join


def _audio_segment_filter(
    label_in,
    label_out,
    start,
    end,
    duration,
    fade_ms,
    extra_filters="",
    fade_in_ms=None,
    fade_out_ms=None,
):
    max_fade = max(0.0, float(duration or 0.0)) / 2
    fade_in = max(
        0.0,
        min(float(fade_ms if fade_in_ms is None else fade_in_ms) / 1000.0, max_fade),
    )
    fade_out = max(
        0.0,
        min(float(fade_ms if fade_out_ms is None else fade_out_ms) / 1000.0, max_fade),
    )
    base = f"{label_in}atrim=start={start:.3f}:end={end:.3f},asetpts=PTS-STARTPTS"
    if fade_in > 0:
        base += f",afade=t=in:st=0:d={fade_in:.3f}"
    if fade_out > 0:
        base += f",afade=t=out:st={max(0.0, duration - fade_out):.3f}:d={fade_out:.3f}"
    if extra_filters:
        base += f",{extra_filters}"
    return f"{base}{label_out}"


def _clip_audio_edge_fades(clips, idx, fade_ms):
    """Do not create an audible dip where adjacent clips are a lossless source continuation."""
    fade_in = (
        0.0
        if idx > 0 and _continuous_source_join(clips[idx - 1], clips[idx])
        else fade_ms
    )
    fade_out = (
        0.0
        if idx + 1 < len(clips) and _continuous_source_join(clips[idx], clips[idx + 1])
        else fade_ms
    )
    return fade_in, fade_out


def _write_filter_script(filter_complex, work_dir):
    script_path = Path(work_dir) / "edit_filter_complex.txt"
    script_path.write_text(filter_complex, encoding="utf-8")
    return script_path


def _probe_audio_sample_rate(video_path):
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=sample_rate",
        "-of",
        "csv=p=0",
        str(video_path),
    ]
    try:
        result = run_cmd(cmd)
    except Exception:  # noqa: BLE001 - delivery QC is observational
        return None
    if result.returncode != 0:
        return None
    try:
        return int(float((result.stdout or "").strip().splitlines()[0]))
    except (IndexError, TypeError, ValueError):
        return None


def _delivery_reencode_reason(source_paths, clips):
    reasons = ["trim_concat_filter_requires_reencode"]
    if len(source_paths or []) > 1:
        reasons.append("multi_source_geometry_audio_normalization")
    if any(not clip.get("source_path") for clip in clips or []):
        reasons.append("single_source_filter_concat_no_stream_copy")
    return "+".join(reasons)


def update_delivery_qc(
    validated_plan, *, source_paths=None, output_path=None, rendered=False
):
    """Attach cut delivery facts to qc.delivery_qc without writing visual_qc."""
    qc = validated_plan.setdefault("qc", {})
    clips = validated_plan.get("clips") or []
    if source_paths is None:
        source_paths = sorted(
            {
                str(clip.get("source_path") or "")
                for clip in clips
                if str(clip.get("source_path") or "")
            }
        )
    target_sample_rate = 48000
    probed_sample_rate = (
        _probe_audio_sample_rate(output_path)
        if output_path and Path(output_path).exists()
        else None
    )
    output_geometry = qc.get("output_geometry")
    delivery_qc = {
        "schema_version": 1,
        "video_encode_passes": 1,
        "reencode_reason": _delivery_reencode_reason(source_paths, clips),
        "stream_copy_risk": {
            "status": "avoided",
            "reason": "cut uses trim/concat/filtergraph with explicit libx264/aac encode; no risky stream-copy path",
        },
        "audio_sample_rate": {
            "target": target_sample_rate,
            "probed": probed_sample_rate,
        },
        "final_compat_notes": [
            "video encoded with libx264/yuv420p-compatible filter path",
            "audio encoded as AAC with 48000 Hz target for delivery compatibility",
            "edited_source.mp4 is an intermediate; downstream assembly may perform another intentional encode",
        ],
        "output_geometry": output_geometry,
        "rendered": bool(rendered),
        "planned": not bool(rendered),
    }
    if probed_sample_rate and probed_sample_rate != target_sample_rate:
        delivery_qc["final_compat_notes"].append(
            f"probed audio sample rate {probed_sample_rate} differs from target {target_sample_rate}"
        )
    qc["delivery_qc"] = delivery_qc
    return delivery_qc


def write_cut_delivery_qc(work_dir, validated_plan):
    delivery_qc = (validated_plan.get("qc") or {}).get("delivery_qc")
    if not delivery_qc or not delivery_qc.get("rendered"):
        return None
    path = Path(work_dir) / "cut_delivery_qc.json"
    path.write_text(
        json.dumps(delivery_qc, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


def build_edited_source_video(input_video, validated_plan, work_dir, output_path=None):
    """Build `edited_source.mp4` by concatenating validated source ranges."""
    work_dir = Path(work_dir)
    output_path = Path(output_path or work_dir / "edited_source.mp4")
    clips = validated_plan["clips"]
    if not clips:
        raise ValueError("validated clip plan has no clips")

    source_paths = []
    for clip in clips:
        source_path = str(clip.get("source_path") or input_video)
        if source_path not in source_paths:
            source_paths.append(source_path)
    source_index = {path: idx for idx, path in enumerate(source_paths)}
    audio_by_input = {path: _has_audio_stream(path) for path in source_paths}
    join_fade_ms = max(0.0, float(CONFIG.get("clip_join_audio_fade_ms", 30.0) or 0.0))
    qc = validated_plan.setdefault("qc", {})
    qc["join_fade_ms"] = round(join_fade_ms, 3)
    if not qc.get("output_geometry"):
        _, _, _, geometry_qc = _select_output_geometry(source_paths, clips)
        qc["output_geometry"] = geometry_qc
        qc["output_geometry_reason"] = geometry_qc.get("reason")

    parts = []
    concat_inputs = []
    extra_inputs = []
    if len(source_paths) > 1:
        # Distinct sources almost always differ in resolution/SAR/fps/pixel-format (and
        # some may lack audio), which the bare concat filter rejects. Normalize every video
        # segment to one canvas and give every clip an audio segment (real or synthesized
        # silence) so concat always succeeds with a continuous track and no source's audio
        # is dropped just because a sibling source is silent.
        # Reuse the geometry already probed+stored above (guard) or by main() — the
        # selection is deterministic, so re-probing every source here is wasted ffprobe work.
        geometry_qc = qc.get("output_geometry")
        if isinstance(geometry_qc, dict) and all(
            geometry_qc.get(k) for k in ("width", "height", "fps")
        ):
            canvas_w, canvas_h, canvas_fps = (
                int(geometry_qc["width"]),
                int(geometry_qc["height"]),
                geometry_qc["fps"],
            )
        else:
            canvas_w, canvas_h, canvas_fps, geometry_qc = _select_output_geometry(
                source_paths, clips
            )
            qc["output_geometry"] = geometry_qc
            qc["output_geometry_reason"] = geometry_qc.get("reason")
        vnorm = (
            f"scale={canvas_w}:{canvas_h}:force_original_aspect_ratio=decrease,"
            f"pad={canvas_w}:{canvas_h}:(ow-iw)/2:(oh-ih)/2,setsar=1,"
            f"fps={canvas_fps},format=yuv420p"
        )
        for clip_pos, clip in enumerate(clips):
            idx = clip["clip_id"]
            clip_source = str(clip.get("source_path") or input_video)
            input_idx = source_index[clip_source]
            start = clip["source_start"]
            end = clip["source_end"]
            dur = max(0.0, float(end) - float(start))
            fade_in_ms, fade_out_ms = _clip_audio_edge_fades(
                clips, clip_pos, join_fade_ms
            )
            parts.append(
                f"[{input_idx}:v]trim=start={start:.3f}:end={end:.3f},setpts=PTS-STARTPTS,{vnorm}[v{idx}]"
            )
            if audio_by_input.get(clip_source):
                parts.append(
                    _audio_segment_filter(
                        f"[{input_idx}:a]",
                        f"[a{idx}]",
                        start,
                        end,
                        dur,
                        join_fade_ms,
                        extra_filters="aresample=48000,aformat=sample_rates=48000:channel_layouts=stereo",
                        fade_in_ms=fade_in_ms,
                        fade_out_ms=fade_out_ms,
                    )
                )
            else:
                parts.append(
                    f"anullsrc=r=48000:cl=stereo,atrim=duration={dur:.3f},asetpts=PTS-STARTPTS,"
                    f"aformat=sample_rates=48000:channel_layouts=stereo[a{idx}]"
                )
            concat_inputs.append(f"[v{idx}][a{idx}]")
        parts.append("".join(concat_inputs) + f"concat=n={len(clips)}:v=1:a=1[v][a]")
        maps = ["-map", "[v]", "-map", "[a]"]
    else:
        has_audio = all(audio_by_input.values())
        for clip_pos, clip in enumerate(clips):
            idx = clip["clip_id"]
            input_idx = source_index[str(clip.get("source_path") or input_video)]
            start = clip["source_start"]
            end = clip["source_end"]
            parts.append(
                f"[{input_idx}:v]trim=start={start:.3f}:end={end:.3f},setpts=PTS-STARTPTS[v{idx}]"
            )
            concat_inputs.append(f"[v{idx}]")
            if has_audio:
                fade_in_ms, fade_out_ms = _clip_audio_edge_fades(
                    clips, clip_pos, join_fade_ms
                )
                parts.append(
                    _audio_segment_filter(
                        f"[{input_idx}:a]",
                        f"[a{idx}]",
                        start,
                        end,
                        max(0.0, float(end) - float(start)),
                        join_fade_ms,
                        fade_in_ms=fade_in_ms,
                        fade_out_ms=fade_out_ms,
                    )
                )
                concat_inputs.append(f"[a{idx}]")

        if has_audio:
            parts.append(
                "".join(concat_inputs) + f"concat=n={len(clips)}:v=1:a=1[v][a]"
            )
            maps = ["-map", "[v]", "-map", "[a]"]
        else:
            total = validated_plan.get("total_duration") or sum(
                c["duration"] for c in clips
            )
            parts.append("".join(concat_inputs) + f"concat=n={len(clips)}:v=1:a=0[v]")
            maps = ["-map", "[v]", "-map", f"{len(source_paths)}:a", "-shortest"]
            extra_inputs = [
                "-f",
                "lavfi",
                "-t",
                f"{float(total):.3f}",
                "-i",
                "anullsrc=channel_layout=stereo:sample_rate=48000",
            ]

    filter_complex = ";".join(parts)
    if len(filter_complex.encode("utf-8")) > 7000:
        filter_script = _write_filter_script(filter_complex, work_dir)
        filter_args = ["-filter_complex_script", str(filter_script)]
    else:
        filter_args = ["-filter_complex", filter_complex]

    input_args = []
    for source_path in source_paths:
        input_args.extend(["-i", str(source_path)])
    cmd = [
        "ffmpeg",
        "-y",
        *input_args,
        *extra_inputs,
        *filter_args,
        *maps,
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-ar",
        "48000",
        "-movflags",
        "+faststart",
        str(output_path),
    ]
    result = run_cmd(cmd)
    if result.returncode != 0:
        raise RuntimeError(f"剪辑源视频失败: {result.stderr}")

    update_delivery_qc(
        validated_plan,
        source_paths=source_paths,
        output_path=output_path,
        rendered=True,
    )
    write_cut_delivery_qc(work_dir, validated_plan)
    _write_edited_source_meta(output_path, validated_plan, input_video)
    duration = get_video_duration(output_path)
    log(f"剪辑源视频: {output_path} ({duration:.1f}s, {len(clips)} clips)")
    return output_path
