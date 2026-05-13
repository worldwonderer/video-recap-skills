import json
import re
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from config import CONFIG
from common import log, api_call, load_prompt, _parse_narration_json
from asr import _annotate_asr_temporal

# ── Step 5 (zone mode): 解说区模式 ────────────────────────────────────

def generate_narration_zones(zones, asr_result, work_dir, style="纪录片"):
    """按解说区生成大段解说（zone 模式）。"""
    system_prompt = load_prompt("NARRATION_SYSTEM_PROMPT")
    if not system_prompt:
        system_prompt = "你是专业的视频解说文案撰写师。"

    style_prompt = load_prompt(f"NARRATION_STYLE_{style}")
    if style_prompt:
        system_prompt = system_prompt + "\n\n" + style_prompt

    ctx = CONFIG.get("context_info", "")
    if ctx:
        system_prompt = system_prompt + f"\n\n已知背景：{ctx}"

    # 加载背景调研（browser-cdp 或 agent 手动写入）
    research_path = Path(work_dir) / "background_research.json"
    if research_path.exists():
        try:
            research = json.loads(research_path.read_text("utf-8"))
            parts = []
            if research.get("synopsis"):
                parts.append(f"剧情梗概: {research['synopsis']}")
            if research.get("characters"):
                chars = "\n".join(f"  - {k}: {v}" for k, v in research["characters"].items())
                parts.append(f"角色信息:\n{chars}")
            if research.get("worldbuilding"):
                parts.append(f"世界观设定: {research['worldbuilding']}")
            if research.get("episode_context"):
                parts.append(f"当前集上下文: {research['episode_context']}")
            if parts:
                system_prompt += "\n\n【背景知识（从网络调研获得）】\n" + "\n".join(parts)
                log(f"  注入背景调研: {len(parts)} 个维度")
        except (json.JSONDecodeError, KeyError):
            pass

    # 组装解说区描述
    effective_rate = CONFIG["speech_rate"] * CONFIG["speech_safety_margin"]
    breath_sec = CONFIG.get("breath_ms", 600) / 1000

    zones_text = ""
    for i, z in enumerate(zones):
        dur = z["duration"]
        max_chars = max(10, int((dur - breath_sec) * effective_rate))
        scenes_desc = ""
        for s in z["scenes"]:
            depth = s.get("depth_analysis", "")
            depth_text = f"\n    深层分析: {depth}" if depth else ""
            facts_text = _format_frame_facts(s)
            scenes_desc += f"    场景{s['scene_id']+1} ({s['start']:.1f}s-{s['end']:.1f}s): {s['description']}{depth_text}{facts_text}\n"
        zones_text += (
            f"【解说区{i+1}】({z['start']:.1f}s-{z['end']:.1f}s, "
            f"时长{dur:.1f}s, 最多{max_chars}字)\n"
            f"  覆盖场景:\n{scenes_desc}\n"
        )

    # ASR 文本
    asr_text = ""
    if asr_result:
        for seg in asr_result:
            asr_text += seg.get("text", "") + " "
    asr_text = asr_text.strip() or "(无原始语音)"

    user_template = load_prompt("NARRATION_ZONE_USER_PROMPT")
    if not user_template:
        user_template = (
            "解说区:\n{zones_text}\n\n"
            "原始对白: {asr_result}\n\n"
            "风格: {style}\n\n"
            "请为每个解说区生成一段解说，JSON 数组输出。"
        )

    user_content = user_template.format(
        zones_text=zones_text.strip(),
        asr_result=asr_text,
        style=style,
    )

    payload = {
        "model": CONFIG.get("llm_model", CONFIG["vlm_model"]),
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "max_tokens": 4000,
    }

    log(f"按解说区模式生成 (风格: {style}, {len(zones)} 个区)...")

    narration_text = ""
    for attempt in range(3):
        resp = api_call(payload)
        try:
            msg = resp["choices"][0]["message"]
            narration_text = msg.get("content", "") or ""
            if not narration_text:
                narration_text = msg.get("reasoning_content", "") or ""
            if narration_text:
                break
        except (KeyError, IndexError):
            pass
        if attempt < 2:
            log(f"  LLM 返回空，重试 ({attempt+2}/3)...")

    narration = _parse_narration_json(narration_text)
    if not narration:
        log("  LLM 未返回有效 JSON，逐区 fallback")
        narration = _fallback_zone_narration(zones)

    # 过滤空段
    narration = [n for n in narration if n.get("narration", "").strip()]

    # 对齐每个解说段的 start 到对应区的开头
    for n in narration:
        n["overlaps_speech"] = False
        # 找最近的区
        for z in zones:
            if z["start"] <= n.get("start", 0) <= z["end"]:
                n["start"] = z["start"]
                if n.get("end", 0) > z["end"]:
                    n["end"] = z["end"]
                break

    log(f"解说区模式生成完成: {len(narration)} 段")
    for n in narration:
        log(f"  {n['start']:.1f}s-{n['end']:.1f}s: {n['narration'][:30]}...")

    return narration


def _fallback_zone_narration(zones):
    """逐区简单生成 fallback。"""
    results = []
    for z in zones:
        # 从场景描述中提取关键信息
        scenes_desc = "，".join(s.get("description", "") for s in z["scenes"])
        prompt = (
            f"根据以下场景描述，生成一段不超过{int(z['duration'] * 2.5)}字的中文解说（讲故事风格，不要描述画面）：\n"
            f"{scenes_desc}\n"
            f"已知背景：{CONFIG.get('context_info', '无')}\n"
            f"只输出解说文本，不要其他内容。"
        )
        payload = {
            "model": CONFIG.get("llm_model", CONFIG["vlm_model"]),
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 500,
        }
        resp = api_call(payload)
        text = resp.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
        if text:
            results.append({
                "start": z["start"],
                "end": z["end"],
                "narration": text,
                "pause_after_ms": 600,
                "overlaps_speech": False,
            })
    return results


def _format_frame_facts(scene):
    """将帧动作描述格式化为可注入 scenes_text/zones_text 的文本"""
    facts = scene.get("frame_facts", {})
    if not facts:
        return ""
    lines = []
    for ts in sorted(facts.keys(), key=lambda x: float(x)):
        actions = facts[ts]
        lines.append(f"    {ts}s: {'; '.join(actions)}")
    return "\n  帧动作:\n" + "\n".join(lines)


# ── Step 5: 解说脚本生成 ─────────────────────────────────────────────

