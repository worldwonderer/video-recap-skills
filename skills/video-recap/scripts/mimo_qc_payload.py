"""Build bounded semantic and multimodal MiMo QC payloads."""

from __future__ import annotations


import importlib.util

import json


from pathlib import Path

from typing import Any, Mapping, Sequence


from mimo_qc_evidence import (
    _cache_evidence,
    _fingerprint_value,
    _redact,
    safe_mimo_config,
)

_LOCAL_LIB_PATH = Path(__file__).with_name("lib.py")

_LOCAL_LIB_SPEC = importlib.util.spec_from_file_location(
    "video_recap_mimo_qc_lib", _LOCAL_LIB_PATH
)

if (
    _LOCAL_LIB_SPEC is None or _LOCAL_LIB_SPEC.loader is None
):  # pragma: no cover - import invariant
    raise ImportError(f"cannot load local MiMo QC client: {_LOCAL_LIB_PATH}")

_LOCAL_LIB = importlib.util.module_from_spec(_LOCAL_LIB_SPEC)

_LOCAL_LIB_SPEC.loader.exec_module(_LOCAL_LIB)

ARTIFACT_NAME = "mimo_qc.json"

DEFAULT_STAGE = "pre_assemble"

MAX_FRAMES = 6


def _semantic_evidence(evidence: Mapping[str, Any], *, stage: str) -> dict[str, Any]:
    """Keep real artifact values visible to MiMo without sending irrelevant paths.

    ``collect_evidence`` already bounds every artifact independently.  The additional
    summarization here therefore needs enough depth to retain the narration/ASR/TTS
    scalars nested inside those summaries.  A shallow second pass used to replace the
    values with fingerprints, which made live QC invent missing-script and failed-TTS
    findings from evidence it could no longer read.
    """
    semantic = dict(evidence)
    semantic.pop("work_dir", None)
    semantic.pop("fingerprint", None)
    semantic["evidence_roles"] = {
        "narration.json": (
            "Planned recap voiceover on the OUTPUT timeline; judge its factual and temporal "
            "fit rather than expecting verbatim source dialogue."
        ),
        "tts_meta.json": (
            "Synthesis and placement metadata for narration.json; an empty failures list means "
            "all requested narration segments synthesized successfully."
        ),
        "source_asr": (
            "Transcript of the SOURCE media audio, not a transcript of generated TTS. "
            "Wording differences from narration are expected; use this evidence for factual "
            "support and original-dialogue timing, not verbatim equality."
        ),
        "generated_subtitles": (
            "Recap subtitles derived from generated narration/TTS during assembly. They are not "
            "source ASR and must never be used as independent factual support. Visible text that "
            "matches one of these cues is the intended generated caption, including when it sits "
            "on the black source-subtitle mask band."
        ),
        "visual_metadata": (
            "Diagnostic storyboard/frame metadata. labels_burned refers only to diagnostic "
            "timestamp labels and does not mean labels were burned into the final video."
        ),
        "final_output": (
            "Only present for post_render and limited to candidates that actually exist."
        ),
        "post_render_frame_limits": (
            "Sampled final-output frames are silent still images: they contain no audible audio. "
            "Do not infer which audio track is playing from visible source captions. Narration/TTS "
            "timings are segment-level, not word-level, so an internal phrase cannot be assigned to "
            "an exact sampled-frame timestamp."
        ),
        "narration_and_subtitle_gaps": (
            "Generated narration/subtitle gaps may be deliberate original-audio blocks. In those "
            "gaps the source audio and its existing captions can intentionally return; do not call "
            "the absence of generated subtitles a defect without evidence of accidental silence."
        ),
        "actual_audio_timing": (
            "narration.json start/end values are authoring slots, not proof that speech fills the "
            "whole slot. For post-render timing, assembly_manifest audio_segments "
            "actual_place_start/actual_place_end are authoritative. A sampled frame after "
            "actual_place_end is in an original-audio gap."
        ),
    }
    if stage != "post_render":
        semantic.pop("final_output", None)
        # These files can be leftovers from an earlier assembly when narration is being revised.
        # Pre-assemble QC must evaluate current narration/TTS, not stale rendered captions.
        semantic.pop("generated_subtitles", None)
        semantic["evidence_roles"].pop("generated_subtitles", None)
        semantic["evidence_roles"].pop("post_render_frame_limits", None)
        semantic["evidence_roles"].pop("actual_audio_timing", None)
    else:
        final_output = semantic.get("final_output")
        candidates = (
            final_output.get("candidates", [])
            if isinstance(final_output, Mapping)
            else []
        )
        semantic["final_output"] = {
            "candidates": [
                dict(candidate)
                for candidate in candidates
                if isinstance(candidate, Mapping) and candidate.get("exists") is True
            ]
        }
    # Artifact summaries are already bounded at collection time; summarizing them again
    # would wrap their ``items`` arrays in another summary layer and obscure the values.
    return _redact(semantic)


