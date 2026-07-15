"""Canonical CLI and programmatic render entry for the self-contained video-assemble skill."""

import json
import os
from pathlib import Path

import artifacts
import assemble_constants as constants
import assembly_contract
import assembly_settings
import audio_mix
import media
import narration_audio
import render_preflight
import subtitle_render
import timeline_emit
import visual_render
import lib

__all__ = ["assemble_video", "main"]


def assemble_video(input_video, tts_segments, work_dir, output_path):
    """组装最终视频"""
    if not tts_segments:
        raise RuntimeError("tts_meta.json 没有有效解说音频，已中止以避免生成无解说视频")

    video_duration = lib.get_video_duration(input_video)
    canvas = media._probe_canvas(input_video)  # drives subtitle PlayRes/scale so 竖屏 text isn't stretched

    # 解说整体提速（可选）后，将所有 TTS 片段按时间位置合成到与视频等长的音轨上
    narration_audio._apply_narration_speed(tts_segments, work_dir)
    narration_wav = work_dir / "narration.wav"
    narration_audio._build_timed_narration(tts_segments, narration_wav, video_duration, work_dir)
    handoffs = audio_mix._apply_source_sentence_handoffs(tts_segments, work_dir, video_duration)
    if handoffs:
        lib.log(
            "原声句末交接: "
            + ", ".join(
                f"{item['end']:.2f}s→{item.get('restore_at', item['end']):.2f}s({item['status']})"
                for item in handoffs
            )
        )

    # 始终生成 SRT 字幕文件（原声留白处补烧原声字幕，传入成片时长以计算留白区间）
    srt_path = subtitle_render._generate_srt(tts_segments, work_dir, video_duration)
    lib.log(f"字幕文件: {srt_path}")
    ass_path = None
    if lib.CONFIG.get("burn_subtitles", False):
        ass_path = subtitle_render._generate_ass(tts_segments, work_dir, video_duration, canvas)
        lib.log(f"压制字幕文件: {ass_path}")

    # 可选 BGM：作为一条独立音轨（input [2:a]）混入，旁白处自动压低
    bgm_path = lib.CONFIG.get("bgm_path", "")
    has_bgm = bool(bgm_path) and os.path.exists(bgm_path)
    if bgm_path and not has_bgm:
        lib.log(f"  ⚠️ BGM 文件不存在，跳过: {bgm_path}")
    elif has_bgm:
        lib.log(f"BGM 铺底: {bgm_path} (音量 {lib.CONFIG.get('bgm_volume', 0.18)}，旁白时 {lib.CONFIG.get('bgm_ducking_volume', 0.10)})")

    # 多轨时间线模型（timeline.json）：canonical 渲染仍是 ffmpeg，此模型供检视/可选导出
    timeline_emit._emit_timeline(input_video, tts_segments, work_dir, video_duration, has_bgm)

    overlay_filters, overlay_qc = visual_render._visual_overlay_filters(work_dir, canvas, video_duration)
    mask_filter = visual_render._source_subtitle_mask_filter(
        canvas, work_dir, tts_segments, video_duration=video_duration
    )
    visual_qc = visual_render._build_visual_qc(
        tts_segments,
        work_dir,
        video_duration,
        canvas,
        overlay_qc=overlay_qc,
        mask_filter=mask_filter,
    )
    visual_render._write_visual_qc(work_dir, visual_qc)
    assembly_qc_path = Path(work_dir) / constants.ASSEMBLY_QC
    assembly_qc_path.unlink(missing_ok=True)
    if visual_qc.get("blocking"):
        codes = ", ".join(visual_qc.get("blocking_codes", []))
        raise RuntimeError(f"视觉 QC 失败: {codes}；详见 {Path(work_dir) / constants.VISUAL_QC}")

    # 混合原始音频 + 解说音频（+ 可选 BGM）
    source_has_audio = media._has_audio_stream(input_video)
    if source_has_audio:
        original_audio_input = []
        original_audio_label = "0:a"
        bgm_audio_label = "2:a"
    else:
        lib.log("源视频无音轨，使用静音原声音轨进行混音")
        original_audio_input = [
            "-f", "lavfi", "-t", str(video_duration),
            "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
        ]
        original_audio_label = "2:a"
        bgm_audio_label = "3:a"
    filter_complex = audio_mix._build_audio_filter_complex(
        tts_segments,
        has_bgm,
        original_audio_label=original_audio_label,
        bgm_audio_label=bgm_audio_label,
    )

    # BGM is input [2:a]; -stream_loop -1 loops it to cover the whole timeline (amix
    # duration=first + -t trim it back to the video length).
    bgm_input = ["-stream_loop", "-1", "-i", str(bgm_path)] if has_bgm else []

    # 对于超长 volume 表达式（多段解说），使用 -filter_complex_script 避免命令行溢出
    # 末端整体响度归一：ducking 只管相对平衡，这一步统一成片绝对响度
    aout_label = "[aout]"
    loudnorm_measurement = audio_mix._run_loudnorm_first_pass(
        input_video,
        narration_wav,
        original_audio_input,
        bgm_input,
        filter_complex,
        work_dir,
    )
    final_ln = audio_mix.final_loudnorm_filter(loudnorm_measurement)
    if final_ln:
        filter_complex += f";[aout]{final_ln}[aoutln]"
        aout_label = "[aoutln]"
        lib.log(f"成片响度归一: {final_ln}")

    filter_complex_bytes = filter_complex.encode('utf-8')
    if len(filter_complex_bytes) > constants.FILTER_SCRIPT_THRESHOLD_BYTES:
        fc_script = Path(work_dir) / ".filter_complex.txt"
        fc_script.write_text(filter_complex, encoding="utf-8")
        lib.log(f"使用 filter_complex_script (表达式长度 {len(filter_complex_bytes)} bytes)")
        cmd = [
            "ffmpeg", "-y",
            "-i", str(input_video),
            "-i", str(narration_wav),
            *original_audio_input,
            *bgm_input,
            "-filter_complex_script", str(fc_script),
            "-map", "0:v", "-map", aout_label,
        ]
    else:
        cmd = [
            "ffmpeg", "-y",
            "-i", str(input_video),
            "-i", str(narration_wav),
            *original_audio_input,
            *bgm_input,
            "-filter_complex", filter_complex,
            "-map", "0:v", "-map", aout_label,
        ]

    # Video filter chain: mask source subtitles first (drawbox), then burn our subtitles
    # on top. Either one forces a re-encode; with neither, the video stream is copied.
    crf = str(lib.CONFIG.get("output_crf", 18))  # env_int already clamps to >=0; keep 0 (lossless) intact
    preset = str(lib.CONFIG.get("output_preset", "veryfast") or "veryfast")
    max_h = int(lib.CONFIG.get("output_max_height", 0) or 0)
    vf_chain = []
    if mask_filter:
        vf_chain.append(mask_filter)
    vf_chain.extend(overlay_filters)
    if lib.CONFIG.get("burn_subtitles", False):
        vf_chain.append(visual_render._subtitle_burn_filter(ass_path))
    # Downscale LAST so the mask + burned subtitles render at native resolution and are then
    # scaled down with the frame (crisp). The helper forces both dimensions even so an odd
    # OUTPUT_MAX_HEIGHT can't crash libx264; 'min(ih,H)' only ever shrinks the source.
    if max_h > 0:
        vf_chain.append(visual_render._output_downscale_filter(max_h))
    # yuv420p: 10-bit/4:2:2 sources re-encoded as-is play on desktop but fail on WeChat/
    # mobile/Safari; force 8-bit 4:2:0 so every recap is universally decodable. yuv420p also
    # needs EVEN width AND height, so normalize odd dims (4:2:2/4:4:4 permit them) before the
    # encode — otherwise libx264 aborts to a 0-byte file. The downscale helper already evens out.
    even = "scale=trunc(iw/2)*2:trunc(ih/2)*2"
    notes = []
    video_filter_script = None
    if vf_chain:
        if max_h <= 0:  # no downscale in the chain to force even dims
            vf_chain.append(even)
        video_filter = ",".join(vf_chain)
        if len(video_filter.encode("utf-8")) > constants.FILTER_SCRIPT_THRESHOLD_BYTES:
            video_filter_script = Path(work_dir) / ".video_filter.txt"
            video_filter_script.write_text(video_filter, encoding="utf-8")
            cmd += ["-filter_script:v:0", str(video_filter_script)]
            lib.log(
                "使用 video filter script "
                f"(表达式长度 {len(video_filter.encode('utf-8'))} bytes)"
            )
        else:
            cmd += ["-vf", video_filter]
        cmd += ["-c:v", "libx264", "-preset", preset, "-crf", crf, "-pix_fmt", "yuv420p"]
        notes = ((["遮挡原字幕"] if mask_filter else [])
                 + ([f"视觉叠加×{len(overlay_filters)}"] if overlay_filters else [])
                 + (["压制解说字幕"] if lib.CONFIG.get("burn_subtitles", False) else [])
                 + ([f"缩放≤{max_h}p"] if max_h > 0 else []))
        lib.log(f"视频重编码: {' + '.join(notes)} (crf={crf}, preset={preset})")
    elif lib.CONFIG.get("force_video_reencode", False):
        notes = ["force_video_reencode"]
        cmd += ["-vf", even, "-c:v", "libx264", "-preset", preset, "-crf", crf, "-pix_fmt", "yuv420p"]
    else:
        cmd += ["-c:v", "copy"]

    # +faststart relocates the moov atom to the front so web/social players can start
    # before the full file downloads; valid (and beneficial) on the copy path too.
    cmd += ["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart",
            "-t", str(video_duration), str(output_path)]
    try:
        result = lib.run_cmd(cmd)
        if result.returncode != 0:
            raise RuntimeError(f"视频组装失败: {result.stderr}")
    finally:
        # 清理临时 filter_complex 脚本（无论 ffmpeg 是否成功）
        if len(filter_complex_bytes) > constants.FILTER_SCRIPT_THRESHOLD_BYTES:
            fc_script.unlink(missing_ok=True)
        if video_filter_script is not None:
            video_filter_script.unlink(missing_ok=True)

    lib.log(f"最终视频: {output_path} ({output_path.stat().st_size / 1024 / 1024:.1f}MB)")
    assembly_contract._write_assembly_qc(
        work_dir,
        assembly_contract._build_assembly_qc(
            tts_segments,
            video_duration,
            output_path=output_path,
            source_has_audio=source_has_audio,
            loudnorm_measurement=loudnorm_measurement,
            visual_qc=visual_qc,
            render_delivery={
                "video_encode_passes": 1 if (vf_chain or lib.CONFIG.get("force_video_reencode", False)) else 0,
                "reencode_reason": notes if (vf_chain or lib.CONFIG.get("force_video_reencode", False)) else [],
                "audio_sample_rate": 48000,
                "final_compat_notes": ["yuv420p"] if (vf_chain or lib.CONFIG.get("force_video_reencode", False)) else ["video_copy", "aac_48000", "faststart"],
            },
        ),
    )
    return output_path