def generate_narration(scenes_analysis, asr_result, work_dir, style="纪录片",
                       silence_periods=None):
    """使用 LLM 生成解说脚本"""
    system_prompt = load_prompt("NARRATION_SYSTEM_PROMPT")
    if not system_prompt:
        system_prompt = "你是专业的视频解说文案撰写师。根据视频的场景分析和原始音频转录，生成流畅自然的中文解说词。每段 15-40 字，适合口语播报。"

    # 加载风格化追加 prompt
    style_prompt = load_prompt(f"NARRATION_STYLE_{style}")
    if style_prompt:
        system_prompt = system_prompt + "\n\n" + style_prompt

    ctx = CONFIG.get("context_info", "")
    if ctx:
        system_prompt = system_prompt + f"\n\n已知背景：{ctx}"

    user_prompt_template = load_prompt("NARRATION_USER_PROMPT")
    if not user_prompt_template:
        user_prompt_template = """场景分析：
{scenes_analysis}

原始音频转录：
{asr_result}

解说风格：{style}

请生成视频解说脚本，严格按 JSON 数组格式输出:
[{{"start": 开始秒数, "end": 结束秒数, "narration": "解说文本"}}]

每段 narration 控制在 15-40 个中文字。"""

    # 组装场景描述（含精确字数预算 + 段数建议 + 叙事角色）
    # 预算基于实际可用时长（减去呼吸+fade），用保守安全系数确保 TTS 不超
    effective_rate = CONFIG["speech_rate"] * CONFIG["speech_safety_margin"]
    breath_sec = CONFIG.get("breath_ms", 600) / 1000
    scenes_text = ""
    for s in scenes_analysis:
        duration = s["end"] - s["start"]
        # 实际可用时长 = 场景时长 - 呼吸空间（fade 只是振幅包络，不消耗时间）
        available = max(1.0, duration - breath_sec)
        # info_weight 影响预算分配
        info_weight = s.get("info_weight", 3)
        weight_multiplier = 0.7 if info_weight <= 2 else (1.5 if info_weight >= 4 else 1.0)
        max_chars = max(5, int(available * effective_rate * weight_multiplier))
        n_seg_hint = max(1, round(duration / 5))
        per_seg_max = max(5, int(max_chars / n_seg_hint))
        role = s.get("narrative_role", "")
        role_tag = f" [{role}]" if role else ""
        # 深层分析（如果有）
        depth = s.get("depth_analysis", "")
        depth_text = f"\n  深层分析: {depth}" if depth else ""

        # 安静窗口标注（如果有）
        quiet_text = ""
        if silence_periods:
            scene_quiets = [qp for qp in silence_periods
                           if not qp.get("has_speech", False)
                           and qp["start"] < s["end"] and qp["end"] > s["start"]]
            if scene_quiets:
                windows = ", ".join(f"{max(qp['start'],s['start']):.1f}s-{min(qp['end'],s['end']):.1f}s"
                                   for qp in scene_quiets)
                quiet_text = f"\n  安静时段(适合放解说): [{windows}]"
            else:
                mid = (s["start"] + s["end"]) / 2
                quiet_text = f"\n  (无明显安静窗口，放在{mid:.1f}s附近即可，后续会自动对齐)"

        if n_seg_hint > 1:
            scenes_text += f"- 场景{s['scene_id']+1}{role_tag} ({s['start']:.1f}s-{s['end']:.1f}s, 总预算{max_chars}字, 每段≤{per_seg_max}字, 建议{n_seg_hint}段): {s['description']}{depth_text}{_format_frame_facts(s)}{quiet_text}\n"
        else:
            scenes_text += f"- 场景{s['scene_id']+1}{role_tag} ({s['start']:.1f}s-{s['end']:.1f}s, 总预算{max_chars}字, 每段≤{per_seg_max}字): {s['description']}{depth_text}{_format_frame_facts(s)}{quiet_text}\n"

    # 组装 ASR 文本（按场景时间段分配）
    asr_text = _annotate_asr_temporal(asr_result, scenes_analysis)

    user_content = user_prompt_template.format(
        scenes_analysis=scenes_text.strip(),
        asr_result=asr_text.strip() or "(无原始语音)",
        style=style,
    )

    payload = {
        "model": CONFIG.get("llm_model", CONFIG["vlm_model"]),
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "max_tokens": 4000,
    }

    log(f"生成解说脚本 (风格: {style})...")

    # 推理模型可能将思考放在 reasoning_content，实际输出在 content
    # 如果 content 为空则重试（最多 3 次）
    narration_text = ""
    for attempt in range(3):
        resp = api_call(payload)
        try:
            msg = resp["choices"][0]["message"]
            narration_text = msg.get("content", "") or ""
            if not narration_text:
                reasoning = msg.get("reasoning_content", "") or ""
                # 尝试从 reasoning 中提取 JSON
                narration_text = _extract_json_from_text(reasoning)
                if narration_text:
                    log(f"  从推理输出中提取 JSON (attempt {attempt+1})")
                    break
                log(f"  content 为空，重试 ({attempt+2}/3)...")
                continue
            break
        except (KeyError, IndexError):
            raise RuntimeError(f"LLM 返回异常: {json.dumps(resp, ensure_ascii=False)[:300]}")

    if not narration_text:
        raise RuntimeError("LLM 多次返回空 content，请检查模型或 API 配置")

    # 解析 JSON
    narration = _parse_narration_json(narration_text)

    # 后验证：按场景检查总字数，超预算按比例缩减
    narration = _validate_narration_budget(narration, scenes_analysis)

    # Debug: 保存初始解说用于分析覆盖率
    initial_covered = set()
    for n in narration:
        n_mid = (n["start"] + n["end"]) / 2
        for s in scenes_analysis:
            if s["start"] <= n_mid <= s["end"]:
                initial_covered.add(s["scene_id"])
                break
    initial_pct = len(initial_covered) / len(scenes_analysis) * 100 if scenes_analysis else 100
    log(f"初始覆盖率: {initial_pct:.0f}% ({len(initial_covered)}/{len(scenes_analysis)})")
    (work_dir / "narration_initial.json").write_text(
        json.dumps(narration, ensure_ascii=False, indent=2))

    # 覆盖率检查：逐场景补充，3 轮递进 (90% → 95% → 100%)
    # 构建 per-scene ASR 对白映射（fill 和 temporal fill 共用）
    scene_asr = {}
    for line in asr_text.strip().split("\n"):
        m = re.match(r'\[(\d+)s-(\d+)s\]\s*(.*)', line)
        if m:
            a_start, a_end = float(m.group(1)), float(m.group(2))
            dialog = m.group(3).strip()
            if not dialog:
                continue
            for s in scenes_analysis:
                if s["start"] < a_end and s["end"] > a_start:
                    scene_asr.setdefault(s["scene_id"], []).append(dialog)

    fill_thresholds = [0.40, 0.60]
    for fill_round, threshold in enumerate(fill_thresholds):
        covered_scenes = set()
        for n in narration:
            if not n.get("narration", "").strip():
                continue
            n_mid = (n["start"] + n["end"]) / 2
            for s in scenes_analysis:
                if s["start"] <= n_mid <= s["end"]:
                    covered_scenes.add(s["scene_id"])
                    break
        coverage = len(covered_scenes) / len(scenes_analysis) if scenes_analysis else 1.0
        if coverage >= threshold:
            log(f"覆盖率 {coverage:.0%} 已达标 ({len(covered_scenes)}/{len(scenes_analysis)})")
            continue
        uncovered = [s for s in scenes_analysis if s["scene_id"] not in covered_scenes and (s["end"] - s["start"]) >= 5.0]
        if not uncovered:
            break
        log(f"覆盖率 {coverage:.0%} 不足 (轮次{fill_round+1}, 阈值{threshold:.0%})，逐场景补充 {len(uncovered)} 个未覆盖场景")
        narration.sort(key=lambda x: x["start"])
        existing_ctx = "\n".join(
            f"  [{n['start']:.1f}-{n['end']:.1f}] {n['narration']}"
            for n in narration
        )
        fill_workers = min(len(uncovered), CONFIG.get("fill_workers", 4))
        with ThreadPoolExecutor(max_workers=fill_workers) as executor:
            futures = {executor.submit(
                _generate_single_fill, scene, silence_periods, existing_ctx,
                "; ".join(scene_asr.get(scene["scene_id"], []))
            ): scene for scene in uncovered}
            for future in as_completed(futures):
                try:
                    seg = future.result()
                except Exception as e:
                    scene = futures[future]
                    log(f"  fill 场景{scene['scene_id']+1} 失败: {e}")
                    log(f"  详细错误: {traceback.format_exc()}")
                    continue
                if seg:
                    narration.append(seg)
        narration.sort(key=lambda x: x["start"])
        narration = _validate_narration_budget(narration, scenes_analysis)

    # Temporal gap fill: 长场景中空白时段追加解说（并行）
    temporal_gap_min = CONFIG.get("temporal_gap_min", 8.0)
    temporal_tasks = []  # (scene, gap_start, gap_end, ctx, dialogue)
    for scene in scenes_analysis:
        scene_dur = scene["end"] - scene["start"]
        if scene_dur < 6.0:
            continue
        scene_segs = sorted(
            [n for n in narration
             if scene["start"] <= (n["start"] + n["end"]) / 2 <= scene["end"]
             and n.get("narration", "").strip()],
            key=lambda x: x["start"]
        )
        if not scene_segs:
            continue
        covered = sum(
            min(n["end"], scene["end"]) - max(n["start"], scene["start"])
            for n in scene_segs
        )
        if covered / scene_dur >= 0.5:
            continue
        gaps = []
        if scene_segs[0]["start"] - scene["start"] > temporal_gap_min:
            gaps.append((scene["start"], scene_segs[0]["start"]))
        for i in range(1, len(scene_segs)):
            gs, ge = scene_segs[i - 1]["end"], scene_segs[i]["start"]
            if ge - gs > temporal_gap_min:
                gaps.append((gs, ge))
        if scene["end"] - scene_segs[-1]["end"] > temporal_gap_min:
            gaps.append((scene_segs[-1]["end"], scene["end"]))
        if not gaps:
            continue
        existing_ctx = "\n".join(
            f"  [{n['start']:.1f}-{n['end']:.1f}] {n['narration']}"
            for n in narration
        )
        dialogue = "; ".join(scene_asr.get(scene["scene_id"], []))
        for gap_start, gap_end in gaps:
            temporal_tasks.append((scene, gap_start, gap_end, existing_ctx, dialogue))

    temporal_filled = 0
    if temporal_tasks:
        fill_workers = min(len(temporal_tasks), CONFIG.get("fill_workers", 4))
        with ThreadPoolExecutor(max_workers=fill_workers) as executor:
            futures = {
                executor.submit(_generate_temporal_fill, scene, gs, ge, ctx, dlg): (scene, gs, ge)
                for scene, gs, ge, ctx, dlg in temporal_tasks
            }
            for future in as_completed(futures):
                try:
                    seg = future.result()
                except Exception as e:
                    log(f"  temporal fill 失败: {e}")
                    log(f"  详细错误: {traceback.format_exc()}")
                    continue
                if seg:
                    narration.append(seg)
                    temporal_filled += 1
    if temporal_filled:
        narration.sort(key=lambda x: x["start"])
        narration = _validate_narration_budget(narration, scenes_analysis)
        log(f"时间覆盖率补充: +{temporal_filled} 段")

    # Fallback: API 反复失败的场景用 depth_analysis 生成简短解说
    covered_scenes = set()
    for n in narration:
        n_mid = (n.get("start", 0) + n.get("end", 0)) / 2
        for s in scenes_analysis:
            if s["start"] <= n_mid <= s["end"]:
                covered_scenes.add(s["scene_id"])
                break
    still_uncovered = [s for s in scenes_analysis if s["scene_id"] not in covered_scenes]
    # Fallback: 跳过未覆盖场景（避免低质量硬拼）
    # 注意：不再从 depth_analysis 截取中文字符拼凑解说，
    # 覆盖率 60% 优于垃圾内容 100%
    uncovered_count = 0
    too_short_count = 0
    for scene in still_uncovered:
        dur = scene["end"] - scene["start"]
        if dur < 5.0:
            too_short_count += 1
            log(f"  未覆盖场景{scene['scene_id']+1} ({dur:.1f}s): 过短，跳过")
        else:
            uncovered_count += 1
            log(f"  未覆盖场景{scene['scene_id']+1} ({dur:.1f}s): 跳过（无可用解说源）")
    if uncovered_count:
        log(f"{uncovered_count} 个场景无解说覆盖，建议手动补充或增大 fill_thresholds")
    if too_short_count:
        log(f"{too_short_count} 个场景过短（<5s）未覆盖")

    # 过滤空段
    narration = [n for n in narration if n.get("narration", "").strip()]

    # 保存
    narration_file = work_dir / "narration.json"
    narration_file.write_text(json.dumps(narration, ensure_ascii=False, indent=2))

    log(f"解说脚本生成完成: {len(narration)} 段")
    for n in narration:
        log(f"  {n['start']:.1f}s-{n['end']:.1f}s: {n['narration'][:30]}...")

    return narration


