# Agent 解说词工作流

## 运行前置分析

```bash
python3 scripts/video_recap.py <video> --tts edge-tts --context "背景"
```

Pipeline 会完成场景检测、ASR、VLM 分析和静音检测，然后暂停，并在 `work_dir/` 里写出：

| 文件 | 内容 |
|------|------|
| `agent_narration_brief.md` | 给 Agent 写解说词用的场景、时长、安静窗口和字数预算 |
| `asr_writing_chunks.json` | 按句界切分、带时间与 scene 对齐的 ASR 写作块 |
| `timeline_fusion.json` | VLM + ASR + 静音窗口 overlap 后的统一时间轴视图 |
| `vlm_analysis.json` | 每场景的画面描述、深度分析、帧级事实 (`frame_facts`) |
| `asr_result.json` | 语音转文字结果，含时间戳和对白文本 |
| `silence_periods.json` | 静音窗口列表，用于确定解说放置位置 |

写稿时优先读 `agent_narration_brief.md`。长对白先按 brief 里的 ASR writing chunks 消化；判断“这幕有无对白/静音槽”时优先看 timeline fusion。需要查证细节时再看原始 JSON。

## 剪辑式解说（长视频剪短）

如果目标是“40 分钟剪成约 10 分钟解说”，启动时加（目标时长用于规划和提醒，最终以 `clip_plan.json` 总时长为准）：

```bash
python3 scripts/video_recap.py <video> --edit-mode cut --target-duration 10m --tts edge-tts
```

cut 模式暂停后按这个顺序写稿：

1. 先写 `clip_plan.json`：选择要保留的原片片段，时间戳使用**原视频时间**。
2. 再审一遍片段选择，写 `clip_plan_review.md`：判断开头钩子、主线递进、关键对白/反转/行动覆盖、重复片段、结尾回收是否够支撑解说。
3. 如果审阅结论是 `REVISE`，先修改 `clip_plan.json` 并重新审阅；如果是 `APPROVE`，进入写稿。
4. 最后写 `narration.json`：解说词也使用**原视频时间**，并且应落在已选择片段内。

默认不允许 `clip_plan.json` 片段重叠；如果确实需要重复使用同一段原片，先拆成不重叠片段；如确实需要重复画面，当前可在 `narration.json` 段落里加 `source_clip_id` 指向 `clip_plan_validated.json` 中的片段编号。CLI 续跑时只校验时间、重叠、片段归属、映射和解说时长预算；`clip_plan_review.md` 只给 Agent 写稿参考，CLI 不读取、不打分。

示例：

```json
{
  "target_duration": "10m",
  "clips": [
    {"start": 12.0, "end": 38.0, "reason": "冲突开端"},
    {"start": 120.0, "end": 168.0, "reason": "关键反转"}
  ]
}
```

`clip_plan_review.md` 建议格式：

```md
# Clip Plan Review

Verdict: APPROVE | REVISE

## Overall judgment
一句话判断这组片段是否能支撑完整解说。

## Required changes
- 如果需要修改，写清楚改哪个片段、补什么剧情 beat、删什么重复或废片段。

## Clip notes
- 0: keep/change/drop — 具体原因。

## Approval condition
如果不是 APPROVE，写清楚改到什么程度即可继续写 narration.json。
```

续跑时 CLI 会生成：

| 文件 | 内容 |
|------|------|
| `clip_plan_validated.json` | 校验并补充 output timeline 后的片段表 |
| `edited_source.mp4` | 按 `clip_plan.json` 拼出的短视频源 |
| `narration_mapped.json` | 把原视频时间映射到短视频时间后的解说稿 |


## 背景调研（推荐）

详细操作指南见 `references/research-guide.md`。如果 `--context` 包含节目/电影名称，且当前环境有可用搜索/浏览能力，推荐先调研并写入 `work_dir/background_research.json`：

```json
{
  "synopsis": "...",
  "characters": {"角色名": "简介"},
  "worldbuilding": "...",
  "episode_context": "..."
}
```

## narration.json 字段

```json
[
  {
    "start": 5.0,
    "end": 12.0,
    "narration": "解说文本。",
    "pause_after_ms": 250,
    "overlaps_speech": true
  }
]
```

| 字段 | 说明 |
|------|------|
| `start` | 解说开始时间（秒） |
| `end` | 解说结束时间（秒） |
| `narration` | 解说文本 |
| `pause_after_ms` | 段后停顿毫秒数，默认 250（保持紧凑节奏） |
| `overlaps_speech` | 是否与原声对白重叠；连续铺底风格默认 true，只在真正静音处才设 false |

## 写作规则（连续原声铺底的高密度 recap）

1. **连续解说**：沿整条时间轴用短促 beat 连续解说，原声作为压低的背景一直存在。
2. **达到密度目标**：按 brief 头部给出的目标（约 9.6 段/分钟，最低 6.24），相邻 beat 间隔不要超过 11 秒。
3. **默认重叠原声**：`overlaps_speech` 默认 true，解说盖在原声之上；只有刻意放进真正静音空档的 beat 才设 false。
4. **每段短小**：大约一句短句（1-2 行字幕），宁短不长；对 edge-tts 更稳、更好读。字数 ≤ `(end - start - 0.25) × 3`。
5. **不要看图说话**：观众看得见动作和表情，解说应讲动机、关系、潜台词和剧情意义。
6. **用已知角色名**：如果 `--context` 或调研提供了角色名，优先使用角色名。
7. **完整句子**：以句号、问号或感叹号结束，不写半句话。

## 解说手法（区分“解说”和“字幕”）

- **钩子**：开头 1-2 个 beat 要制造悬念或利害，而不是交代场景，让观众想看下一句。
- **主线**：选定一条主线（目标 / 关系 / 悬念），每个 beat 都推进它，不要每个场景从头开始。
- **递进**：信息和张力逐步升级，后面的 beat 要比前面的更重。
- **悬念缺口**：提前埋后果（“他还不知道，这一步会要命”），后面再回收。
- **收尾**：最后 1-2 个 beat 必须给出结果或反转，留余味，不要用泛泛的句子收场。
- **给信息而非念画面**：每个 beat 都要补充画面看不出的东西（是谁、为什么、有什么利害）。
- **去废词**：用具体名词动词，删掉“危机四伏”“震撼人心”这类空泛形容。

> 若 work_dir 里有 `background_research.json`，CLI 会自动把其中的人物 / 关系 / 剧情读进 brief 顶部的「Story context」；写稿时直接引用，不要再写“男子”“白发女子”这类泛称。

## 继续 TTS + 组装

写完 `narration.json` 后执行：

```bash
python3 scripts/video_recap.py <video> --resume work_dir
```

如果改过已经配音的 `narration.json`，先清理旧 TTS 缓存再续跑，清理命令见 `references/pipeline-resume.md`（「改解说词后重新配音」一节）。