def build_payload(
    evidence: Mapping[str, Any],
    *,
    stage: str = DEFAULT_STAGE,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the report-safe semantic payload (never contains image base64)."""
    cfg = safe_mimo_config(config)
    payload = {
        "stage": stage,
        "artifact": ARTIFACT_NAME,
        "model": cfg["model"],
        "config": cfg,
        "instructions": (
            "你是 video-recap 的建议性质量审阅器。结合解说、剪辑计划、字幕/ASR、TTS、"
            "组装元数据和抽样画面，指出语义或审美问题。只返回主观观察，不做自动修复，"
            "只根据可见的实际字段判断，不得从文件字节数、指纹、截断或省略标记推断内容缺失；"
            "source_asr 是源素材原声证据，不是生成后 TTS 的逐字转录；generated_subtitles 是本轮生成字幕，"
            "二者角色不可混淆，解说改写与源台词不一致本身不是问题；"
            "空 failures 表示没有失败，storyboard 的 labels_burned 仅表示诊断图上的时间标签，"
            "不代表标签烧进最终视频；pre_assemble 阶段尚无 final_output 属于正常状态；"
            "成片抽样画面是带时间戳的稀疏点样本，只能判断该具体时刻，不得用单帧否定整段内其他时刻的画面。"
            "这些抽样帧是无声静帧，不能从画面中的源字幕推断当时实际播放哪条音轨；"
            "源字幕遮罩策略为 off 时，保留的源字幕无需与解说逐字一致；解说/TTS 只有段级时间，"
            "没有词级对齐，不得把句中某个短语强行对应到抽样帧的精确秒数。"
            "生成解说和字幕之间的空档可能是刻意保留的原声块，此时原声和源字幕回归是正常设计，"
            "没有意外静音证据时不得把生成字幕空档当成缺失。只输出可采取行动的疑似问题，"
            "不要把正常、一致、相符或通过项作为 observation。不得提出阻断决定。"
            "narration.json 的 start/end 是创作槽位，不表示旁白铺满整段；成片阶段必须以 "
            "assembly_manifest.audio_segments 的 actual_place_start/actual_place_end 判断实际旁白。"
            "抽样点超过 actual_place_end 时属于原声块；一个字幕时间段含多个分句时，静帧匹配其中任一分句都不算错位。"
            "只有同一编号静帧中同时清晰可读两层不同字幕，才能报告字幕重叠；不同静帧分别出现解说字幕和源字幕不算重叠。"
            "遮罩 opacity=1.0 表示源字幕像素已被不透明覆盖，不得臆测遮罩下仍有可见文字。"
            "黑色遮罩带上与 generated_subtitles cue 一致的白字是本轮预期生成字幕，不是残留源字幕；"
            "必须先逐字对照 generated_subtitles，再判断是否另有第二层源字幕。"
            "每张抽样图都由同编号 BEGIN/END 文本包围，必须按编号和时间戳独立判断，"
            "不得把一张图里的文字或人物归到另一张图。"
            '返回 JSON：{"observations":[{"code":...,'
            '"message":...,"category":"semantic|aesthetic",'
            '"confidence":"low|medium|high","sample_policy":'
            '"semantic|aesthetic|sampled","evidence":{...}}]}。最多 12 条。'
        ),
        "evidence": _semantic_evidence(evidence, stage=stage),
        "evidence_fingerprint": evidence.get("fingerprint")
        or _fingerprint_value(_cache_evidence(evidence)),
    }
    payload["payload_fingerprint"] = _fingerprint_value(payload)
    return _redact(payload)


def _request_payload(
    payload: Mapping[str, Any], frame_samples: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    request_evidence = {
        "stage": payload["stage"],
        "instructions": payload["instructions"],
        "evidence": payload["evidence"],
    }
    content: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": json.dumps(
                request_evidence, ensure_ascii=False, separators=(",", ":")
            ),
        }
    ]
    for index, sample in enumerate(list(frame_samples)[:MAX_FRAMES], start=1):
        data_url = sample.get("data_url") if isinstance(sample, Mapping) else None
        if isinstance(data_url, str) and data_url.startswith("data:image/jpeg;base64,"):
            timestamp = sample.get("timestamp")
            try:
                timestamp_label = f"{float(timestamp):.3f}s"
            except (TypeError, ValueError):
                timestamp_label = "unknown"
            content.append(
                {
                    "type": "text",
                    "text": (
                        f"BEGIN QC_FRAME_{index}: final-output timestamp {timestamp_label}. "
                        f"Judge only this image and do not transfer its content to another frame."
                    ),
                }
            )
            content.append({"type": "image_url", "image_url": {"url": data_url}})
            content.append(
                {
                    "type": "text",
                    "text": f"END QC_FRAME_{index}: timestamp {timestamp_label}.",
                }
            )
    return {
        "model": payload["model"],
        "messages": [{"role": "user", "content": content}],
        "max_completion_tokens": 1600,
        "thinking": {"type": "disabled"},
    }


def _strip_json_fence(text: str) -> str:
    value = text.strip()
    if value.startswith("```") and value.endswith("```"):
        lines = value.splitlines()
        if len(lines) >= 3:
            value = "\n".join(lines[1:-1]).strip()
    return value


def _validated_live_output(response: Any) -> Any:
    try:
        content = response["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise ValueError("malformed_response") from None
    if isinstance(content, str):
        try:
            content = json.loads(_strip_json_fence(content))
        except (TypeError, ValueError):
            raise ValueError("malformed_json_content") from None
    if isinstance(content, list):
        return content
    if isinstance(content, Mapping) and isinstance(content.get("observations"), list):
        return content
    if isinstance(content, Mapping) and isinstance(content.get("findings"), list):
        return content
    raise ValueError("malformed_observations")