def _fill_api_call(prompt, max_chars):
    """公共 fill API 调用：发送 prompt，解析并验证返回的解说 JSON。返回 dict 或 None"""
    system_prompt = (
        "你是视频解说精炼师。只输出 JSON，不要多余文字。\n"
        "核心规则：口语化自然表达，揭示角色真实意图和潜台词，"
        "禁止'象征''隐喻''展现了'等空洞/文学词汇，"
        "用口语化方式揭示角色潜台词，每个场景用不同的表达方式。"
        "注意画面字幕与内容的反差（如结尾字幕揭示结局与画面形成讽刺对比），优先捕捉。"
    )
    ctx = CONFIG.get("context_info", "")
    if ctx:
        system_prompt += f"\n已知背景：{ctx}"
    for _attempt in range(2):
        try:
            resp = api_call({
                "model": CONFIG["llm_model"],
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt}
                ],
                "max_tokens": 200,
            })
            msg = resp["choices"][0]["message"]
            text = msg.get("content", "") or ""
            if not text:
                text = msg.get("reasoning_content", "") or ""
                text = _extract_json_from_text(text) or text
            if not text or text.strip() in ("0", "1", ""):
                if _attempt == 0:
                    time.sleep(1)
                    continue
                return None
            segments = _parse_narration_json(text)
            if isinstance(segments, dict):
                segments = [segments]
            if not segments or not isinstance(segments, list):
                # Fallback: 尝试从纯文本提取解说
                clean = text.strip().strip('`').strip('"').strip("'")
                if 5 <= len(clean) <= max_chars * 1.5:
                    return {"narration": clean[:max_chars], "start": 0, "end": 0,
                            "pause_after_ms": 600}
                if _attempt == 0:
                    time.sleep(1)
                    continue
                return None
            seg = segments[0]
            narr_text = seg.get("narration", "")
            if len(narr_text) > max_chars:
                truncated = _truncate_at_sentence(narr_text, max_chars)
                if truncated and len(truncated) >= 5:
                    seg["narration"] = truncated
                else:
                    if _attempt == 0:
                        time.sleep(1)
                        continue
                    return None
            if not seg.get("narration", "").strip():
                if _attempt == 0:
                    time.sleep(1)
                    continue
                return None
            return seg
        except Exception as e:
            if _attempt == 0:
                time.sleep(2)
                continue
    return None