def main():
    import argparse
    import shutil
    from pathlib import Path
    ap = argparse.ArgumentParser(
        description="video-assemble: mux narration audio over the video, duck the original, render subtitles.")
    ap.add_argument("video", help="source video (edited_source.mp4 in cut mode, else the original)")
    ap.add_argument("--work-dir", required=True)
    ap.add_argument("--tts-meta", default=None, help="tts_meta.json (default: <work-dir>/tts_meta.json)")
    ap.add_argument("--recap-stem", default=None, help="final recap filename stem (default: video stem)")
    ap.add_argument("--output-dir", default=None)
    ap.add_argument("--burn-subtitles", action=argparse.BooleanOptionalAction, default=None,
                    help="burn narration subtitles into the video (default on; --no-burn-subtitles to disable)")
    ap.add_argument("--subtitle-y-top", type=int, default=None,
                    help="inclusive top of a measured subtitle band in display-frame pixels")
    ap.add_argument("--subtitle-y-bot", type=int, default=None,
                    help="exclusive bottom of a measured subtitle band in display-frame pixels")
    ap.add_argument("--source-video", default=None,
                    help="original source video (cut mode) so timeline.json / 剪映 export reference the real clips")
    ap.add_argument("--export-jianying", action="store_true",
                    help="also export an OPTIONAL 剪映/JianYing draft from timeline.json after rendering")
    ap.add_argument("--jianying-out", default=None, help="parent dir for the 剪映 draft (default: work-dir)")
    ap.add_argument("--jianying-bundle-media", action="store_true",
                    help="copy media into the 剪映 draft folder (default on; portable/self-contained)")
    ap.add_argument("--jianying-no-bundle-media", action="store_true",
                    help="do NOT copy media into the draft — reference in place (only if 剪映 can read those paths; macOS 剪映 usually cannot)")
    args = ap.parse_args()
    work_dir = Path(args.work_dir)
    if args.burn_subtitles is not None:
        lib.CONFIG["burn_subtitles"] = args.burn_subtitles
    if (args.subtitle_y_top is None) != (args.subtitle_y_bot is None):
        ap.error("--subtitle-y-top and --subtitle-y-bot must be provided together")
    if args.subtitle_y_top is not None:
        if args.subtitle_y_top < 0 or args.subtitle_y_bot <= args.subtitle_y_top:
            ap.error("subtitle Y coordinates must satisfy 0 <= top < bot")
        lib.CONFIG["subtitle_y_top"] = args.subtitle_y_top
        lib.CONFIG["subtitle_y_bot"] = args.subtitle_y_bot
        lib.CONFIG["mask_source_subtitles"] = True
        lib.CONFIG["source_subtitle_mask_policy"] = "opt_in"
        lib.CONFIG["source_subtitle_mask_policy_declared"] = True
        # A measured band is an explicit request to conceal the known source-caption
        # pixels.  The general 0.6 translucent look can leave white glyphs visible under
        # the generated subtitles; use an opaque mask unless the caller deliberately
        # chose a different opacity through the existing environment override.
        if "SUBTITLE_MASK_OPACITY" not in os.environ:
            lib.CONFIG["subtitle_mask_opacity"] = 1.0
    if args.source_video:
        lib.CONFIG["source_video"] = args.source_video
        lib.CONFIG["source_video_explicit"] = True
    else:
        # SOURCE_VIDEO is an ambient env var in lib.CONFIG. Do not let a stale
        # shell value silently bind full-mode/direct timeline.json or JianYing
        # exports to an unrelated original; cut mode must pass --source-video.
        lib.CONFIG["source_video"] = ""
        lib.CONFIG["source_video_explicit"] = False
    if args.export_jianying:
        lib.CONFIG["export_jianying"] = True
    if args.jianying_bundle_media:
        lib.CONFIG["jianying_bundle_media"] = True
    if args.jianying_no_bundle_media:
        lib.CONFIG["jianying_bundle_media"] = False
    render_preflight._preflight_burn_subtitles()  # fail before the render if burn-in is on but ffmpeg lacks libass
    tts_meta = Path(args.tts_meta) if args.tts_meta else work_dir / "tts_meta.json"
    tts_segments = json.loads(tts_meta.read_text(encoding="utf-8"))["segments"]
    output_path = work_dir / "output.mp4"
    assemble_video(args.video, tts_segments, work_dir, output_path)
    assembly_qc = artifacts._load_work_json(work_dir, constants.ASSEMBLY_QC) or {}
    if assembly_qc.get("blocking"):
        codes = ", ".join(assembly_qc.get("blocking_codes") or ["unknown"])
        raise SystemExit(f"组装 QC 阻断交付: {codes}；详见 {work_dir / constants.ASSEMBLY_QC}")
    stem = args.recap_stem or Path(args.video).stem
    base = Path(args.output_dir) if args.output_dir else work_dir.parent
    base.mkdir(parents=True, exist_ok=True)
    final_output = assembly_contract._resolve_final_output(base, stem)
    shutil.copy2(str(output_path), str(final_output))
    manifest = assembly_contract._assembly_manifest_payload(
        args.video, tts_segments, work_dir, output_path,
        tts_meta_path=tts_meta,
        final_output=final_output,
        settings_fingerprint=assembly_settings.assembly_settings_fingerprint,
    )
    assembly_contract._write_assembly_manifest(work_dir, manifest)
    lib.log(f"组装完成: {final_output}")

    # OPTIONAL, decoupled: export a 剪映 draft from the timeline (lazy import; never
    # required by the core render path).
    if lib.CONFIG.get("export_jianying"):
        from jianying_optional import _maybe_export_jianying
        _maybe_export_jianying(work_dir, args.jianying_out, stem)

    print(json.dumps({"status": "assembled", "output": str(final_output), "work_dir": str(work_dir)},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
