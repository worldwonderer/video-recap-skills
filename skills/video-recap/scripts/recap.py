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
import os
import shlex
import subprocess
import sys
from pathlib import Path

BUNDLE = Path(__file__).resolve().parents[2]  # the skills/ directory
RUN_MANIFEST = "recap_run_manifest.json"
ASSEMBLY_MANIFEST = "assembly_manifest.json"
PHASE_LEDGER = "recap_phase.json"


def _entry(skill, script):
    return BUNDLE / skill / "scripts" / script


def _run(skill, script, *cli_args):
    cmd = [sys.executable, str(_entry(skill, script)), *map(str, cli_args)]
    print(f"[video-recap] ▶ {skill}/{script}", flush=True)
    res = subprocess.run(cmd)
    if res.returncode != 0:
        raise SystemExit(f"{skill}/{script} 失败 (exit {res.returncode})")


def _file_fingerprint(path, chunk_size=1024 * 1024):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


def _run_manifest_payload(video, args):
    return {
        "schema_version": 1,
        "source_video": str(Path(video).resolve()),
        "source_video_fingerprint": _file_fingerprint(video),
        "settings": {
            "context": args.context,
            "scene_threshold": args.scene_threshold,
            "style": args.style,
            "edit_mode": args.edit_mode,
            "target_duration": args.target_duration,
            "skip_asr": bool(args.skip_asr),
            "mimo_video_overview": bool(args.mimo_video_overview),
            "consolidate": bool(args.consolidate),
            "consolidate_asr": bool(args.consolidate_asr),
        },
    }


def _write_run_manifest(work_dir, video, args):
    payload = _run_manifest_payload(video, args)
    (work_dir / RUN_MANIFEST).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


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


def _cut_narration_is_stale(ledger, current_clip_plan_fp, current_narration_fp):
    """Stale iff the clip_plan changed since the ledger was written but the narration did NOT
    — i.e. the agent re-cut without re-writing the narration, so it describes the old cut.
    If BOTH changed (narration re-authored for the new clips) it is fresh."""
    if not ledger:
        return False
    recorded_cp = ledger.get("clip_plan_fingerprint")
    recorded_narr = ledger.get("narration_fingerprint")
    return bool(
        recorded_cp is not None
        and recorded_cp != current_clip_plan_fp
        and recorded_narr == current_narration_fp
    )


def _continuation_command(video, work_dir, args):
    parts = [sys.executable, str(_entry("video-recap", "recap.py")), str(video), "--work-dir", str(work_dir)]
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
    if getattr(args, "allow_partial_tts", False):
        parts.append("--allow-partial-tts")
    if getattr(args, "burn_subtitles", False):
        parts.append("--burn-subtitles")
    if getattr(args, "output_dir", None):
        parts += ["--output-dir", args.output_dir]
    if getattr(args, "export_jianying", False):
        parts.append("--export-jianying")
    if getattr(args, "jianying_bundle_media", False):
        parts.append("--jianying-bundle-media")
    if getattr(args, "jianying_no_bundle_media", False):
        parts.append("--jianying-no-bundle-media")
    return " ".join(shlex.quote(part) for part in parts)