def _generate_single_fill(scene, silence_periods=None, existing_narration="", scene_dialogue="", research_ctx=""):
    """为单个未覆盖场景生成一句简短解说，返回 dict 或 None"""
    duration = scene["end"] - scene["start"]
    if duration < 5.0:
        return None

    effective_rate = CONFIG["speech_rate"] * CONFIG["speech_safety_margin"]
    breath_sec = CONFIG.get("breath_ms", 600) / 1000
    available = max(1.0, duration - breath_sec)
    max_chars = max(5, int(available * effective_rate))
    target_chars = max(5, int(max_chars * 0.85))

    # 安静窗口
    quiet_info = ""
    best_start = scene["start"]
    if silence_periods:
        quiets = [qp for qp in silence_periods
                  if not qp.get("has_speech", False)
                  and qp["start"] < scene["end"] and qp["end"] > scene["start"]]
        if quiets:
            windows = [f"{max(qp['start'], scene['start']):.1f}-{min(qp['end'], scene['end']):.1f}"
                       for qp in quiets]
            quiet_info = f" 安静窗口:{','.join(windows)}"
            longest = max(quiets, key=lambda q: q["duration"])
            best_start = max(longest["start"], scene["start"])

    depth = scene.get("depth_analysis", "")
    depth_text = f" ({depth})" if depth else ""

    ctx_block = ""
    if existing_narration:
        ctx_block = f"\n已有解说（保持叙事连贯，承接上下文）:\n{existing_narration}\n"

    dialog_block = ""
    if scene_dialogue:
        dialog_block = f"\n原始对白(英文，翻译融入解说): {scene_dialogue}\n"

    research_block = ""
    if research_ctx:
        research_block = f"\n{research_ctx}\n"

    prompt = (
        f"为这个视频场景写一句中文解说。\n"
        f"场景: {scene['start']:.1f}s-{scene['end']:.1f}s "
        f"({target_chars}-{max_chars}字){quiet_info}\n"
        f"画面: {scene['description']}{depth_text}"
        f"{research_block}{ctx_block}{dialog_block}\n"
        f"要求：{target_chars}-{max_chars}字，揭示角色意图或潜台词，与前后解说自然衔接，以句号/感叹号结尾。避免重复用词和相同句式，用不同的表达方式。\n"
        f"严格输出 JSON: {{\"start\": {best_start:.1f}, \"end\": {scene['end']:.1f}, "
        f"\"narration\": \"...\", \"pause_after_ms\": 600}}"
    )

    seg = _fill_api_call(prompt, max_chars)
    if seg:
        seg["start"] = scene["start"]
        seg["end"] = scene["end"]
        log(f"  补充场景{scene['scene_id']+1}: \"{seg['narration']}\"")
    return seg



def _generate_temporal_fill(scene, gap_start, gap_end, existing_narration="", scene_dialogue=""):
    """为场景内的空白时段生成追加解说"""
    gap_dur = gap_end - gap_start
    if gap_dur < 3.0:
        return None
    effective_rate = CONFIG["speech_rate"] * CONFIG["speech_safety_margin"]
    breath_sec = CONFIG.get("breath_ms", 600) / 1000
    max_chars = max(5, int((gap_dur - breath_sec) * effective_rate))
    target_chars = max(5, int(max_chars * 0.85))
    depth = scene.get("depth_analysis", "")
    depth_text = f" ({depth})" if depth else ""
    ctx_block = ""
    if existing_narration:
        ctx_block = f"\n已有解说（保持叙事连贯，承接上下文）:\n{existing_narration}\n"
    dialog_block = ""
    if scene_dialogue:
        dialog_block = f"\n原始对白(英文，翻译融入解说): {scene_dialogue}\n"
    prompt = (
        f"为这个视频场景的空白时段追加一句中文解说。\n"
        f"场景: {scene['start']:.1f}s-{scene['end']:.1f}s\n"
        f"空白时段: {gap_start:.1f}s-{gap_end:.1f}s ({target_chars}-{max_chars}字)\n"
        f"画面: {scene['description']}{depth_text}"
        f"{ctx_block}{dialog_block}\n"
        f"要求：{target_chars}-{max_chars}字，揭示角色意图或潜台词，与前后解说自然衔接，以句号/感叹号结尾。避免重复用词和相同句式，用不同的表达方式。\n"
        f"严格输出 JSON: {{\"start\": {gap_start:.1f}, \"end\": {gap_end:.1f}, "
        f"\"narration\": \"...\", \"pause_after_ms\": 600}}"
    )
    seg = _fill_api_call(prompt, max_chars)
    if seg:
        seg["start"] = gap_start
        seg["end"] = gap_end
        log(f"  时间填充场景{scene['scene_id']+1} [{gap_start:.1f}-{gap_end:.1f}]: \"{seg['narration']}\"")
    return seg


def _text_char_count(text):
    """计算文本的有效字数（去除标点和空白，这些不占 TTS 朗读时间）"""
    return len(re.sub(r'[，。！？、；：…“”‘’《》〈〉\s"\'「」『』（）()【】\[\]—～·,.!?;:\\-]', '', text))


