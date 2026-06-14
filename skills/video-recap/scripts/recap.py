#!/usr/bin/env python3
"""video-recap orchestrator.

Chains the independent video-* stage skills into a full narrated recap:

  video-understanding  ->  (agent writes narration.json per video-script)  ->
  [video-cut]  ->  video-voiceover  ->  video-assemble

Each stage is a self-contained sibling skill invoked as a subprocess; they communicate
only through JSON/MP4 artifacts in the shared work_dir. Stateless: rerun the same command
after writing narration.json to continue (understanding artifacts are reused if fresh).
"""
import argparse
import subprocess
import sys
from pathlib import Path

BUNDLE = Path(__file__).resolve().parents[2]  # the skills/ directory


def _entry(skill, script):
    return BUNDLE / skill / "scripts" / script


def _run(skill, script, *cli_args):
    cmd = [sys.executable, str(_entry(skill, script)), *map(str, cli_args)]
    print(f"[video-recap] ▶ {skill}/{script}", flush=True)
    res = subprocess.run(cmd)
    if res.returncode != 0:
        raise SystemExit(f"{skill}/{script} 失败 (exit {res.returncode})")


def main():
    ap = argparse.ArgumentParser(description="Full video recap orchestrator (video-* skill bundle).")
    ap.add_argument("video", nargs="?")
    ap.add_argument("--work-dir", default=None)
    ap.add_argument("--context", default="")
    ap.add_argument("--scene-threshold", type=float, default=None)
    ap.add_argument("--style", default="纪录片")
    ap.add_argument("--edit-mode", default="full", choices=["full", "cut"])
    ap.add_argument("--target-duration", default=None)
    ap.add_argument("--skip-asr", action="store_true")
    ap.add_argument("--mimo-video-overview", action="store_true")
    ap.add_argument("--consolidate", action="store_true", help="build understanding index (Pass B)")
    ap.add_argument("--consolidate-asr", action="store_true", help="also clean ASR (Pass A)")
    ap.add_argument("--mimo-tts-voice", default=None, help="MiMo TTS voice")
    ap.add_argument("--burn-subtitles", action="store_true")
    ap.add_argument("--output-dir", default=None)
    ap.add_argument("--export-jianying", action="store_true",
                    help="also export an OPTIONAL 剪映/JianYing draft (decoupled; never required)")
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
        if args.skip_asr:
            uargs.append("--skip-asr")
        if args.mimo_video_overview:
            uargs.append("--mimo-video-overview")
        if args.consolidate:
            uargs.append("--consolidate")
        if args.consolidate_asr:
            uargs.append("--consolidate-asr")
        _run("video-understanding", "understand.py", *uargs)
        brief = work_dir / "agent_narration_brief.md"
        cont = f"python3 {_entry('video-recap', 'recap.py')} {video} --work-dir {work_dir}"
        if cut:
            cont += " --edit-mode cut"
        print("=" * 50)
        need = f"{narration_json}" + (f" 和 {clip_plan_json}" if cut else "")
        print(f"[video-recap] ⏸  阅读 {brief}（按 video-script 规则）后写入 {need}")
        print(f"[video-recap]    写完后重跑继续: {cont}")
        print("=" * 50)
        return

    # Phase B: produce
    _run("video-script", "validate.py", "--work-dir", work_dir, "--mode", args.edit_mode)
    assemble_video_path = video
    if cut:
        cargs = [str(video), "--work-dir", str(work_dir)]
        if args.target_duration:
            cargs += ["--target-duration", args.target_duration]
        _run("video-cut", "cut.py", *cargs)
        assemble_video_path = work_dir / "edited_source.mp4"

    vargs = ["--work-dir", str(work_dir)]
    if args.mimo_tts_voice:
        vargs += ["--mimo-voice", args.mimo_tts_voice]
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
    _run("video-assemble", "assemble.py", *aargs)

    final_dir = Path(args.output_dir) if args.output_dir else work_dir.parent
    print(f"[video-recap] ✅ 完成: {final_dir / ('recap_' + video.stem + '.mp4')}")


if __name__ == "__main__":
    main()
