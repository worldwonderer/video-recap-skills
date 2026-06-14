import base64
import json
from pathlib import Path

from lib import CONFIG
from lib import log, run_cmd, get_video_duration, mimo_asr_api_call

# ── Step 3: ASR 转录（MiMo mimo-v2.5-asr，云端 API）────────────────────────

_ASR_AUDIO_MIME = "audio/wav"


def transcribe_audio(video_path, work_dir):
    """提取音频并用 MiMo ASR 分段转录，通过分段合成时间戳。"""
    asr_file = work_dir / "asr_result.json"

    if not CONFIG.get("mimo_asr_api_key"):
        key_name = CONFIG.get("mimo_asr_api_key_source", "MIMO_API_KEY")
        log(f"ASR 跳过：未设置 {key_name}（MiMo ASR 需要）。如不需要对白可加 --skip-asr")
        asr_file.write_text(json.dumps([], ensure_ascii=False, indent=2))
        return []

    # 提取音频
    audio_wav = work_dir / "audio.wav"
    cmd = ["ffmpeg", "-y", "-i", str(video_path), "-vn",
           "-ar", "16000", "-ac", "1", str(audio_wav)]
    result = run_cmd(cmd)
    if result.returncode != 0:
        raise RuntimeError(f"音频提取失败: {result.stderr}")

    # 获取音频时长
    duration = get_video_duration(video_path)
    if duration <= 0:
        # ffprobe 失败时不再伪造 180s 时长，否则会向 asr_result.json 写入虚构时间戳
        log("ASR 警告: 无法获取音频时长（ffprobe 失败），跳过 ASR 转录")
        asr_file.write_text(json.dumps([], ensure_ascii=False, indent=2))
        return []

    segments_dir = work_dir / "audio_segments"
    segments_dir.mkdir(exist_ok=True)

    segment_length = max(5, int(CONFIG.get("asr_segment_seconds", 30) or 30))
    if duration <= segment_length:
        # 短音频，整段转录
        text = _run_asr(audio_wav)
        asr_result = [{"start": 0.0, "end": round(duration, 2), "text": text}]
    else:
        # 长音频，分段转录（更细的窗口 → 更精细的对白时间戳）
        asr_result = _segment_and_transcribe(audio_wav, segments_dir, duration, segment_length)

    # 保存
    asr_file.write_text(json.dumps(asr_result, ensure_ascii=False, indent=2))

    total_text = " ".join(s["text"] for s in asr_result if s["text"])
    log(f"ASR 转录完成: {len(asr_result)} 段, 共 {len(total_text)} 字")
    return asr_result


def _run_asr(wav_path):
    """用 MiMo ASR (mimo-v2.5-asr) 转录单个 wav 文件，返回纯文本。

    音频以 base64 data-URI 放进 OpenAI 风格的 chat/completions 消息里，转写文本回到
    choices[0].message.content。单段失败只记日志返回空串（保留其它段），与本地 ASR 旧行为一致。
    """
    try:
        raw = Path(wav_path).read_bytes()
    except OSError as e:
        log(f"ASR 警告: 无法读取音频 {wav_path}: {e}")
        return ""
    if not raw:
        return ""

    b64 = base64.b64encode(raw).decode("ascii")
    max_b64_bytes = int(float(CONFIG.get("mimo_asr_base64_max_mb", 10.0)) * 1024 * 1024)
    if len(b64) > max_b64_bytes:
        log(f"ASR 警告: 分片 base64 体积 {len(b64) / 1024 / 1024:.1f}MB 超过 MiMo 上限 "
            f"{CONFIG.get('mimo_asr_base64_max_mb')}MB，跳过该段；可调小 ASR_SEGMENT_SECONDS")
        return ""

    payload = {
        "model": CONFIG.get("mimo_asr_model", "mimo-v2.5-asr"),
        "messages": [{
            "role": "user",
            "content": [{
                "type": "input_audio",
                "input_audio": {"data": f"data:{_ASR_AUDIO_MIME};base64,{b64}"},
            }],
        }],
        "asr_options": {"language": CONFIG.get("mimo_asr_language", "auto")},
    }
    try:
        resp = mimo_asr_api_call(payload)
    except Exception as e:
        log(f"ASR 警告: MiMo ASR 调用失败: {e}")
        return ""
    try:
        return str(resp["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, TypeError):
        log(f"ASR 警告: MiMo ASR 返回结构异常: {json.dumps(resp, ensure_ascii=False)[:200]}")
        return ""


def _segment_and_transcribe(audio_wav, segments_dir, total_duration, segment_length=None):
    """分段转录长音频"""
    if segment_length is None:
        segment_length = max(5, int(CONFIG.get("asr_segment_seconds", 30) or 30))
    results = []

    for i, start in enumerate(range(0, int(total_duration), segment_length)):
        end = min(start + segment_length, total_duration)
        seg_wav = segments_dir / f"seg_{i:03d}.wav"

        cmd = ["ffmpeg", "-y", "-i", str(audio_wav),
               "-ss", str(start), "-to", str(end),
               "-ar", "16000", "-ac", "1", str(seg_wav)]
        cut = run_cmd(cmd)
        if cut.returncode != 0:
            # 切分失败时不要对磁盘上的陈旧/残缺音频转录，否则会得到错位文本
            log(f"  段 {i+1}: 切分失败，跳过转录 ({cut.stderr.strip()[:200]})")
            text = ""
        else:
            text = _run_asr(seg_wav)
        results.append({
            "start": round(start, 2),
            "end": round(end, 2),
            "text": text,
        })
        log(f"  段 {i+1}: {start:.0f}s-{end:.0f}s => {len(text)} 字")

    return results