def _truncate_at_sentence(text, max_chars):
    """在句子边界截断，不产生残句。max_chars 按有效字符计（不含标点空白）"""
    if _text_char_count(text) <= max_chars:
        return text
    # 将有效字符预算转换为字符串位置
    eff = 0
    cutoff = len(text)
    for i, ch in enumerate(text):
        eff += 1 if _text_char_count(ch) else 0
        if eff > max_chars:
            cutoff = i + 1
            break
    # 先尝试在句号/感叹号/问号处截断
    for sep in ['。', '！', '？', '!', '?']:
        idx = text[:cutoff].rfind(sep)
        if idx > 0:
            return text[:idx + 1]
    # 回退：在最后一个逗号/顿号处截断，补句号
    for sep in ['，', '、', '；', ',']:
        idx = text[:cutoff].rfind(sep)
        if idx > 3:
            return text[:idx] + '。'
    # 无法在合理边界截断，跳过该段（避免产生不通顺的半句话）
    return ""


def _validate_narration_budget(narration, scenes_analysis):
    """后验证：按场景检查总字数，超预算在句子边界缩减"""
    effective_rate = CONFIG["speech_rate"] * CONFIG["speech_safety_margin"]
    breath_sec = CONFIG.get("breath_ms", 600) / 1000

    # 第一轮：逐段校验 — 超预算则截断到句子边界（保留覆盖，避免碎片）
    for n in narration:
        text = n.get("narration", "")
        if not text.strip():
            continue
        seg_duration = n.get("end", 0) - n.get("start", 0)
        seg_pause = n.get("pause_after_ms", CONFIG.get("breath_ms", 600)) / 1000
        available = max(0.5, seg_duration - seg_pause)
        # TTS 至少需要 2s 可用时间，否则即使最短文本也放不下
        if available < 2.0:
            n["narration"] = ""
            continue
        max_chars = max(5, int(available * effective_rate))
        # 短段弹性系数更低：3s以下不弹性，3-5s 允许 20%，5s+ 允许 30%
        flex = 1.0 if seg_duration < 3.0 else (1.2 if seg_duration < 5.0 else 1.3)
        if _text_char_count(text) > max_chars * flex:
            truncated = _truncate_at_sentence(text, max_chars)
            if truncated and len(truncated) >= 5:
                n["narration"] = truncated
            else:
                n["narration"] = ""

    # 第二轮：按场景总预算校验
    # 为每个场景计算预算（可用时间 = 场景时长 - 呼吸空间）
    scene_budgets = {}
    for s in scenes_analysis:
        duration = s["end"] - s["start"]
        available = max(1.0, duration - breath_sec)
        scene_budgets[s["scene_id"]] = max(5, int(available * effective_rate))

    # 收集需要重写的段（超预算但不截断，让 LLM 重写）
    rewrite_tasks = []  # [(narration_item, max_chars, scene_id)]
    for s in scenes_analysis:
        sid = s["scene_id"]
        budget = scene_budgets[sid]
        segs = [n for n in narration
                if s["start"] <= (n.get("start", 0) + n.get("end", 0)) / 2 <= s["end"]]
        if not segs:
            continue

        total_chars = sum(len(n.get("narration", "")) for n in segs)
        if total_chars <= budget:
            continue

        # 超预算：分配每段预算，找出超出的段
        n_segs = len(segs)
        per_seg_budget = max(5, budget // max(n_segs, 1))
        remaining = budget
        for n in segs:
            text = n.get("narration", "")
            seg_budget = min(per_seg_budget + 5, remaining)  # 允许微调
            if len(text) > seg_budget and remaining > 5:
                rewrite_tasks.append((n, seg_budget, sid))
                remaining = max(0, remaining - seg_budget)
                log(f"  场景{sid+1} 段超预算: \"{text[:20]}...\" ({len(text)}字 > {seg_budget}字), 标记重写")
            else:
                remaining = max(0, remaining - len(text))

    # 批量让 LLM 重写超预算的段
    if rewrite_tasks:
        prompt_parts = []
        for i, (n, mc, sid) in enumerate(rewrite_tasks):
            text = n.get("narration", "")
            slot_dur = n["end"] - n["start"]
            prompt_parts.append(f"{i+1}. 原文({len(text)}字，需压缩到≤{mc}字，时段{slot_dur:.1f}s): {text}")

        rewrite_prompt = (
            "以下解说段落超出时间预算，请重写每段使其在字数限制内。\n"
            "要求：\n"
            "- 用一针见血的短句表达核心洞察，而不是概括事件\n"
            "- 好的短解说示例：「她在试探他的底线。」「他就是想找借口靠近。」\n"
            "- 差的短解说示例：「女方客气作评价。」「男子尝酱满脸嫌弃。」\n"
            "- 必须是完整的一句话，以句号/感叹号/问号结尾\n"
            "- 如果字数限制少于6字，输出空字符串表示跳过该段\n"
            "- 严格按 JSON 数组格式输出，顺序对应\n\n"
            + "\n".join(prompt_parts) + "\n\n"
            + "输出格式: [\"重写后的第1段\", \"重写后的第2段\", ...]"
        )

        payload = {
            "model": CONFIG.get("llm_model", CONFIG["vlm_model"]),
            "messages": [
                {"role": "system", "content": "你是视频解说文案精炼师。将过长的解说压缩到指定字数内。核心要求：揭示角色潜台词或真实意图，而不是概括事件。宁可空字符串跳过，也不要写空洞概括。" + (f"\n已知背景：{CONFIG.get('context_info', '')}" if CONFIG.get("context_info", "") else "")},
                {"role": "user", "content": rewrite_prompt},
            ],
            "max_tokens": 2000,
        }

        log(f"  重写 {len(rewrite_tasks)} 段超预算解说...")
        try:
            resp = api_call(payload)
            content = resp["choices"][0]["message"].get("content", "") or ""
            if not content:
                content = resp["choices"][0]["message"].get("reasoning_content", "")
            # 解析 JSON 数组
            rewrites = _parse_narration_json(content)
            if isinstance(rewrites, list) and len(rewrites) == len(rewrite_tasks):
                for i, (n, max_chars, sid) in enumerate(rewrite_tasks):
                    if i < len(rewrites):
                        new_text = rewrites[i] if isinstance(rewrites[i], str) else rewrites[i].get("narration", "")
                        if new_text and len(new_text) <= max_chars * 1.1:
                            n["narration"] = new_text
                            log(f"    重写: \"{new_text}\" ({len(new_text)}字)")
                        else:
                            n["narration"] = ""
                            log(f"    重写超限，丢弃: \"{new_text[:20]}...\"")
            else:
                # 解析失败，尝试简单分割
                import json as _json
                try:
                    parsed = _json.loads(content)
                    if isinstance(parsed, list):
                        for i, item in enumerate(parsed):
                            if i < len(rewrite_tasks):
                                n, mc, sid = rewrite_tasks[i]
                                text = item if isinstance(item, str) else item.get("narration", "")
                                if text and len(text) <= mc * 1.1:
                                    n["narration"] = text
                except (_json.JSONDecodeError, TypeError):
                    log(f"    重写结果解析失败，丢弃超预算段")
                    for n, mc, sid in rewrite_tasks:
                        n["narration"] = ""
        except (KeyError, IndexError, RuntimeError):
            log(f"    重写 API 异常，丢弃超预算段")
            for n, mc, sid in rewrite_tasks:
                n["narration"] = ""

    # 移除空文本段、过短残段、零时长段和以不完整标点结尾的段
    narration = [n for n in narration
                 if n.get("narration", "").strip()
                 and len(n.get("narration", "").strip()) >= 5
                 and n.get("end", 0) - n.get("start", 0) >= 1.0
                 and n["narration"].strip()[-1] not in "，：、；,—…"]

    # 清理标点错误
    for n in narration:
        text = n.get("narration", "")
        # 修复：[标点]。→ 。
        text = re.sub(r'[，：、；,]["\']?[。！？]', '。', text)
        # 清理末尾不匹配的左引号："好。→ 好。
        text = re.sub(r'["\']。$', '。', text)
        n["narration"] = text

    # 修正时间边界：确保每段在场景范围内，且 start < end
    valid = []
    for n in narration:
        if n["start"] >= n["end"]:
            continue
        for s in scenes_analysis:
            if s["start"] <= n["start"] < s["end"]:
                n["end"] = min(n["end"], s["end"])
                break
        # 边界修正后再次检查时长（可能被裁到很短）
        if n["end"] - n["start"] >= 1.0:
            valid.append(n)
    narration = valid

    # 去重：相同文本只保留一段
    seen_text = set()
    unique = []
    for n in narration:
        key = n["narration"].strip()
        if key not in seen_text:
            seen_text.add(key)
            unique.append(n)
        else:
            log(f"  去重: 删除重复文本 '{key[:25]}...'")
    narration = unique

    # 按时间排序并检测重叠
    narration.sort(key=lambda n: n["start"])
    deduped = []
    for n in narration:
        if deduped and n["start"] < deduped[-1]["end"]:
            prev = deduped[-1]
            log(f"  重叠: 段({n['start']:.1f}-{n['end']:.1f}) vs "
                f"({prev['start']:.1f}-{prev['end']:.1f})")
            if len(n["narration"]) > len(prev["narration"]):
                deduped[-1] = n
        else:
            deduped.append(n)
    narration = deduped

    return narration


# ── Phase 2: 画面对齐检测 + 自动重写 ──────────────────────────────────

# 中文停用词（简化版，覆盖常见虚词/代词/量词）
_STOP_WORDS = set("的了是在不有人我他她它们这那个一上下来去说到做会能要就和又"
                  "也都被从把让给向与而为则若虽却已之于以及其中".split())
_STOP_WORDS.update(["什么", "怎么", "这个", "那个", "一个", "自己", "已经",
                     "可以", "因为", "所以", "但是", "如果", "虽然", "而且"])


def _extract_noun_phrases(text):
    """从中文文本中提取2-4字名词短语（无需分词库）"""
    phrases = set()
    if not text:
        return phrases
    # 过滤标点，保留中文和数字
    cleaned = re.sub(r'[^一-鿿0-9]', '', text)
    # 滑动窗口提取2-4字片段
    for length in (4, 3, 2):
        for i in range(len(cleaned) - length + 1):
            phrase = cleaned[i:i + length]
            # 跳过全停用词的片段
            if all(c in _STOP_WORDS for c in phrase):
                continue
            # 至少包含一个实词字符（非停用词）
            if any(c not in _STOP_WORDS for c in phrase):
                phrases.add(phrase)
    return phrases


def _extract_frame_entities(vlm_analysis, start, end):
    """从帧动作描述中收集时间段内所有关键实体"""
    entities = set()
    for scene in vlm_analysis:
        if not (scene["start"] < end and scene["end"] > start):
            continue
        facts = scene.get("frame_facts", {})
        for ts, actions in facts.items():
            t = float(ts)
            if start <= t <= end:
                for action in actions:
                    entities.update(_extract_noun_phrases(action))
    return entities


def _char_bigrams(text):
    return {text[i:i+2] for i in range(len(text)-1) if text[i:i+2].strip()}

def _post_dedup_narration(narration):
    """去除相邻相似解说段（bigram Jaccard >50% 则合并）"""
    if len(narration) < 2:
        return narration
    result = [narration[0]]
    for seg in narration[1:]:
        prev = result[-1]
        if not prev["narration"].strip() or not seg["narration"].strip():
            result.append(seg)
            continue
        # bigram 级 Jaccard 相似度
        set_a, set_b = _char_bigrams(prev["narration"]), _char_bigrams(seg["narration"])
        if not set_a or not set_b:
            result.append(seg)
            continue
        overlap = len(set_a & set_b) / min(len(set_a), len(set_b))
        if overlap > 0.4:
            # 保留更长的版本，合并时间范围
            if len(seg["narration"]) > len(prev["narration"]):
                prev["narration"] = seg["narration"]
            prev["end"] = seg["end"]
            prev["pause_after_ms"] = seg.get("pause_after_ms", prev.get("pause_after_ms", 600))
            log(f"  去重合并: {prev['start']:.0f}-{prev['end']:.0f}s")
        else:
            result.append(seg)
    removed = len(narration) - len(result)
    if removed:
        log(f"  去重: {len(narration)} → {len(result)} 段 (合并 {removed} 段)")
    return result


def _validate_and_rewrite_narration(narration, vlm_analysis, work_dir):
    """关键词检测 → 约束重写闭环"""
    pass_threshold = 0.30
    warn_threshold = 0.10
    report_segments = []

    for n in narration:
        text = n.get("narration", "")
        if not text.strip():
            continue

        seg_start = n.get("start", 0)
        seg_end = n.get("end", 0)

        # 收集该时段的帧实体
        frame_entities = _extract_frame_entities(vlm_analysis, seg_start, seg_end)
        if not frame_entities:
            report_segments.append({
                "start": seg_start, "end": seg_end,
                "match_rate": 1.0, "level": "PASS",
                "reason": "no frame_facts for this time range",
            })
            continue

        # 从解说中提取实体
        narr_entities = _extract_noun_phrases(text)
        if not narr_entities:
            report_segments.append({
                "start": seg_start, "end": seg_end,
                "match_rate": 1.0, "level": "PASS",
                "reason": "no entities extracted from narration",
            })
            continue

        # 计算匹配率
        matched = 0
        mismatched = []
        for ne in narr_entities:
            found = False
            for fe in frame_entities:
                if ne in fe or fe in ne:
                    found = True
                    break
            if found:
                matched += 1
            else:
                mismatched.append(ne)

        match_rate = matched / len(narr_entities) if narr_entities else 0

        # 确定级别
        if match_rate >= pass_threshold:
            level = "PASS"
        elif match_rate >= warn_threshold:
            level = "WARN"
        else:
            level = "WARN_HIGH"

        seg_report = {
            "start": seg_start, "end": seg_end,
            "match_rate": round(match_rate, 2), "level": level,
            "narration_preview": text[:50],
            "frame_entities_count": len(frame_entities),
            "mismatched_sample": mismatched[:5],
        }
        report_segments.append(seg_report)

        # WARN_HIGH 且启用自动重写 → 约束重写
        if level == "WARN_HIGH":
            log(f"  对齐检测 WARN_HIGH ({match_rate:.0%}): \"{text[:30]}...\" → 触发重写")
            rewritten = _rewrite_segment_with_constraints(n, vlm_analysis, mismatched)
            if rewritten:
                n["narration"] = rewritten
                n["alignment_rewritten"] = True
                seg_report["rewritten"] = True
                seg_report["new_narration_preview"] = rewritten[:50]

    # 汇总
    levels = [s["level"] for s in report_segments]
    summary = {
        "total": len(report_segments),
        "pass": levels.count("PASS"),
        "warn": levels.count("WARN"),
        "warn_high": levels.count("WARN_HIGH"),
        "rewritten": sum(1 for s in report_segments if s.get("rewritten")),
    }

    report = {"segments": report_segments, "summary": summary}
    report_path = Path(work_dir) / "alignment_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    log(f"对齐检测完成: {summary['pass']} PASS, {summary['warn']} WARN, {summary['warn_high']} WARN_HIGH"
        + (f", {summary['rewritten']} rewritten" if summary["rewritten"] else ""))

    return narration, report


