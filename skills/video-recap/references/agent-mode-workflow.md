# Agent 模式工作流

## 输入文件清单

Agent 写解说词前，先读取以下中间文件（均在 work_dir/ 下）：

| 文件 | 内容 |
|------|------|
| `vlm_analysis.json` | 每场景的画面描述、深度分析、帧级事实 (`frame_facts`) |
| `asr_result.json` | 语音转文字结果，含时间戳和对白文本 |
| `silence_periods.json` | 静音窗口列表，用于确定解说放置位置 |

## 背景调研（可选）

用 `browser-cdp` skill 搜索以下内容，写入 `work_dir/background_research.json`：

- `"{节目名} 剧情 介绍"` → synopsis
- `"{节目名} 人物 关系"` → characters
- `"{节目名} 世界观/设定"` → worldbuilding

**background_research.json 格式：**
```json
{
  "synopsis": "...",
  "characters": {"角色名": "简介"},
  "worldbuilding": "...",
  "episode_context": "..."
}
```

Pipeline 会自动检测并注入到解说生成的 system prompt 中。

## narration.json 字段

```json
[
  {
    "start": 5.0,
    "end": 12.0,
    "narration": "解说文本",
    "pause_after_ms": 600,
    "overlaps_speech": false
  }
]
```

| 字段 | 说明 |
|------|------|
| `start` | 解说开始时间（秒） |
| `end` | 解说结束时间（秒） |
| `narration` | 解说文本 |
| `pause_after_ms` | 段后停顿毫秒数 |
| `overlaps_speech` | 是否与原声对白重叠 |

## 写作铁律

1. **禁止看图说话**：观众看得见画面，不要描述动作和表情
2. **讲故事**：揭示角色意图、潜台词、关系动态
3. **大段解说 + 原声交替**：解说区集中在安静窗口，原声区保留原始对白
4. **字数公式**：每段字数 ≤ (end - start - 0.6) × 3，超限会被截断
5. **用角色名**：如果提供了 `--context`，使用角色名（如 Big、凯莉）

## 完成后操作

写完 narration.json 后执行：

```bash
# 标记解说词完成
touch work_dir/.step_script.done
# 清除 TTS 缓存（否则复用旧音频）
rm -rf work_dir/tts_segments/ work_dir/.step_tts.done \
  work_dir/.step_assemble.done work_dir/tts_meta.json
# 继续 TTS + 组装
python3 scripts/video_recap.py <video> --resume work_dir
```

**每次改完 narration.json 后必须删 `tts_segments/`**，否则会复用旧音频。
