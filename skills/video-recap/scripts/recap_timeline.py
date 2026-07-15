"""Own recap timeline artifacts, continuation state, and cut QC surfaces."""

import hashlib

import json

import math

import os

import shlex


import sys

from pathlib import Path


from recap_runtime import (
    _coerce_videos,
    _entry,
    _load_json,
    _load_run_manifest,
    _multi_run_manifest_payload,
    _run_manifest_payload,
)

ASSEMBLY_MANIFEST = "assembly_manifest.json"

PHASE_LEDGER = "recap_phase.json"

CUT_TIMELINE_CRAFT_BULLETS = [
    "- 片段顺序必须服务同一条故事主线，而不是无序高光；可使用 0–1 个 cold open，随后回到 setup → turn → escalation → payoff。",
    "- 每个片段必须对应 `recap_story_plan.json` 中的一个 change-based beat；删除后不损失因果、人物或情绪的片段通常不保留。",
    "- `reason` 统一写成 `beat_id | function | change | POV | preferred moment | 入点 | 出点`，不能只写 hook、重要剧情或事件摘要。",
    "- 优先保留因果、揭示、决定、关系移动、情绪转向与不可替代的表演/反应；跳过片尾、广告、重复静态画面和水印废片段。",
    "- 片段长度服从具体时刻；在完整台词、完整动作或自然声音边界结束，避免原声从半句中切入或切出。",
]

MULTI_SOURCE_NARRATION_CRAFT_BULLETS = [
    "- `narration.json` 只使用 edited_source.mp4 的 OUTPUT 时间线，不使用任一原片时间。",
    "- 先为每个 beat 指定 `audio_owner`，再决定是否需要旁白；允许 original_dialogue、action_sound、ambience、music、silence 或 narration 主导。",
    "- 旁白只承担 context、causal_link、foreshadow、interpretation 或 transition；`narration_job=none` 的 beat 不写旁白。",
    "- 7:3 不是配额，只是素材无法给出更好判断时的粗略回退；强对白、动作声、环境或沉默可以完整拥有一个 beat。",
    "- 旁白拥有 beat 时才写成 2–4 个完整句子的连续 BLOCK；不要把每个 beat 都机械变成旁白块或固定原声留白。",
    "- 使用 kept-clip map 与每个 source work_dir 核对人物、事实、ASR 和上下文；跨源转场必须有明确叙事任务。",
]

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
        print(
            f"[video-recap] 🧭 Grounding QC: {verdict} · ranges {len(ranges)}{suffix} → {qc_path}",
            flush=True,
        )
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
        print(
            f"[video-recap] 📋 解说评审（建议性，不拦截）: {tag} · "
            f"{len(findings)} 条意见（error {n_err}）→ {review_md}"
        )
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
        return [
            "缺少或无法读取 recap_run_manifest.json；不能证明 work_dir 属于当前视频/参数"
        ]
    mismatches = []
    for key in ("source_video", "source_video_fingerprint"):
        if actual.get(key) != expected.get(key):
            mismatches.append(
                f"{key}: expected {expected.get(key)!r}, got {actual.get(key)!r}"
            )
    if _settings_for_compare(actual.get("settings")) != _settings_for_compare(
        expected.get("settings")
    ):
        mismatches.append("settings: 当前 CLI/env 参数与 Phase A manifest 不匹配")
    return mismatches