def _rewrite_segment_with_constraints(seg, vlm_analysis, mismatched):
    """对不匹配的解说段执行约束重写（最多1轮）"""
    start = seg.get("start", 0)
    end = seg.get("end", 0)
    original = seg.get("narration", "")
    effective_rate = CONFIG["speech_rate"] * CONFIG["speech_safety_margin"]
    breath_sec = CONFIG.get("breath_ms", 600) / 1000
    max_chars = max(10, int((end - start - breath_sec) * effective_rate))

    # 收集该时段的帧动作描述
    facts_lines = []
    for scene in vlm_analysis:
        if not (scene["start"] < end and scene["end"] > start):
            continue
        for ts, actions in sorted(scene.get("frame_facts", {}).items(),
                                   key=lambda x: float(x[0])):
            t = float(ts)
            if start <= t <= end:
                facts_lines.append(f"{ts}s: {'; '.join(actions)}")

    if not facts_lines:
        return None

    facts_text = "\n".join(facts_lines)
    constraints = (
        f"你之前为 {start:.1f}s-{end:.1f}s 时段写的解说提到了: {', '.join(mismatched[:5])}\n"
        f"但画面数据中不包含这些内容。该时段实际画面:\n{facts_text}\n\n"
        f"请仅基于以上画面数据重新生成该时段的解说（不超过{max_chars}字）。\n"
        f"要求：讲故事风格，不要描述画面本身，讲画面背后的故事。只输出解说文本。"
    )

    payload = {
        "model": CONFIG.get("llm_model", CONFIG["vlm_model"]),
        "messages": [{"role": "user", "content": constraints}],
        "max_tokens": 500,
    }

    try:
        resp = api_call(payload)
        text = resp.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
        if text and len(text) <= max_chars * 1.3 and len(text) >= 5:
            return text
    except Exception as e:
        log(f"  约束重写失败: {e}")
    return None


