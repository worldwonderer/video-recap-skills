import json
import re

from config import CONFIG
from common import log, run_cmd, api_call, get_video_duration, load_prompt, _parse_narration_json

# ── Step 3: ASR 转录 ──────────────────────────────────────────────────

def transcribe_audio(video_path, work_dir):
    """提取音频并使用 qwen3-asr-rs 转录，通过分段合成时间戳"""
    # 提取音频
    audio_wav = work_dir / "audio.wav"
    cmd = ["ffmpeg", "-y", "-i", str(video_path), "-vn",
           "-ar", "16000", "-ac", "1", str(audio_wav)]
    result = run_cmd(cmd)
    if result.returncode != 0:
        raise RuntimeError(f"音频提取失败: {result.stderr}")

    # 获取音频时长
    duration = get_video_duration(video_path)
    if duration == 0:
        duration = 180.0

    segments_dir = work_dir / "audio_segments"
    segments_dir.mkdir(exist_ok=True)

    if duration <= 180:
        # 短音频，整段转录
        text = _run_asr(audio_wav)
        asr_result = [{"start": 0.0, "end": round(duration, 2), "text": text}]
    else:
        # 长音频，分段转录
        asr_result = _segment_and_transcribe(audio_wav, segments_dir, duration)

    # 保存
    asr_file = work_dir / "asr_result.json"
    asr_file.write_text(json.dumps(asr_result, ensure_ascii=False, indent=2))

    total_text = " ".join(s["text"] for s in asr_result if s["text"])
    log(f"ASR 转录完成: {len(asr_result)} 段, 共 {len(total_text)} 字")
    return asr_result


def _run_asr(wav_path):
    """调用 qwen3-asr-rs 转录单个音频文件"""
    cmd = [CONFIG["asr_bin"], CONFIG["asr_model_dir"], str(wav_path)]
    result = run_cmd(cmd, timeout=600)
    if result.returncode != 0:
        log(f"ASR 警告: {result.stderr}")
        return ""

    # 解析输出 - 格式: Language : xxx\nText     : 实际文本
    text = ""
    for line in result.stdout.strip().split("\n"):
        m = re.match(r"Text\s*:\s*(.*)", line.strip())
        if m:
            text = m.group(1).strip()
            break

    return text


def _segment_and_transcribe(audio_wav, segments_dir, total_duration):
    """分段转录长音频"""
    segment_length = 180  # 3 分钟
    results = []

    for i, start in enumerate(range(0, int(total_duration), segment_length)):
        end = min(start + segment_length, total_duration)
        seg_wav = segments_dir / f"seg_{i:03d}.wav"

        cmd = ["ffmpeg", "-y", "-i", str(audio_wav),
               "-ss", str(start), "-to", str(end),
               "-ar", "16000", "-ac", "1", str(seg_wav)]
        run_cmd(cmd)

        text = _run_asr(seg_wav)
        results.append({
            "start": round(start, 2),
            "end": round(end, 2),
            "text": text,
        })
        log(f"  段 {i+1}: {start:.0f}s-{end:.0f}s => {len(text)} 字")

    return results



def _distribute_asr_by_scenes(asr_result, scenes_analysis):
    """将 ASR 文本按语义分配到场景（LLM 优先，按时长比例 fallback）"""
    if not asr_result:
        return ""

    full_text = " ".join(seg["text"] for seg in asr_result if seg["text"])
    if not full_text:
        return ""

    # 尝试 LLM 语义对齐
    align_prompt = load_prompt("ASR_ALIGN_PROMPT")
    if align_prompt and scenes_analysis:
        scenes_desc = ""
        for s in scenes_analysis:
            scenes_desc += f"- scene_id={s['scene_id']} ({s['start']:.1f}s-{s['end']:.1f}s): {s['description']}\n"

        payload = {
            "model": CONFIG["vlm_model"],
            "messages": [
                {"role": "user", "content": f"{align_prompt}\n\n场景列表：\n{scenes_desc.strip()}\n\nASR 转录文本：\n{full_text}"},
            ],
            "max_tokens": 1000,
        }

        try:
            resp = api_call(payload)
            msg = resp["choices"][0]["message"]
            result_text = msg.get("content", "") or msg.get("reasoning_content", "")
            alignments = _parse_narration_json(result_text)
            if isinstance(alignments, list) and alignments:
                lines = []
                for a in alignments:
                    sid = a.get("scene_id", 0)
                    text = a.get("text", "")
                    if text and 0 <= sid < len(scenes_analysis):
                        s = scenes_analysis[sid]
                        lines.append(f"[{s['start']:.0f}s-{s['end']:.0f}s] {text}")
                if lines:
                    log("ASR 语义对齐成功")
                    return "\n".join(lines)
        except Exception as e:
            log(f"ASR 语义对齐失败，使用时长比例分配: {e}")

    # Fallback: 按时长比例分配
    return _distribute_asr_by_time(asr_result, scenes_analysis)