def _multi_manifest_mismatches(work_dir, videos, args, source_records):
    expected = _multi_run_manifest_payload(videos, args, source_records)
    actual = _load_run_manifest(work_dir)
    if not actual:
        return [
            "缺少或无法读取 recap_run_manifest.json；不能证明 work_dir 属于当前多视频/参数"
        ]
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
        mismatches.append(
            "sources: 当前输入视频列表/顺序/source_id/fingerprint 与 Phase A manifest 不匹配"
        )
    if _settings_for_compare(actual.get("settings")) != _settings_for_compare(
        expected.get("settings")
    ):
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
        json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8"
    )
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
    parts = [
        sys.executable,
        str(_entry("video-recap", "recap.py")),
        *[str(v) for v in videos],
        "--work-dir",
        str(work_dir),
    ]
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
    mimo_mode = getattr(args, "mimo_qc", "off")
    if mimo_mode != "off":
        parts += ["--mimo-qc", mimo_mode]
    if getattr(args, "mimo_qc_refresh", False):
        parts.append("--mimo-qc-refresh")
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
        parts.append(
            "--burn-subtitles" if args.burn_subtitles else "--no-burn-subtitles"
        )
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
        parts.append(
            "--review-narration" if args.review_narration else "--no-review-narration"
        )
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
    uargs = [
        source_record["source_path"],
        "--work-dir",
        str(source_work_dir),
        "--style",
        args.style,
    ]
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
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit < 8:
        return text[:limit]

    def section(heading):
        lines = text.splitlines()
        try:
            start = lines.index(heading)
        except ValueError:
            return ""
        end = next(
            (
                index
                for index in range(start + 1, len(lines))
                if lines[index].startswith("## ")
            ),
            len(lines),
        )
        return "\n".join(lines[start:end]).strip()

    def clipped(value, budget):
        if len(value) <= budget:
            return value
        marker = "\n…\n"
        if budget <= len(marker) + 1:
            return value[:budget]
        usable = max(0, budget - len(marker))
        head = max(1, round(usable * 0.7))
        tail = max(0, usable - head)
        suffix = value[-tail:].lstrip() if tail else ""
        return value[:head].rstrip() + marker + suffix

    # The generic writing contract occupies the front of every generated brief. A raw
    # prefix therefore drops the source facts that multi-source planning actually needs.
    # Give evidence-bearing sections equal space and retain both their opening and tail.
    preferred = [
        section("## Story context (from background_research.json)"),
        section("## Understanding index (from consolidate.py)"),
        section("## ASR writing chunks (semantic windows)"),
        section("## Scene timing guide"),
    ]
    preferred = [value for value in preferred if value]
    if preferred:
        separators = 2 * (len(preferred) - 1)
        per_section = max(1, (limit - separators) // len(preferred))
        excerpt = "\n\n".join(clipped(value, per_section) for value in preferred)
        return excerpt[:limit]

    marker = "\n…\n"
    usable = max(0, limit - len(marker))
    head = max(1, usable // 4)
    tail = max(0, usable - head)
    suffix = text[-tail:].lstrip() if tail else ""
    return text[:head].rstrip() + marker + suffix


def _write_multi_source_clip_brief(work_dir, source_records, args):
    lines = [
        "# Multi-source Clip Plan Brief",
        "",
        "你正在做多视频剪辑复盘。当前 MVP 只支持 `--edit-mode cut`：先做跨素材的故事与视听决定，再写 `clip_plan.json`；下一步会剪出 `edited_source.mp4`，最后按 OUTPUT 时间轴写 `narration.json`。",
        "",
        "## 创作决定",
        "",
        "先比较至少两个可行剪辑假设，再写 `recap_story_plan.json` 与 `visual_audio_board.json`。前者记录观众承诺、POV、戏剧问题、选定主线及 change-based beats；后者记录每拍的具体画面/反应、入点/出点、原声锚点、`audio_owner` 与 `narration_job`。",
        "",
        "多视频不是把每个来源各做一段小总结。每个来源片段都必须服务同一条主线，并用 `source_id` 保留证据归属。这两份计划是 Agent 与建议型评审使用的工作记录，不是 CLI 渲染门禁。",
        "",
        "## 必须写入的格式",
        "",
        "```json",
        '{"target_duration":"10m","clips":[{"source_id":"src_xxx","start":12.0,"end":38.0,"reason":"b01 | hook | knowledge: unknown→threat | POV=主角 | 保留倾听反应 | 入点=问题已问出 | 出点=沉默落地"}]}',
        "```",
        "",
        "- 每个 clip 必须带 `source_id`。",
        "- `start`/`end` 是对应 source 原视频时间（秒）。",
        "- 不同 `source_id` 的相同时间段不算重叠；同一 `source_id` 内不要重复/重叠，除非你明确接受稀疏/重复剪辑风险。",
        '- 素材库是文件系统 JSON/MD/JSONL；需要找历史素材时直接 `grep -R "关键词" <material-library-dir>`。',
        "",
        "## 剪辑规则",
        *CUT_TIMELINE_CRAFT_BULLETS,
        "- 跨来源选择必须服务共享主线；除非本来就是 setup / turn / payoff 的设计，不要让某个来源变成脱节的小复盘。",
        "- 选片前使用下方 source work_dir（`sources/<source_id>`）核对 scenes.json、ASR、索引和逐来源 brief。",
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
    (Path(work_dir) / "agent_narration_brief.md").write_text(
        "\n".join(lines).rstrip() + "\n", encoding="utf-8"
    )


def _write_multi_source_output_speech_evidence(work_dir, source_records, plan):
    """Map each source's speech and quiet evidence onto the combined output clock."""
    clips = plan.get("clips", []) if isinstance(plan, dict) else []
    source_by_id = {str(row["source_id"]): row for row in source_records}
    cache = {}

    def load_source(source_id):
        if source_id in cache:
            return cache[source_id]
        record = source_by_id.get(source_id)
        source_dir = _source_work_dir(work_dir, record) if record else None
        anchors = _load_json(source_dir / "speech_boundary_anchors.json") if source_dir else None
        speech = None
        for name in ("asr_result.json", "asr_clean.json"):
            speech = _load_json(source_dir / name) if source_dir else None
            rows = speech.get("segments", []) if isinstance(speech, dict) else speech
            if isinstance(rows, list) and any(
                isinstance(row, dict) and str(row.get("text") or "").strip()
                for row in rows
            ):
                break
        if isinstance(speech, dict):
            speech = speech.get("segments", [])
        quiet = _load_json(source_dir / "silence_periods.json") if source_dir else None
        cache[source_id] = (
            anchors.get("sentence_anchors", []) if isinstance(anchors, dict) else [],
            speech if isinstance(speech, list) else [],
            quiet if isinstance(quiet, list) else [],
        )
        return cache[source_id]

    mapped_anchors, mapped_speech, mapped_quiet = [], [], []
    for clip in clips:
        source_id = str(clip.get("source_id") or "")
        try:
            source_start = float(clip["source_start"])
            source_end = float(clip["source_end"])
            output_start = float(clip["output_start"])
        except (KeyError, TypeError, ValueError):
            continue
        anchors, speech_rows, quiet_rows = load_source(source_id)
        for anchor in anchors:
            try:
                when = float(anchor.get("time"))
            except (AttributeError, TypeError, ValueError):
                continue
            if source_start - 0.05 <= when <= source_end + 0.05:
                item = dict(anchor)
                try:
                    pause = float(item.get("pause_start", when))
                except (TypeError, ValueError):
                    pause = when
                if not math.isfinite(pause):
                    pause = when
                pause = max(source_start, min(pause, when))
                item.update(
                    source_id=source_id,
                    source_time=round(when, 3),
                    time=round(output_start + when - source_start, 3),
                    source_pause_start=round(pause, 3),
                    pause_start=round(output_start + pause - source_start, 3),
                )
                mapped_anchors.append(item)
        for rows, destination, require_text in (
            (speech_rows, mapped_speech, True),
            (quiet_rows, mapped_quiet, False),
        ):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                if require_text and not str(row.get("text") or "").strip():
                    continue
                if not require_text and bool(row.get("has_speech", False)):
                    continue
                try:
                    start = max(source_start, float(row["start"]))
                    end = min(source_end, float(row["end"]))
                except (KeyError, TypeError, ValueError):
                    continue
                if end <= start:
                    continue
                item = dict(row)
                item.update(
                    source_id=source_id,
                    source_start=round(start, 3),
                    source_end=round(end, 3),
                    start=round(output_start + start - source_start, 3),
                    end=round(output_start + end - source_start, 3),
                )
                destination.append(item)

    payload = {
        "schema_version": 2,
        "artifact": "speech_boundary_anchors_output.json",
        "timeline": "cut_output",
        "source_artifact": "multi_source_manifest.json",
        "clip_plan_fingerprint": hashlib.md5(
            json.dumps(
                plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
            ).encode("utf-8")
        ).hexdigest(),
        "sentence_anchors": sorted(mapped_anchors, key=lambda row: float(row["time"])),
        "speech_spans": sorted(mapped_speech, key=lambda row: (row["start"], row["end"])),
        "quiet_windows": sorted(mapped_quiet, key=lambda row: (row["start"], row["end"])),
    }
    Path(work_dir, "speech_boundary_anchors_output.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return payload


def _write_multi_source_output_brief(work_dir, source_records, validated_plan_path):
    plan = _load_json(validated_plan_path)
    clips = plan.get("clips", []) if isinstance(plan, dict) else []
    source_by_id = {s["source_id"]: s for s in source_records}
    speech_evidence = _write_multi_source_output_speech_evidence(
        work_dir, source_records, plan
    )
    lines = [
        "# Multi-source Output Narration Brief",
        "",
        "现在 `edited_source.mp4` 已经由多个源视频剪好。请对剪后成片的 OUTPUT 时间轴写 `narration.json`。",
        "",
        "## 更新创作决定",
        "",
        "先查看 `edited_source.mp4` 与下方 kept-clip map，在 `visual_audio_board.json` 中补齐 OUTPUT 起止时间，并根据实际成片重新确认每拍的 `audio_owner`、原声锚点与 `narration_job`。如剪后顺序改变了主线或情绪路径，同时更新 `recap_story_plan.json`。",
        "",
        "beat 对应关系保留在视听板中；`narration.json` 仍只承载时间、文本与朗读参数，CLI 不声称校验计划映射。",
        "",
        "## narration.json 格式",
        "",
        "```json",
        '[{"start":0.0,"end":4.0,"narration":"解说文本。","pause_after_ms":250,"overlaps_speech":true,"emotion":"平静"}]',
        "```",
        "",
        "注意：`start`/`end` 是剪后成片时间，不是原视频时间。",
        "",
        "## 输出时间线写作规则",
        *MULTI_SOURCE_NARRATION_CRAFT_BULLETS,
        "- 旁白增加上下文、因果、预期、证据支持的解释或跨源过渡，不复述画面像素；先保人物与原声，再润色句子。",
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
    anchors = speech_evidence["sentence_anchors"]
    if anchors:
        lines += ["", "## 原声句末安全切入点"]
        lines.extend(
            f"- {float(row['time']):.3f}s ({row.get('source_id')})"
            for row in anchors
            if row.get("confidence") in {"high", "medium"}
        )
    lines += ["", "## Source work dirs"]
    for s in source_records:
        lines.append(f"- {s['source_id']}: `{_source_work_dir(work_dir, s)}`")
    (Path(work_dir) / "agent_narration_brief.md").write_text(
        "\n".join(lines).rstrip() + "\n", encoding="utf-8"
    )


def _qc_status_is_blocking(value):
    if isinstance(value, str):
        return value.strip().lower() in {
            "blocking",
            "blocked",
            "fail",
            "failed",
            "error",
        }
    return bool(value) if isinstance(value, bool) else False


def _cut_qc_blocking_reasons(qc):
    if not isinstance(qc, dict):
        return []
    reasons = []
    for key in (
        "status",
        "target_duration_status",
        "duration_status",
        "boundary_status",
    ):
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
    for key in (
        "target_duration_status",
        "total_duration",
        "clip_count",
        "join_fade_ms",
    ):
        if key in qc:
            parts.append(f"{key}={qc.get(key)}")
    geometry = qc.get("output_geometry")
    if isinstance(geometry, dict):
        dims = "x".join(
            str(geometry.get(k))
            for k in ("width", "height")
            if geometry.get(k) is not None
        )
        fps = geometry.get("fps")
        reason = geometry.get("reason") or qc.get("output_geometry_reason")
        fps_suffix = f"@{fps}fps" if fps else ""
        reason_suffix = f" reason={reason}" if reason else ""
        parts.append(f"output_geometry={dims or geometry}{fps_suffix}{reason_suffix}")
    warnings = qc.get("warnings")
    if warnings:
        parts.append(
            f"warnings={len(warnings) if isinstance(warnings, list) else warnings}"
        )
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
    return (
        args.material_library_dir
        or os.environ.get("VIDEO_RECAP_MATERIAL_LIBRARY_DIR")
        or None
    )


def _materials_enabled(args):
    return bool(_material_library_dir(args) and args.use_materials)


def _save_materials_enabled(args):
    return bool(_material_library_dir(args) and args.save_materials)


def _pause_for_agent(work_dir, need_text, cont, inspect_hint=None):
    brief = Path(work_dir) / "agent_narration_brief.md"
    print("=" * 50)
    if brief.exists() and "Research the story FIRST" in brief.read_text(
        encoding="utf-8"
    ):
        print(
            "[video-recap] ⚑ 理解素材偏薄：先按 brief 顶部「Research the story FIRST」调研并写 "
            "background_research.json，再写解说，避免看图说话。"
        )
    print(f"[video-recap] ⏸  阅读 {brief}（按 video-script 规则）后写入 {need_text}")
    if inspect_hint:
        print(f"[video-recap]    先核对状态/时间轴（建议性）: {inspect_hint}")
    print(f"[video-recap]    写完后重跑继续: {cont}")
    print("=" * 50)
