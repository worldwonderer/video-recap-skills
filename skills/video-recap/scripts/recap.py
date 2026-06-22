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

from doctor import ffmpeg_has_subtitles_filter

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


def _env_bool(name, default=False):
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


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
    if str(data.get("verdict", "")).upper() == "REVISE" and n_err:
        return {"ok": False, "reason": f"error {n_err}", "review": data, "errors": n_err}
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
            "  自检：python3 skills/video-recap/scripts/doctor.py")


def _print_narration_review_pointer(work_dir, *, review_ran=True):
    """Surface the advisory narration review produced by this run, if any.

    Review is optional/fail-open. Avoid surfacing a stale narration_review.md from an
    older run when review was disabled or failed before producing fresh artifacts.
    """
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
    if getattr(args, "burn_subtitles", None) is not None:
        parts.append("--burn-subtitles" if args.burn_subtitles else "--no-burn-subtitles")
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
    return " ".join(shlex.quote(part) for part in parts)


def main():
    ap = argparse.ArgumentParser(description="Full video recap orchestrator (video-* skill bundle).")
    ap.add_argument("video", nargs="?")
    ap.add_argument("--work-dir", default=None)
    ap.add_argument("--context", default="")
    ap.add_argument("--scene-threshold", type=float, default=None)
    ap.add_argument("--style", default="纪录片")
    ap.add_argument("--edit-mode", default=os.environ.get("EDIT_MODE", "full"), choices=["full", "cut", "dub"])
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
    ap.add_argument("--burn-subtitles", action=argparse.BooleanOptionalAction, default=None,
                    help="burn narration subtitles into the video (default on; --no-burn-subtitles to disable)")
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
    ap.add_argument("--doctor", action="store_true")
    args = ap.parse_args()

    if args.doctor:
        _run("video-recap", "doctor.py")
        return
    if not args.video:
        ap.error("video is required (unless --doctor)")

    # Fail fast before any expensive understanding/VLM/ASR/TTS work if the run will burn
    # subtitles but this ffmpeg can't (otherwise it only blows up at the final render).
    _preflight_burn_subtitles(args)

    video = Path(args.video).resolve()
    work_dir = Path(args.work_dir).resolve() if args.work_dir else video.parent / f"work_dir_{video.stem}"
    work_dir.mkdir(parents=True, exist_ok=True)
    cut = args.edit_mode == "cut"
    narration_json = work_dir / "narration.json"
    clip_plan_json = work_dir / "clip_plan.json"

    edited_source = work_dir / "edited_source.mp4"

    def _understand():
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
        _run("video-cut", "cut.py", *crender)
        if not narration_json.exists():
            # PASS 2: rebuild the brief (now an OUTPUT-timeline variant) and pause for narration.
            _understand()
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
    vargs = ["--work-dir", str(work_dir), "--narration", str(narration_for_tts)]
    if args.mimo_tts_voice:
        vargs += ["--mimo-voice", args.mimo_tts_voice]
    if args.allow_partial_tts:
        vargs.append("--allow-partial-tts")
    _run("video-voiceover", "voiceover.py", *vargs)

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
    print(f"[video-recap] ✅ 完成: {final_output}")
    _print_narration_review_pointer(work_dir, review_ran=review_ran)


if __name__ == "__main__":
    main()