def _extract_json_from_text(text):
    """从推理模型的思考文本中提取 JSON（数组或单个对象）"""
    if not text:
        return ""
    for pattern in [r"```json\s*(.*?)\s*```", r"```\s*(.*?)\s*```"]:
        m = re.search(pattern, text, re.DOTALL)
        if m:
            candidate = m.group(1).strip()
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, (list, dict)):
                    return candidate
            except json.JSONDecodeError:
                continue
    # 尝试匹配裸 JSON 数组或对象
    for pattern in [r"(\[.*\])", r"(\{.*\})"]:
        m = re.search(pattern, text, re.DOTALL)
        if m:
            candidate = m.group(1).strip()
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, (list, dict)):
                    return candidate
            except json.JSONDecodeError:
                continue
    return ""


def _zone_coverage_fill(narration, scenes_analysis, asr_result, silence_periods, work_dir):
    """混合模式补充：zone 解说后，为未覆盖的重要场景生成 fill 解说。
    fill 段标记 overlaps_speech=True，组装时使用 speech ducking。
    """
    if not narration or not scenes_analysis:
        return narration

    # 计算每个场景的覆盖百分比
    scene_cov_pct = {}
    for s in scenes_analysis:
        s_dur = s["end"] - s["start"]
        narrated = 0.0
        for n in narration:
            if not n.get("narration", "").strip():
                continue
            ov_start = max(n["start"], s["start"])
            ov_end = min(n["end"], s["end"])
            if ov_end > ov_start:
                narrated += ov_end - ov_start
        scene_cov_pct[s["scene_id"]] = narrated / s_dur if s_dur > 0 else 1.0

    covered = {sid for sid, pct in scene_cov_pct.items() if pct >= 0.5}
    coverage = len(covered) / len(scenes_analysis)
    target = 0.60
    if coverage >= target:
        log(f"Zone+Fill 覆盖率 {coverage:.0%} 已达标 ({len(covered)}/{len(scenes_analysis)})")
        return narration

    # 找覆盖不足的场景（<50%），同场景合并为单次 fill 避免重复
    uncovered_raw = [s for s in scenes_analysis
                     if scene_cov_pct.get(s["scene_id"], 0) < 0.5 and (s["end"] - s["start"]) >= 5.0]
    uncovered = []
    for s in uncovered_raw:
        s_dur = s["end"] - s["start"]
        # 计算该场景内已解说区间
        narrated_intervals = []
        for n in narration:
            if not n.get("narration", "").strip():
                continue
            ov_start = max(n["start"], s["start"])
            ov_end = min(n["end"], s["end"])
            if ov_end > ov_start:
                narrated_intervals.append((ov_start, ov_end))
        narrated_intervals.sort()

        # 计算未覆盖的间隙
        gaps = []
        cursor = s["start"]
        for ns, ne in narrated_intervals:
            if ns > cursor + 1.0:
                gaps.append((cursor, ns))
            cursor = max(cursor, ne)
        if cursor < s["end"] - 1.0:
            gaps.append((cursor, s["end"]))

        if not gaps:
            continue

        # 同一场景只取最大间隙生成1个 fill，避免重复内容
        best_gap = max(gaps, key=lambda g: g[1] - g[0])
        gap_s, gap_e = best_gap
        if gap_e - gap_s >= 3.0:
            sub = dict(s)
            sub["start"] = gap_s
            sub["end"] = min(gap_e, gap_s + 25)  # 最多覆盖25s
            sub["is_sub"] = True
            uncovered.append(sub)
    if not uncovered:
        return narration
    log(f"Zone 覆盖率 {coverage:.0%}，补充 {len(uncovered)} 个重要未覆盖场景 (ducking)")

    # 构建 ASR 场景映射
    scene_asr = {}
    if asr_result:
        for seg in asr_result:
            text = seg.get("text", "").strip()
            if not text:
                continue
            a_start = seg.get("start", 0)
            a_end = seg.get("end", 0)
            for s in scenes_analysis:
                if s["start"] < a_end and s["end"] > a_start:
                    scene_asr.setdefault(s["scene_id"], []).append(text)

    # 构建 context
    narration_sorted = sorted(narration, key=lambda x: x["start"])
    existing_ctx = "\n".join(
        f"  [{n['start']:.1f}-{n['end']:.1f}] {n['narration']}"
        for n in narration_sorted
    )

    # 并行 fill
    # 加载背景调研
    research_ctx = ""
    research_path = Path(work_dir) / "background_research.json"
    if research_path.exists():
        try:
            research = json.loads(research_path.read_text("utf-8"))
            parts = []
            if research.get("characters"):
                parts.append("角色: " + ", ".join(f"{k}({v})" for k, v in research["characters"].items()))
            if research.get("episode_context"):
                parts.append(research["episode_context"])
            if parts:
                research_ctx = "【背景】" + "; ".join(parts)
        except (json.JSONDecodeError, KeyError):
            pass

    fill_workers = min(len(uncovered), CONFIG.get("fill_workers", 4))
    with ThreadPoolExecutor(max_workers=fill_workers) as executor:
        futures = {executor.submit(
            _generate_single_fill, scene, silence_periods, existing_ctx,
            "; ".join(scene_asr.get(scene["scene_id"], [])),
            research_ctx
        ): scene for scene in uncovered}
        for future in as_completed(futures):
            try:
                seg = future.result()
            except Exception:
                continue
            if seg:
                seg["overlaps_speech"] = True
                narration.append(seg)

    narration.sort(key=lambda x: x["start"])
    # 去重
    narration = _post_dedup_narration(narration)

    # 报告最终覆盖率（基于时长百分比）
    covered2 = set()
    for s in scenes_analysis:
        s_dur = s["end"] - s["start"]
        narrated = 0.0
        for n in narration:
            if not n.get("narration", "").strip():
                continue
            ov_start = max(n["start"], s["start"])
            ov_end = min(n["end"], s["end"])
            if ov_end > ov_start:
                narrated += ov_end - ov_start
        if narrated / s_dur >= 0.5 if s_dur > 0 else True:
            covered2.add(s["scene_id"])
    final_cov = len(covered2) / len(scenes_analysis) if scenes_analysis else 1.0
    zone_count = sum(1 for n in narration if not n.get("overlaps_speech"))
    fill_count = sum(1 for n in narration if n.get("overlaps_speech"))
    log(f"混合模式完成: {len(narration)} 段 (zone={zone_count}, fill={fill_count}), 覆盖率 {final_cov:.0%}")
    return narration


