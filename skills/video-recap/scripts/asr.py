import json
import re

from config import CONFIG
from common import log, run_cmd, get_video_duration

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
