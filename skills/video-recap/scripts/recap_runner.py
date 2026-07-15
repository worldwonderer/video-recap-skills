"""Orchestrate single- and multi-source recap pipelines."""

import os


from pathlib import Path


import materials as material_lib

from recap_cli import parse_args
from recap_review import run_narration_review

from recap_runtime import (
    _coerce_videos,
    _entry,
    _file_fingerprint,
    _material_settings_fingerprint,
    _optional_env_int,
    _preflight_burn_subtitles,
    _probe_display_height_or_raise,
    _read_video_duration_or_raise,
    _run,
    _write_run_manifest,
)
from recap_stage_qc import (
    _post_render_qc_metadata,
    _prepare_mimo_qc,
    _print_final_qc_pointer,
    _run_mimo_qc_stage,
    _tts_qc_metadata,
    _write_final_qc_reports,
    _write_shift_left_stage_qc,
)
from recap_timeline import (
    _continuation_command,
    _cut_narration_is_stale,
    _file_md5,
    _manifest_mismatches,
    _print_narration_review_pointer,
    _read_assembly_output,
    _read_phase_ledger,
    _surface_cut_qc,
    _write_canonical_visual_overlays,
    _write_phase_ledger,
)
from recap_runtime import (
    _build_multi_source_records,
    _write_project_run_manifest,
    _write_multi_source_manifest,
)
from recap_timeline import (
    _multi_manifest_mismatches,
    _source_work_dir,
    _understand_args_for_source,
    _write_multi_source_clip_brief,
    _write_multi_source_output_brief,
    _material_library_dir,
    _materials_enabled,
    _save_materials_enabled,
    _pause_for_agent,
)

RUN_MANIFEST = "recap_run_manifest.json"


def _run_or_restore_understanding(source_record, source_work_dir, args):
    """Run video-understanding for one source, or restore it from the material library."""
    source_work_dir = Path(source_work_dir)
    source_work_dir.mkdir(parents=True, exist_ok=True)
    source_fp = source_record["source_video_fingerprint"]
    settings_fp = source_record.get(
        "settings_fingerprint"
    ) or _material_settings_fingerprint(args)
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
            print(
                f"[video-recap] ♻️  复用素材库: {result.get('material_id')} → {source_work_dir}",
                flush=True,
            )
        elif result.get("reason"):
            print(
                f"[video-recap] 素材库未复用 {source_record['source_name']}: {result['reason']}",
                flush=True,
            )

    if not restored:
        _run(
            "video-understanding",
            "understand.py",
            *_understand_args_for_source(source_record, source_work_dir, args),
        )

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
        print(
            f"[video-recap] 💾 已沉淀素材: {meta.get('material_id')} → {lib_dir}",
            flush=True,
        )
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
            f"  - {details}"
        )


