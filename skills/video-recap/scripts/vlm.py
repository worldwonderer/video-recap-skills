import base64
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

from config import CONFIG
from common import log, api_call, load_prompt

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
                raw_response = msg.get("content", "") or msg.get("reasoning_content", "")
            except (KeyError, IndexError):
                log(f"VLM 返回异常: {json.dumps(resp, ensure_ascii=False)[:200]}")

            if raw_response.strip():
                break

            if attempt < 2:
                log(f"  场景 {i+1} VLM 返回空，重试 ({attempt+2}/3)...")
                retry_parts = content_parts[:-1]
                retry_parts.append({"type": "text", "text": vlm_prompt + "\n请务必按格式输出，不要留空。"})
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




# ── Step 4.5: 叙事结构分析 ───────────────────────────────────────────

def analyze_narrative_structure(scenes_analysis, work_dir):
    """分析视频的叙事结构，为每个场景标注 narrative_role 和 info_weight"""
    analysis_prompt = load_prompt("NARRATIVE_ANALYSIS_PROMPT")
    if not analysis_prompt:
        return scenes_analysis

    scenes_text = ""
    for s in scenes_analysis:
        scenes_text += f"- scene_id={s['scene_id']} ({s['start']:.1f}s-{s['end']:.1f}s): {s['description']}\n"

    payload = {
        "model": CONFIG.get("llm_model", CONFIG["vlm_model"]),
        "messages": [
            {"role": "user", "content": f"{analysis_prompt}\n\n场景列表：\n{scenes_text.strip()}"},
        ],
        "max_tokens": 1000,
    }

    log("分析叙事结构 (Step 4.5)...")
    resp = api_call(payload)

    try:
        msg = resp["choices"][0]["message"]
        result_text = msg.get("content", "") or msg.get("reasoning_content", "")
    except (KeyError, IndexError):
        log("叙事结构分析失败，使用默认值")
        return scenes_analysis

    # 解析 JSON
    roles = []
    try:
        roles = json.loads(result_text)
    except json.JSONDecodeError:
        for pattern in [r"```json\s*(.*?)\s*```", r"(\[.*\])"]:
            m = re.search(pattern, result_text, re.DOTALL)
            if m:
                try:
                    roles = json.loads(m.group(1))
                    break
                except json.JSONDecodeError:
                    continue

    if not isinstance(roles, list):
        log("叙事结构解析失败，使用默认值")
        return scenes_analysis

    # 将 narrative_role 和 info_weight 合并到 scenes_analysis
    role_map = {r.get("scene_id"): r for r in roles if isinstance(r, dict)}
    for s in scenes_analysis:
        sid = s["scene_id"]
        if sid in role_map:
            s["narrative_role"] = role_map[sid].get("narrative_role", "setup")
            s["info_weight"] = role_map[sid].get("info_weight", 3)
        else:
            s["narrative_role"] = "setup"
            s["info_weight"] = 3

    # 保存
    narr_file = work_dir / "narrative_structure.json"
    narr_file.write_text(json.dumps(scenes_analysis, ensure_ascii=False, indent=2))

    log(f"叙事结构分析完成: {len(roles)}/{len(scenes_analysis)} 场景已标注")
    return scenes_analysis
