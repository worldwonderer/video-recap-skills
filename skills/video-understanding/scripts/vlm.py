import base64
import json
import mimetypes
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

from lib import CONFIG
from lib import log, api_call, load_prompt, mimo_video_api_call, run_cmd

# ── Step 4: VLM 视觉分析 ─────────────────────────────────────────────

def _parse_vlm_depth_response(raw_text):
    """解析 VLM 深度分析响应，提取【描述】、【帧标签】和【深层分析】"""
    if not raw_text or not raw_text.strip():
        return "(VLM 无法识别此场景画面)", "", {}

    # 提取【描述】
    desc_match = re.search(r'【描述】\s*\n?(.*?)(?=【帧标签】|【深层分析】|$)', raw_text, re.DOTALL)
    if desc_match:
        description = desc_match.group(1).strip()
    else:
        description = raw_text.strip()

    # 提取【帧标签】
    frame_facts = {}
    facts_match = re.search(r'【帧标签】\s*\n?(.*?)(?=【深层分析】|$)', raw_text, re.DOTALL)
    if facts_match:
        for line in facts_match.group(1).strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            # 格式: "12.0s | 男子拿起茶壶对嘴喝, 满脸疲惫"
            m = re.match(r'([\d.]+)\s*s?\s*\|\s*(.+)', line)
            if m:
                ts = m.group(1)
                actions = [a.strip() for a in m.group(2).split(",") if a.strip()]
                if actions:
                    frame_facts[ts] = actions

    # 提取【深层分析】
    depth_match = re.search(r'【深层分析】\s*\n?(.*?)$', raw_text, re.DOTALL)
    depth_analysis = depth_match.group(1).strip() if depth_match else ""

    if not description:
        description = "(VLM 无法识别此场景画面)"

    return description, depth_analysis, frame_facts


def analyze_scenes(scenes, frames, work_dir):
    """对每个场景的关键帧调用 VLM 进行视觉分析（并行）"""
    if not scenes:
        analyses = []
        vlm_file = work_dir / "vlm_analysis.json"
        vlm_file.write_text(json.dumps(analyses, ensure_ascii=False, indent=2))
        log("VLM 分析完成: 0 个场景")
        return analyses

    if not frames:
        raise RuntimeError("VLM 分析需要先提取至少一帧；frames 为空")

    fps = CONFIG["fps"]
    if fps <= 0:
        raise ValueError("CONFIG['fps'] 必须大于 0；请先运行完整 pipeline 或指定 --fps")

    vlm_prompt = load_prompt("VLM_DEPTH_PROMPT")
    if not vlm_prompt:
        vlm_prompt = "仔细观察这些视频帧。分两部分输出：\n【描述】不超过80字，描述画面中正在发生什么。\n【深层分析】不超过120字，分析角色情绪、关系动态、潜台词。"

    ctx = CONFIG.get("context_info", "")
    if ctx:
        vlm_prompt = f"已知信息：{ctx}\n\n{vlm_prompt}"

    # 构建帧时间映射 (frame_NNNNN.jpg -> time in seconds)
    frame_times = {}
    for f in frames:
        parts = f.stem.split("_")
        if len(parts) != 2 or not parts[1].isdigit():
            continue
        num = int(parts[1])
        t = num / fps
        frame_times[f] = t

    # base64 编码缓存
    b64_cache = {}

    def _get_b64(frame_path):
        if frame_path not in b64_cache:
            b64_cache[frame_path] = base64.b64encode(frame_path.read_bytes()).decode()
        return b64_cache[frame_path]

    def _analyze_single_scene(i, scene):
        """分析单个场景，返回 (scene_id, result_dict)"""
        scene_frames = [f for f, t in frame_times.items()
                        if scene["start"] <= t <= scene["end"]]
        if not scene_frames:
            mid = (scene["start"] + scene["end"]) / 2
            scene_frames = [min(frames, key=lambda f: abs(frame_times.get(f, 999) - mid))]

        duration = scene["end"] - scene["start"]
        if duration > 8:
            max_frames = min(6, max(3, int(duration / 3)))
        else:
            max_frames = 3

        if len(scene_frames) > max_frames:
            step = len(scene_frames) / max_frames
            scene_frames = [scene_frames[int(j * step)] for j in range(max_frames)]
        else:
            scene_frames = scene_frames[:max_frames]

        content_parts = []
        for f in scene_frames:
            b64 = _get_b64(f)
            content_parts.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{b64}"}
            })

        # 帧级事实标签：将帧时间注入prompt
        frame_ts_list = [f"{frame_times[f]:.1f}s" for f in scene_frames]
        frame_ts_text = "帧时间点: " + ", ".join(frame_ts_list)
        content_parts.append({"type": "text", "text": frame_ts_text + "\n\n" + vlm_prompt})

        payload = {
            "model": CONFIG["vlm_model"],
            "messages": [{"role": "user", "content": content_parts}],
            "max_tokens": 800,
        }

        log(f"VLM 分析场景 {i+1}/{len(scenes)} ({len(scene_frames)} 帧)...")

        raw_response = ""
        for attempt in range(3):
            resp = api_call(payload)
            try:
                msg = resp["choices"][0]["message"]
                raw_response = (msg.get("content") or msg.get("reasoning_content") or "")
            except (KeyError, IndexError):
                log(f"VLM 返回异常: {json.dumps(resp, ensure_ascii=False)[:200]}")

            if raw_response.strip():
                break

            if attempt < 2:
                log(f"  场景 {i+1} VLM 返回空，重试 ({attempt+2}/3)...")
                retry_parts = content_parts[:-1]
                retry_parts.append({"type": "text", "text": frame_ts_text + "\n\n" + vlm_prompt + "\n请务必按格式输出，不要留空。"})
                payload = {
                    "model": CONFIG["vlm_model"],
                    "messages": [{"role": "user", "content": retry_parts}],
                    "max_tokens": 800,
                }

        # 解析 【描述】、【帧标签】和【深层分析】
        description, depth_analysis, frame_facts = _parse_vlm_depth_response(raw_response)

        result = {
            "scene_id": i,
            "start": scene["start"],
            "end": scene["end"],
            "description": description,
            "depth_analysis": depth_analysis,
        }
        if frame_facts:
            result["frame_facts"] = frame_facts

        return i, result

    # 并行调用 VLM
    analyses = [None] * len(scenes)
    max_workers = min(len(scenes), CONFIG.get("vlm_workers", 4))
    log(f"VLM 并行分析 {len(scenes)} 个场景 (workers={max_workers})...")

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_analyze_single_scene, i, s): i for i, s in enumerate(scenes)}
        for future in as_completed(futures):
            try:
                idx, result = future.result()
                analyses[idx] = result
            except Exception as e:
                i = futures[future]
                log(f"VLM 场景 {i+1} 分析失败: {e}")
                analyses[i] = {
                    "scene_id": i, "start": scenes[i]["start"], "end": scenes[i]["end"],
                    "description": f"(VLM 分析失败: {e})", "depth_analysis": "", "frame_facts": {},
                }

    # 保存
    vlm_file = work_dir / "vlm_analysis.json"
    vlm_file.write_text(json.dumps(analyses, ensure_ascii=False, indent=2))

    log(f"VLM 分析完成: {len(analyses)} 个场景")
    return analyses


