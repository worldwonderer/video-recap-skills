import base64
import json
import os
import re
import time
from pathlib import Path

from lib import CONFIG
from lib import log, run_cmd, get_video_duration, mimo_asr_api_call, file_fingerprint

# ── Step 3: ASR 转录（MiMo mimo-v2.5-asr，云端 API）────────────────────────

_ASR_AUDIO_MIME = "audio/wav"


def _load_name_glossary(work_dir):
    """从 background_research.json 收集已知人名（characters 键 + character_details 键及别名）。

    返回去重后、长度 >=2 的人名列表（按长度降序，长名优先匹配）。文件缺失或无名字时返回 []。
    """
    research_path = Path(work_dir) / "background_research.json"
    if not research_path.exists():
        return []
    try:
        data = json.loads(research_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []

    names = set()
    characters = data.get("characters")
    if isinstance(characters, dict):
        names.update(characters.keys())
    details = data.get("character_details")
    if isinstance(details, dict):
        for key, val in details.items():
            names.add(key)
            if isinstance(val, dict):
                aliases = val.get("aliases")
                if isinstance(aliases, list):
                    names.update(a for a in aliases if isinstance(a, str))

    cleaned = {n for n in names if isinstance(n, str) and len(n) >= 2}
    return sorted(cleaned, key=len, reverse=True)


def _correct_text_with_glossary(text, names):
    """用人名表修正 ASR 同音字错误（如 叶青眉 → 叶轻眉）。

    对每个已知人名，扫描文本中所有等长窗口；若某窗口与人名恰好相差一个字符（仅一处不同），
    则替换为该人名。严格约束为「恰好一字之差」，避免过度纠正（叶轻风 与 叶轻眉 也是一字之差，
    但这正是限制为单字替换的边界——只有当窗口本身不是任何已知人名时才会被改写）。
    """
    if not text or not names:
        return text
    name_set = set(names)
    for name in names:
        n = len(name)
        if n < 2 or len(text) < n:
            continue
        i = 0
        while i <= len(text) - n:
            window = text[i:i + n]
            # Only rewrite a one-char-off window when it is NOT itself a known name —
            # otherwise a distinct real name one char away (叶轻风 vs 叶轻眉) would be corrupted.
            if window != name and window not in name_set and _one_char_diff(window, name):
                text = text[:i] + name + text[i + n:]
                i += n
            else:
                i += 1
    return text


def _one_char_diff(a, b):
    """两个等长字符串是否恰好相差一个字符（仅一处不同）。"""
    if len(a) != len(b):
        return False
    diff = 0
    for ca, cb in zip(a, b):
        if ca != cb:
            diff += 1
            if diff > 1:
                return False
    return diff == 1


def _apply_glossary_corrections(segments, work_dir):
    """对已转录的 segments 就地应用人名表修正；无人名表时为 no-op。"""
    names = _load_name_glossary(work_dir)
    if not names:
        return segments
    for seg in segments:
        original = seg.get("text") or ""
        corrected = _correct_text_with_glossary(original, names)
        if corrected != original:
            seg["text"] = corrected
    return segments


def _audio_meta_path(work_dir):
    return Path(work_dir) / "audio.wav.meta.json"


def _write_audio_meta(work_dir, video_path):
    _audio_meta_path(work_dir).write_text(
        json.dumps({
            "schema_version": 1,
            "source_video_fingerprint": file_fingerprint(video_path),
            "audio": "audio.wav",
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def transcribe_audio(video_path, work_dir):
    """提取音频并用 MiMo ASR 分段转录，通过分段合成时间戳。"""
    asr_file = work_dir / "asr_result.json"

    if not CONFIG.get("mimo_asr_api_key"):
        key_name = CONFIG.get("mimo_asr_api_key_source", "MIMO_API_KEY")
        log(f"ASR 跳过：未设置 {key_name}（MiMo ASR 需要；VLM/TTS 也需要同一个 key）。"
            f"如不需要对白可加 --skip-asr")
        asr_file.write_text(json.dumps([], ensure_ascii=False, indent=2), encoding="utf-8")
        return []

    # 提取音频
    audio_wav = work_dir / "audio.wav"
    cmd = ["ffmpeg", "-y", "-i", str(video_path), "-vn",
           "-ar", "16000", "-ac", "1", str(audio_wav)]
    result = run_cmd(cmd)
    if result.returncode != 0:
        raise RuntimeError(f"音频提取失败: {result.stderr}")
    _write_audio_meta(work_dir, video_path)

    # 获取音频时长
    duration = get_video_duration(video_path)
    if duration <= 0:
        # ffprobe 失败时不再伪造 180s 时长，否则会向 asr_result.json 写入虚构时间戳
        log("ASR 警告: 无法获取音频时长（ffprobe 失败），跳过 ASR 转录")
        asr_file.write_text(json.dumps([], ensure_ascii=False, indent=2), encoding="utf-8")
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

    # 用 background_research.json 的人名表修正 ASR 同音字错误（如 叶青眉 → 叶轻眉）；无人名表时为 no-op
    _apply_glossary_corrections(asr_result, work_dir)

    # 保存
    asr_file.write_text(json.dumps(asr_result, ensure_ascii=False, indent=2), encoding="utf-8")

    total_text = " ".join(s["text"] for s in asr_result if s["text"])
    empty = sum(1 for s in asr_result if not s["text"])
    suffix = f"（{empty} 段无文本：静音/切分失败/超限被跳过）" if empty else ""
    log(f"ASR 转录完成: {len(asr_result)} 段, 共 {len(total_text)} 字{suffix}")
    return asr_result


def _strip_reasoning_residue(text):
    """Remove MiMo reasoning-model <think>…</think> leakage from ASR content.

    Thinking-disable is not applied to -asr models (lib._prepare_api_payload), so the reasoning
    model can leak a <think> block — full, truncated/unclosed, or a leading orphan/residual tag
    (a bare "think>" prefix) — into the transcript. Independent copy of the same strip in
    video-voiceover/scripts/dub.py (skills share no code); keep the two in sync.
    """
    text = re.sub(r"(?is)<think\b.*?</think\s*>", "", text)  # full <think>…</think> block
    text = re.sub(r"(?is)<think\b.*\Z", "", text)            # unclosed/truncated <think tail
    text = re.sub(r"(?i)^\s*<?/?think\s*>\s*", "", text)     # leading orphan/residual think tag
    return text


def _run_asr(wav_path):
    """用 MiMo ASR (mimo-v2.5-asr) 转录单个 wav 文件，返回纯文本。

    音频以 base64 data-URI 放进 OpenAI 风格的 chat/completions 消息里，转写文本回到
    choices[0].message.content。API/响应结构失败会抛错，避免把瞬时失败缓存成空转写；
    只有无音频、超体积等确定不可发送的片段返回空串。
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
        raise RuntimeError(f"MiMo ASR 调用失败: {e}") from e
    try:
        return _strip_reasoning_residue(str(resp["choices"][0]["message"]["content"] or "")).strip()
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(f"MiMo ASR 返回结构异常: {json.dumps(resp, ensure_ascii=False)[:200]}")


def _segment_and_transcribe(audio_wav, segments_dir, total_duration, segment_length=None):
    """分段转录长音频"""
    if segment_length is None:
        segment_length = max(5, int(CONFIG.get("asr_segment_seconds", 30) or 30))
    # 长视频 ASR 是顺序调用；可选节流让调用间隔开，降低踩到集群限流的频率（默认 0=不节流）
    try:
        throttle = max(0.0, float(os.environ.get("ASR_THROTTLE_SECONDS", "0") or 0))
    except ValueError:
        throttle = 0.0
    results = []

    for i, start in enumerate(range(0, int(total_duration), segment_length)):
        if throttle and i:
            time.sleep(throttle)
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