def _distribute_asr_by_time(asr_result, scenes_analysis):
    """按时长比例分配 ASR 文本到场景（fallback）"""
    full_text = " ".join(seg["text"] for seg in asr_result if seg["text"])
    if not full_text:
        return ""

    total_duration = sum(s["end"] - s["start"] for s in scenes_analysis)
    if total_duration == 0:
        return full_text

    words = full_text.split()
    total_words = len(words)
    if total_words == 0:
        return ""

    lines = []
    word_offset = 0
    for s in scenes_analysis:
        duration = s["end"] - s["start"]
        n_words = max(1, round(total_words * duration / total_duration))
        end_offset = min(word_offset + n_words, total_words)
        segment_text = " ".join(words[word_offset:end_offset])
        if segment_text:
            lines.append(f"[{s['start']:.0f}s-{s['end']:.0f}s] {segment_text}")
        word_offset = end_offset

    if word_offset < total_words:
        remaining = " ".join(words[word_offset:])
        if lines:
            lines[-1] += " " + remaining
        else:
            lines.append(remaining)

    return "\n".join(lines)


def _annotate_asr_temporal(asr_result, scenes_analysis):
    """利用帧动作描述中的对白/字幕信息为 ASR 大块文本添加模糊时间标注"""

    # 从帧动作描述中收集带时间的锚点文本
    anchors = []  # [(timestamp, snippet_text)]
    for scene in scenes_analysis:
        facts = scene.get("frame_facts", {})
        for ts, actions in facts.items():
            t = float(ts)
            for action in actions:
                # 提取引号内的文本（对话/字幕）
                quoted = re.findall(r'[""“”\'\'「」](.+?)[""“”\'\'「」]', action)
                for q in quoted:
                    if len(q) >= 2:
                        anchors.append((t, q))
                # 提取"字幕:"后的文本
                sub_match = re.search(r'字幕[：:]\s*(.+)', action)
                if sub_match:
                    sub_text = sub_match.group(1).strip()
                    if len(sub_text) >= 2:
                        anchors.append((t, sub_text))

    if not anchors:
        log("ASR 时间标注: 无帧级字幕/对白锚点，回退到原有分配")
        return _distribute_asr_by_scenes(asr_result, scenes_analysis)

    # 按时间排序锚点
    anchors.sort(key=lambda x: x[0])

    # 对每个 ASR 块，尝试用锚点分割
    annotated_lines = []
    for seg in asr_result:
        text = seg.get("text", "").strip()
        if not text:
            continue
        seg_start = seg.get("start", 0)
        seg_end = seg.get("end", 0)

        # 找属于该时间段的锚点
        seg_anchors = [(t, s) for t, s in anchors if seg_start <= t <= seg_end]

        if not seg_anchors:
            # 无锚点，整体标注
            annotated_lines.append(f"[~{seg_start:.0f}s-{seg_end:.0f}s] {text}")
            continue

        # 尝试按锚点分割文本
        chunks = []  # [(approx_start, approx_end, text_chunk)]
        remaining = text
        prev_time = seg_start

        for anchor_time, anchor_text in seg_anchors:
            # 在剩余文本中查找锚点文本
            # 模糊匹配：取锚点文本的前3-4个字
            key = anchor_text[:min(4, len(anchor_text))]
            idx = remaining.find(key)
            if idx >= 0:
                # 找到锚点，分割
                before = remaining[:idx].strip()
                if before:
                    chunks.append((prev_time, anchor_time, before))
                remaining = remaining[idx:].strip()
                prev_time = anchor_time

        # 最后的剩余文本
        if remaining:
            chunks.append((prev_time, seg_end, remaining))

        if chunks:
            for cs, ce, ct in chunks:
                annotated_lines.append(f"[~{cs:.0f}s-{ce:.0f}s] {ct}")
        else:
            annotated_lines.append(f"[~{seg_start:.0f}s-{seg_end:.0f}s] {text}")

    result = "\n".join(annotated_lines)
    log(f"ASR 时间标注完成: {len(annotated_lines)} 个子段, {len(anchors)} 个锚点")
    return result