def _align_narration_to_quiet(narration, scenes_analysis, silence_periods):
    """将解说段移到同场景内的安静窗口，标记是否与语音重叠"""
    if not silence_periods:
        for n in narration:
            n["overlaps_speech"] = False
        return narration

    quiet_windows = [qp for qp in silence_periods if not qp.get("has_speech", False)]

    for n in narration:
        seg_start = n["start"]
        seg_end = n["end"]
        seg_dur = seg_end - seg_start
        best_window = None
        best_overlap = 0

        # 找同时间范围内最佳安静窗口
        for qw in quiet_windows:
            overlap_start = max(seg_start, qw["start"])
            overlap_end = min(seg_end, qw["end"])
            overlap = overlap_end - overlap_start
            if overlap > best_overlap:
                best_overlap = overlap
                best_window = qw

        if best_window and best_overlap > 0:
            # 尝试将段移到安静窗口内
            new_start = max(best_window["start"], seg_start - (seg_dur * 0.5))
            new_start = min(new_start, best_window["end"] - seg_dur)

            # 确保不超出场景边界（用 midpoint 查找，与覆盖率/验证一致）
            seg_mid = (seg_start + seg_end) / 2
            parent_scene = None
            for s in scenes_analysis:
                if s["start"] <= seg_mid <= s["end"]:
                    parent_scene = s
                    break
            if parent_scene:
                new_start = max(parent_scene["start"], new_start)
                new_start = min(new_start, parent_scene["end"] - seg_dur)

            # 不允许移到视频之前
            new_start = max(0.0, new_start)
            new_start = round(new_start, 2)
            new_end = round(new_start + seg_dur, 2)

            if new_end > new_start:
                n["start"] = new_start
                n["end"] = new_end
                n["overlaps_speech"] = False
            else:
                n["overlaps_speech"] = True
        else:
            n["overlaps_speech"] = True

    # 后处理：解决段间重叠（按时间排序，确保后段不与前段冲突）
    narration.sort(key=lambda x: x["start"])
    for i in range(1, len(narration)):
        prev = narration[i - 1]
        curr = narration[i]
        min_gap = 0.3  # 段间最小间隔
        min_start = prev["end"] + min_gap
        if curr["start"] < min_start:
            seg_dur = curr["end"] - curr["start"]
            curr["start"] = round(min_start, 2)
            curr["end"] = round(min_start + seg_dur, 2)
            # 检查是否超出父场景边界
            mid = (curr["start"] + curr["end"]) / 2
            for s in scenes_analysis:
                if s["start"] <= mid <= s["end"]:
                    if curr["end"] > s["end"]:
                        curr["end"] = round(s["end"], 2)
                    break
            # 段太短则清空（后续会被过滤）
            if curr["end"] - curr["start"] < 1.5:
                curr["narration"] = ""

    # 后处理：预算重校验 — alignment 可能缩短了段时长，截断超预算文本
    effective_rate = CONFIG["speech_rate"] * CONFIG["speech_safety_margin"]
    breath_sec = CONFIG.get("breath_ms", 600) / 1000
    for n in narration:
        dur = n["end"] - n["start"]
        if dur < 1.0:
            n["narration"] = ""
            continue
        max_chars = max(5, int((dur - breath_sec) * effective_rate))
        text = n.get("narration", "")
        chars = len(re.sub(r'[，。！？、…\s]', '', text))
        if chars > max_chars:
            truncated = _truncate_at_sentence(text, max_chars)
            if truncated and len(re.sub(r'[，。！？、…\s]', '', truncated)) >= 5:
                n["narration"] = truncated
            else:
                n["narration"] = ""

    return narration