def _run_multi_cut(videos, work_dir, args):
    """Multi-video MVP: cut-first/narrate-second over a project work_dir."""
    videos = _coerce_videos(videos)
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    source_records = _build_multi_source_records(videos, args)
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
            _run_or_restore_understanding(
                record, _source_work_dir(work_dir, record), args
            )
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
    crender = [
        str(videos[0]),
        "--work-dir",
        str(work_dir),
        "--sources-manifest",
        str(manifest_path),
        "--no-narration-map",
    ]
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
        _write_multi_source_output_brief(
            work_dir, source_records, work_dir / "clip_plan_validated.json"
        )
        _write_phase_ledger(
            work_dir,
            clip_plan_fingerprint=cp_fp,
            edited_source_rendered=True,
            multi_source=True,
        )
        _pause_for_agent(
            work_dir,
            f"{narration_json}（用成片 OUTPUT 时间轴写解说，对着 {edited_source}）",
            _continuation_command(videos, work_dir, args),
            inspect_hint=(
                f"python3 {inspect_py} --work-dir {work_dir} "
                "clip-map --output-start <s> --output-end <e>"
            ),
        )
        return
    if _cut_narration_is_stale(_read_phase_ledger(work_dir), cp_fp):
        raise SystemExit(
            "clip_plan.json 已改变，但 narration.json 仍是对旧剪辑写的，会与剪后画面对不上。"
            "请删除 narration.json，重跑后按新成片重新写解说。"
        )
    _write_phase_ledger(
        work_dir,
        clip_plan_fingerprint=cp_fp,
        narration_fingerprint=_file_md5(narration_json),
        narration_written=True,
        multi_source=True,
    )
    output_duration = _read_video_duration_or_raise(edited_source)
    _run(
        "video-script",
        "validate.py",
        "--work-dir",
        work_dir,
        "--mode",
        "cut_output",
        "--output-duration",
        f"{output_duration:.3f}",
    )
    review_ran = run_narration_review(work_dir, args, run=_run, timeline="cut_output")
    _write_shift_left_stage_qc(
        work_dir,
        "pre_tts",
        metadata={"review_ran": review_ran, "timeline": "cut_output"},
    )
    vargs = ["--work-dir", str(work_dir), "--narration", str(narration_json)]
    if args.mimo_tts_voice:
        vargs += ["--mimo-voice", args.mimo_tts_voice]
    if args.voice_ref:
        vargs += ["--voice-ref", args.voice_ref]
    if args.allow_partial_tts:
        vargs.append("--allow-partial-tts")
    _run("video-voiceover", "voiceover.py", *vargs)
    _write_shift_left_stage_qc(
        work_dir, "post_tts", metadata=_tts_qc_metadata(work_dir)
    )
    overlays_path = _write_canonical_visual_overlays(work_dir, narration_json)
    _write_shift_left_stage_qc(
        work_dir, "pre_assemble", metadata={"visual_overlays": str(overlays_path)}
    )
    _run_mimo_qc_stage(work_dir, args, "pre_assemble")

    recap_stem = f"multi_{videos[0].stem}"
    aargs = [
        str(edited_source),
        "--work-dir",
        str(work_dir),
        "--recap-stem",
        recap_stem,
    ]
    if args.output_dir:
        aargs += ["--output-dir", args.output_dir]
    if args.burn_subtitles is not None:
        aargs.append(
            "--burn-subtitles" if args.burn_subtitles else "--no-burn-subtitles"
        )
    if args.export_jianying:
        aargs.append("--export-jianying")
    if args.jianying_bundle_media:
        aargs.append("--jianying-bundle-media")
    if args.jianying_no_bundle_media:
        aargs.append("--jianying-no-bundle-media")
    _run("video-assemble", "assemble.py", *aargs)

    final_dir = Path(args.output_dir) if args.output_dir else work_dir.parent
    final_output = _read_assembly_output(work_dir) or (
        final_dir / ("recap_" + recap_stem + ".mp4")
    )
    _write_shift_left_stage_qc(
        work_dir,
        "post_render",
        metadata=_post_render_qc_metadata(work_dir, final_output),
    )
    _run_mimo_qc_stage(work_dir, args, "post_render", final_output=final_output)
    final_qc_result = _write_final_qc_reports(work_dir, final_output)
    print(f"[video-recap] ✅ 完成: {final_output}")
    _print_final_qc_pointer(final_qc_result)
    _print_narration_review_pointer(work_dir, review_ran=review_ran)