def _video_data_url(video_path):
    """Return a MiMo-compatible data URL for a local video chunk, or None when too large."""
    max_bytes = int(float(CONFIG.get("mimo_video_base64_max_mb", 45.0)) * 1024 * 1024)
    encoded_size = int(video_path.stat().st_size * 4 / 3) + 128
    if encoded_size > max_bytes:
        log(
            "MiMo 视频分片超过 base64 上限: "
            f"编码后约 {encoded_size / 1024 / 1024:.1f}MB，超过限制 {max_bytes / 1024 / 1024:.1f}MB；"
            "请降低 MIMO_VIDEO_CHUNK_MAX_SECONDS 或 MIMO_VIDEO_FPS"
        )
        return None
    mime_type = mimetypes.guess_type(str(video_path))[0] or "video/mp4"
    encoded = base64.b64encode(video_path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _mimo_video_chunks(scenes):
    """Build local MiMo video-understanding chunks from ffmpeg scene boundaries."""
    if not scenes:
        raise RuntimeError("MiMo 视频分片理解需要 scenes；请先运行 ffmpeg scene/scdet 场景检测")

    max_seconds = float(CONFIG.get("mimo_video_chunk_max_seconds", 20.0) or 20.0)
    min_seconds = float(CONFIG.get("mimo_video_chunk_min_seconds", 1.0) or 1.0)
    chunks = []
    for scene_index, scene in enumerate(scenes):
        try:
            start = float(scene.get("start", 0.0))
            end = float(scene.get("end", start))
        except (TypeError, ValueError, AttributeError):
            continue
        if end <= start:
            continue
        scene_id = scene.get("scene_id", scene_index) if isinstance(scene, dict) else scene_index
        cursor = start
        while cursor < end:
            chunk_end = min(end, cursor + max_seconds)
            if end - chunk_end < min_seconds and chunk_end < end:
                chunk_end = end
            if chunk_end > cursor:
                chunks.append({
                    "chunk_id": len(chunks),
                    "scene_id": scene_id,
                    "start": round(cursor, 3),
                    "end": round(chunk_end, 3),
                })
            cursor = chunk_end
    if not chunks:
        raise RuntimeError("MiMo 视频分片理解没有可用分片；请检查 scenes.json")
    return chunks


def _extract_video_chunk(video_path, chunk, output_path):
    """Cut one scene-based chunk into a compact local MP4 for MiMo video_url data URL."""
    start = float(chunk["start"])
    duration = max(0.1, float(chunk["end"]) - start)
    fps = float(CONFIG.get("mimo_video_fps", 2.0) or 2.0)
    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{start:.3f}",
        "-t", f"{duration:.3f}",
        "-i", str(video_path),
        "-map", "0:v:0",
        "-an",
        "-vf", f"fps={fps:g}",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "30",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(output_path),
    ]
    result = run_cmd(cmd, timeout=CONFIG.get("mimo_video_chunk_timeout", 180))
    if result.returncode != 0:
        raise RuntimeError(f"MiMo 视频分片裁剪失败: {result.stderr[-500:]}")
    return output_path