def main():
    ap = argparse.ArgumentParser(description="Full video recap orchestrator (video-* skill bundle).")
    ap.add_argument("video", nargs="?")
    ap.add_argument("--work-dir", default=None)
    ap.add_argument("--context", default="")
    ap.add_argument("--scene-threshold", type=float, default=None)
    ap.add_argument("--style", default="纪录片")
    ap.add_argument("--edit-mode", default=os.environ.get("EDIT_MODE", "full"), choices=["full", "cut"])
    ap.add_argument("--target-duration", default=os.environ.get("TARGET_DURATION") or None)
    ap.add_argument("--allow-sparse-cut", action="store_true",
                    help="cut mode: accept a sparse/heavily-dropped narration mapping instead of failing the cut preflight")
    ap.add_argument("--skip-asr", action="store_true")
    ap.add_argument("--mimo-video-overview", action="store_true")
    ap.add_argument("--consolidate", action=argparse.BooleanOptionalAction, default=True,
                    help="build the understanding story index (Pass B); default ON, --no-consolidate to skip")
    ap.add_argument("--consolidate-asr", action="store_true", help="also clean ASR (Pass A)")
    ap.add_argument("--mimo-tts-voice", default=None, help="MiMo TTS voice")
    ap.add_argument("--allow-partial-tts", action="store_true",
                    help="allow video-voiceover to continue when some narration segments fail TTS")
    ap.add_argument("--burn-subtitles", action="store_true")
    ap.add_argument("--output-dir", default=None)
    ap.add_argument("--export-jianying", action="store_true",
                    help="also export an OPTIONAL 剪映/JianYing draft (decoupled; never required)")
    ap.add_argument("--jianying-bundle-media", action="store_true",
                    help="copy media into the 剪映 draft (default on; portable to another machine)")
    ap.add_argument("--jianying-no-bundle-media", action="store_true",
                    help="reference media in place instead of copying it into the draft")
    ap.add_argument("--doctor", action="store_true")
    args = ap.parse_args()

    if args.doctor:
        _run("video-recap", "doctor.py")
        return
    if not args.video:
        ap.error("video is required (unless --doctor)")

    video = Path(args.video).resolve()
    work_dir = Path(args.work_dir).resolve() if args.work_dir else video.parent / f"work_dir_{video.stem}"
    work_dir.mkdir(parents=True, exist_ok=True)
    cut = args.edit_mode == "cut"
    narration_json = work_dir / "narration.json"
    clip_plan_json = work_dir / "clip_plan.json"

    need_script = not narration_json.exists() or (cut and not clip_plan_json.exists())
    if need_script:
        # Phase A: understand -> brief, then pause for the agent to write narration.json (+ clip_plan.json)
        uargs = [str(video), "--work-dir", str(work_dir), "--style", args.style]
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
        _run("video-understanding", "understand.py", *uargs)
        _write_run_manifest(work_dir, video, args)
        brief = work_dir / "agent_narration_brief.md"
        cont = _continuation_command(video, work_dir, args)
        print("=" * 50)
        need = f"{narration_json}" + (f" 和 {clip_plan_json}" if cut else "")
        # The brief fires a research directive only when the substrate is thin/empty and no
        # background_research.json exists yet. Amplify it here so the agent researches the
        # title BEFORE writing, instead of shipping cold "看图说话".
        if brief.exists() and "Research the story FIRST" in brief.read_text(encoding="utf-8"):
            print("[video-recap] ⚑ 理解素材偏薄：先按 brief 顶部「Research the story FIRST」调研并写 "
                  "background_research.json，再写解说，避免看图说话。")
        print(f"[video-recap] ⏸  阅读 {brief}（按 video-script 规则）后写入 {need}")
        print(f"[video-recap]    写完后重跑继续: {cont}")
        print("=" * 50)
        return

    # Phase B: produce
    mismatches = _manifest_mismatches(work_dir, video, args)
    if mismatches:
        details = "\n  - ".join(mismatches)
        raise SystemExit(
            "work_dir 与当前 recap 输入不匹配，拒绝复用既有 narration/clip_plan；"
            "请使用新的 --work-dir，或删除旧产物后重新运行 Phase A。\n"
            f"  - {details}"
        )
    if cut:
        # Phase ledger: refuse to drive a stale narration (written for a previous clip_plan)
        # into the cut/TTS. If clip_plan changed but narration did not, it describes the old cut.
        cp_fp = _file_md5(clip_plan_json)
        narr_fp = _file_md5(narration_json)
        if _cut_narration_is_stale(_read_phase_ledger(work_dir), cp_fp, narr_fp):
            raise SystemExit(
                "clip_plan.json 已改变，但 narration.json 没有相应更新：解说仍是对旧剪辑写的，"
                "会与剪后画面对不上。请按新的保留片段重写 narration.json（或删除它重新进入暂停）。")
        _write_phase_ledger(work_dir, clip_plan_fingerprint=cp_fp,
                            narration_fingerprint=narr_fp, narration_written=True)
        # Normalize the clip plan FIRST (cheap, no render) so validate lints the SAME
        # padded/pruned clips the mapper uses — otherwise validate sees the raw plan and
        # can pass a beat the mapper later silently drops.
        ncargs = [str(video), "--work-dir", str(work_dir), "--normalize-only"]
        if args.target_duration:
            ncargs += ["--target-duration", args.target_duration]
        _run("video-cut", "cut.py", *ncargs)
    _run("video-script", "validate.py", "--work-dir", work_dir, "--mode", args.edit_mode)
    assemble_video_path = video
    if cut:
        cargs = [str(video), "--work-dir", str(work_dir)]
        if args.target_duration:
            cargs += ["--target-duration", args.target_duration]
        if args.allow_sparse_cut:
            cargs.append("--allow-sparse-cut")
        _run("video-cut", "cut.py", *cargs)
        assemble_video_path = work_dir / "edited_source.mp4"

    narration_for_tts = work_dir / ("narration_mapped.json" if cut else "narration.json")
    vargs = ["--work-dir", str(work_dir), "--narration", str(narration_for_tts)]
    if args.mimo_tts_voice:
        vargs += ["--mimo-voice", args.mimo_tts_voice]
    if args.allow_partial_tts:
        vargs.append("--allow-partial-tts")
    _run("video-voiceover", "voiceover.py", *vargs)

    aargs = [str(assemble_video_path), "--work-dir", str(work_dir), "--recap-stem", video.stem]
    if args.output_dir:
        aargs += ["--output-dir", args.output_dir]
    if args.burn_subtitles:
        aargs.append("--burn-subtitles")
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
    print(f"[video-recap] ✅ 完成: {final_output}")


if __name__ == "__main__":
    main()