def main():
    ap, args = parse_args()

    # argparse does not validate environment-derived defaults against choices.
    if args.mimo_qc not in {"off", "pre-assemble", "post-render", "both"}:
        ap.error(
            "MIMO_QC/--mimo-qc must be one of: off, pre-assemble, post-render, both"
        )

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

    explicit_mimo_voice = (
        args.mimo_tts_voice or os.environ.get("MIMO_TTS_VOICE", "").strip()
    )
    if explicit_mimo_voice and args.voice_ref:
        ap.error("--mimo-tts-voice and --voice-ref are mutually exclusive")
    if args.edit_mode == "dub" and args.voice_ref:
        ap.error(
            "--voice-ref is only supported in full/cut modes; dub clones the source voice automatically"
        )
    if args.edit_mode == "dub" and args.subtitle_y_top is not None:
        ap.error(
            "--subtitle-y-top/--subtitle-y-bot are only supported in full/cut modes"
        )
    if (args.subtitle_y_top is None) != (args.subtitle_y_bot is None):
        ap.error("--subtitle-y-top and --subtitle-y-bot must be provided together")
    if args.subtitle_y_top is not None:
        if args.subtitle_y_top < 0 or args.subtitle_y_bot <= args.subtitle_y_top:
            ap.error("subtitle Y coordinates must satisfy 0 <= top < bot")

    videos = _coerce_videos(args.video)
    if len(videos) > 1 and args.edit_mode != "cut":
        raise SystemExit(
            "多视频输入当前 MVP 只支持 --edit-mode cut；full/dub 请一次输入一个视频。"
        )
    if len(videos) > 1 and args.subtitle_y_top is not None:
        ap.error("多视频 cut 暂不支持全局 subtitle Y 坐标；各源字幕带可能不同")
    if args.subtitle_y_top is not None:
        canvas_height = _probe_display_height_or_raise(
            videos[0], require_square_pixels=True
        )
        if args.subtitle_y_bot > canvas_height:
            ap.error(
                f"subtitle Y coordinates exceed display canvas height {canvas_height}: "
                f"bot={args.subtitle_y_bot}"
            )
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
        _prepare_mimo_qc(work_dir, args)
        _run_multi_cut(videos, work_dir, args)
        return

    video = videos[0]
    work_dir = (
        Path(args.work_dir).resolve()
        if args.work_dir
        else video.parent / f"work_dir_{video.stem}"
    )
    work_dir.mkdir(parents=True, exist_ok=True)
    _prepare_mimo_qc(work_dir, args)
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
        if brief.exists() and "Research the story FIRST" in brief.read_text(
            encoding="utf-8"
        ):
            print(
                "[video-recap] ⚑ 理解素材偏薄：先按 brief 顶部「Research the story FIRST」调研并写 "
                "background_research.json，再写解说，避免看图说话。"
            )
        print(
            f"[video-recap] ⏸  阅读 {brief}（按 video-script 规则）后写入 {need_text}"
        )
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
                f"  - {details}"
            )

    if args.edit_mode == "dub":
        # Dub mode: EN→ZH translation-dub in the original cloned voice (replaces speech, not
        # overlay). One pause: prepare (ASR + sentence-seg + reference) -> agent writes the
        # Chinese translation (dub_script.json) -> render (clone TTS + full-replace mux).
        dub_script = work_dir / "dub_script.json"
        if not dub_script.exists():
            _run(
                "video-voiceover",
                "dub.py",
                "--stage",
                "prepare",
                "--video",
                str(video),
                "--work-dir",
                str(work_dir),
            )
            _write_run_manifest(work_dir, video, args)
            cont = _continuation_command(video, work_dir, args)
            print("=" * 50)
            print(
                f"[video-recap] ⏸  阅读 {work_dir / 'dub_brief.md'}，把英文原声转写切分并翻译成中文，写入 {dub_script}"
            )
            print(
                '[video-recap]    格式 [{"start": 起秒, "end": 止秒, "zh": "译文"}]（按 start 升序）；逐句忠实、跟原声节奏一致、保留原音色'
            )
            print(f"[video-recap]    写完后重跑继续: {cont}")
            print("=" * 50)
            return
        _reject_stale_manifest()
        _run(
            "video-voiceover",
            "dub.py",
            "--stage",
            "render",
            "--video",
            str(video),
            "--work-dir",
            str(work_dir),
        )
        print(f"[video-recap] ✅ 配音完成: {work_dir / ('dub_' + video.stem + '.mp4')}")
        return

    if not cut:
        # Full mode: a single pause (understand -> agent writes narration.json -> produce).
        if not narration_json.exists():
            _understand()
            _write_run_manifest(work_dir, video, args)
            _pause(
                f"{narration_json}",
                inspect_hint=f"python3 {inspect_py} --work-dir {work_dir} state",
            )
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
            _pause(
                f"{clip_plan_json}（只写剪辑计划；解说下一步对着剪好的成片写）",
                inspect_hint=f"python3 {inspect_py} --work-dir {work_dir} state",
            )
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
            _write_phase_ledger(
                work_dir, clip_plan_fingerprint=cp_fp, edited_source_rendered=True
            )
            _pause(
                f"{narration_json}（用成片 OUTPUT 时间轴写解说，对着 {edited_source}）",
                inspect_hint=(
                    f"python3 {inspect_py} --work-dir {work_dir} "
                    "clip-map --output-start <s> --output-end <e>  # 核对输出↔原片时间轴"
                ),
            )
            return
        if _cut_narration_is_stale(_read_phase_ledger(work_dir), cp_fp):
            raise SystemExit(
                "clip_plan.json 已改变，但 narration.json 仍是对旧剪辑写的，会与剪后画面对不上。"
                "请删除 narration.json，重跑后按新成片重新写解说。"
            )
        _write_phase_ledger(
            work_dir,
            clip_plan_fingerprint=cp_fp,
            narration_fingerprint=_file_md5(narration_json),
            narration_written=True,
        )
        output_duration = _read_video_duration_or_raise(edited_source)
        _run(
            "video-script",
            "validate.py",
            "--work-dir",
            work_dir,
            "--mode",
            "cut_output",
            "--output-duration",
            f"{output_duration:.3f}",
        )
        narration_for_tts = narration_json
        assemble_video_path = edited_source
    review_ran = run_narration_review(
        work_dir,
        args,
        run=_run,
        timeline="cut_output" if cut else "source",
    )
    _write_shift_left_stage_qc(
        work_dir,
        "pre_tts",
        metadata={
            "review_ran": review_ran,
            "timeline": "cut_output" if cut else "source",
        },
    )
    vargs = ["--work-dir", str(work_dir), "--narration", str(narration_for_tts)]
    if args.mimo_tts_voice:
        vargs += ["--mimo-voice", args.mimo_tts_voice]
    if args.voice_ref:
        vargs += ["--voice-ref", args.voice_ref]
    if args.allow_partial_tts:
        vargs.append("--allow-partial-tts")
    _run("video-voiceover", "voiceover.py", *vargs)
    _write_shift_left_stage_qc(
        work_dir, "post_tts", metadata=_tts_qc_metadata(work_dir)
    )
    overlays_path = _write_canonical_visual_overlays(work_dir, narration_for_tts)
    _write_shift_left_stage_qc(
        work_dir, "pre_assemble", metadata={"visual_overlays": str(overlays_path)}
    )
    _run_mimo_qc_stage(work_dir, args, "pre_assemble")

    aargs = [
        str(assemble_video_path),
        "--work-dir",
        str(work_dir),
        "--recap-stem",
        video.stem,
    ]
    if args.output_dir:
        aargs += ["--output-dir", args.output_dir]
    if args.burn_subtitles is not None:
        aargs.append(
            "--burn-subtitles" if args.burn_subtitles else "--no-burn-subtitles"
        )
    if args.subtitle_y_top is not None:
        aargs += [
            "--subtitle-y-top",
            str(args.subtitle_y_top),
            "--subtitle-y-bot",
            str(args.subtitle_y_bot),
        ]
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
    final_output = _read_assembly_output(work_dir) or (
        final_dir / ("recap_" + video.stem + ".mp4")
    )
    _write_shift_left_stage_qc(
        work_dir,
        "post_render",
        metadata=_post_render_qc_metadata(work_dir, final_output),
    )
    _run_mimo_qc_stage(work_dir, args, "post_render", final_output=final_output)
    final_qc_result = _write_final_qc_reports(work_dir, final_output)
    print(f"[video-recap] ✅ 完成: {final_output}")
    _print_final_qc_pointer(final_qc_result)
    _print_narration_review_pointer(work_dir, review_ran=review_ran)