def _mimo_chunk_prompt(chunk):
    return (
        f"这是原视频 {chunk['start']:.1f}s-{chunk['end']:.1f}s 的场景分片，"
        f"scene_id={chunk['scene_id']}。"
        f"{CONFIG.get('mimo_video_prompt', '请用中文概括这个视频分片。')}"
    )


def _mimo_video_model():
    """MiMo 视频理解使用的模型：优先 mimo_video_model，回退 mimo_model，再回退 vlm_model。"""
    return CONFIG.get("mimo_video_model") or CONFIG.get("mimo_model") or CONFIG["vlm_model"]


def mimo_video_settings_fingerprint():
    """Return non-secret MiMo video-overview settings that affect generated content."""
    return {
        "model": _mimo_video_model(),
        "mimo_video_fps": CONFIG.get("mimo_video_fps", 2.0),
        "mimo_media_resolution": CONFIG.get("mimo_media_resolution", "default"),
        "mimo_video_chunk_max_seconds": CONFIG.get("mimo_video_chunk_max_seconds", 20.0),
        "mimo_video_chunk_min_seconds": CONFIG.get("mimo_video_chunk_min_seconds", 1.0),
        "mimo_video_base64_max_mb": CONFIG.get("mimo_video_base64_max_mb", 45.0),
        "mimo_video_prompt": CONFIG.get("mimo_video_prompt", ""),
        "mimo_disable_thinking": CONFIG.get("mimo_disable_thinking", True),
    }


def _mimo_chunk_cache_key(chunk):
    """Stable identifier for a MiMo chunk (index + scene span) for partial-cache reuse."""
    return (
        f"{chunk['chunk_id']}|{chunk['scene_id']}|"
        f"{float(chunk['start']):.3f}-{float(chunk['end']):.3f}"
    )


def _load_mimo_partial(partial_path):
    """Load the internal partial chunk cache, keyed by chunk identifier.

    Returns {} when missing/unreadable or when the settings fingerprint differs,
    so a settings change invalidates already-cached chunks.
    """
    if not partial_path.exists():
        return {}
    try:
        partial = json.loads(partial_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(partial, dict):
        return {}
    if partial.get("settings") != mimo_video_settings_fingerprint():
        return {}
    done = partial.get("chunks")
    if not isinstance(done, dict):
        return {}
    return done


def _save_mimo_partial(partial_path, done):
    """Persist completed chunk results incrementally so paid chunks survive a mid-loop failure."""
    payload = {
        "settings": mimo_video_settings_fingerprint(),
        "chunks": done,
    }
    partial_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _analyze_mimo_video_chunk(chunk_path, chunk):
    video_url = _video_data_url(chunk_path)
    if not video_url:
        raise RuntimeError(f"MiMo 视频分片 {chunk['chunk_id'] + 1} 超过 data URL 上限")

    content_parts = [
        {
            "type": "video_url",
            "video_url": {"url": video_url},
            "fps": CONFIG.get("mimo_video_fps", 2.0),
            "media_resolution": CONFIG.get("mimo_media_resolution", "default"),
        },
        {"type": "text", "text": _mimo_chunk_prompt(chunk)},
    ]
    model = _mimo_video_model()
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": content_parts}],
        "max_tokens": 1200,
    }
    resp = mimo_video_api_call(payload)
    try:
        msg = resp["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("MiMo 视频分片理解响应缺少 choices[0].message") from exc
    return {
        "chunk_id": chunk["chunk_id"],
        "scene_id": chunk["scene_id"],
        "start": chunk["start"],
        "end": chunk["end"],
        "model": resp.get("model", model),
        "content": msg.get("content", ""),
        "reasoning_content": msg.get("reasoning_content", ""),
        "usage": resp.get("usage", {}),
        "clip_path": f"mimo_video_chunks/{chunk_path.name}",
    }


_MIMO_REJECTION_MARKERS = (
    "request was rejected", "considered high risk", "high risk",
    "content policy", "cannot process", "无法处理", "内容审核", "违规",
)


def _is_mimo_chunk_usable(content):
    """A chunk is usable only if MiMo returned real analysis (not empty / a moderation refusal)."""
    text = str(content or "").strip()
    if not text:
        return False
    low = text.lower()
    return not any(marker in low for marker in _MIMO_REJECTION_MARKERS)


def analyze_video_overview(video_path, work_dir, scenes=None):
    """Use MiMo video understanding over local ffmpeg scene chunks."""
    if not CONFIG.get("mimo_video_overview", False):
        return None
    if not CONFIG.get("mimo_video_api_key"):
        log("MiMo 视频概览已启用，但未设置 MIMO_VIDEO_API_KEY/MIMO_API_KEY，跳过")
        return None

    chunks = _mimo_video_chunks(scenes)
    chunks_dir = work_dir / "mimo_video_chunks"
    chunks_dir.mkdir(exist_ok=True)

    # 增量缓存：已完成的分片先落盘，避免一段失败时丢弃所有已付费分片
    partial_path = work_dir / "mimo_video_overview.partial.json"
    done = _load_mimo_partial(partial_path)

    log(f"MiMo 视频理解：按 ffmpeg scene 分片分析 {len(chunks)} 段...")
    chunk_results = []
    for chunk in chunks:
        cache_key = _mimo_chunk_cache_key(chunk)
        cached = done.get(cache_key)
        if cached is not None:
            log(
                f"  MiMo 分片 {chunk['chunk_id'] + 1}/{len(chunks)}: "
                f"{chunk['start']:.1f}-{chunk['end']:.1f}s（命中增量缓存，跳过）"
            )
            chunk_results.append(cached)
            continue
        chunk_path = chunks_dir / (
            f"chunk_{chunk['chunk_id']:03d}_scene_{chunk['scene_id']}_"
            f"{chunk['start']:.2f}-{chunk['end']:.2f}.mp4"
        )
        _extract_video_chunk(video_path, chunk, chunk_path)
        log(
            f"  MiMo 分片 {chunk['chunk_id'] + 1}/{len(chunks)}: "
            f"{chunk['start']:.1f}-{chunk['end']:.1f}s"
        )
        chunk_result = _analyze_mimo_video_chunk(chunk_path, chunk)
        chunk_results.append(chunk_result)
        done[cache_key] = chunk_result
        _save_mimo_partial(partial_path, done)

    usable = sum(1 for it in chunk_results if _is_mimo_chunk_usable(it.get("content")))
    if usable == 0:
        log(f"MiMo 视频概览：{len(chunk_results)} 段均无有效内容（疑似被内容审核拦截），跳过概览")
        try:
            partial_path.unlink()
        except OSError:
            pass
        return None

    content = "\n\n".join(
        f"### 分片 {item['chunk_id'] + 1} "
        f"(scene {item['scene_id']}, {item['start']:.1f}-{item['end']:.1f}s)\n"
        f"{item['content'].strip() if _is_mimo_chunk_usable(item.get('content')) else '(MiMo 未返回内容)'}"
        for item in chunk_results
    )

    overview = {
        "model": _mimo_video_model(),
        "content": content,
        "chunks": chunk_results,
        "chunk_count": len(chunk_results),
        "fps": CONFIG.get("mimo_video_fps", 2.0),
        "media_resolution": CONFIG.get("mimo_media_resolution", "default"),
        "input": "scene_chunks",
        "chunk_max_seconds": CONFIG.get("mimo_video_chunk_max_seconds", 20.0),
        "settings": mimo_video_settings_fingerprint(),
    }
    overview_path = work_dir / "mimo_video_overview.json"
    overview_path.write_text(json.dumps(overview, ensure_ascii=False, indent=2), encoding="utf-8")
    # 所有分片完成后清理增量缓存，保持 work_dir 仅有规范产物
    try:
        partial_path.unlink()
    except OSError:
        pass
    log(f"MiMo 分片视频概览完成: {overview_path}")
    return overview
