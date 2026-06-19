# 数据格式（中间 JSON）

所有中间文件均在 pipeline 工作目录（work_dir/）下。

## vlm_analysis.json

每场景的 VLM 分析结果，数组格式：

```json
[
  {
    "scene_id": 1,
    "start": 5.0,
    "end": 15.0,
    "description": "男子闯入房间",
    "depth_analysis": "角色情绪分析...",
    "frame_facts": {
      "5.0": ["男子闯入房间, 头发蓬乱表情紧张"],
      "10.0": ["男子俯身盯着床上男孩, 男孩睁眼惊醒"]
    }
  }
]
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `scene_id` | int | 场景编号 |
| `start` | float | 开始时间（秒） |
| `end` | float | 结束时间（秒） |
| `description` | string | 画面简述（≤80字） |
| `depth_analysis` | string | 深层分析（情绪/关系/潜台词） |
| `frame_facts` | object | 帧级事实，key 为时间戳字符串 |

## asr_result.json

语音转文字结果：

```json
[
  {"start": 0.0, "end": 3.5, "text": "What are you doing here?"}
]
```

## asr_writing_chunks.json

由 CLI 在生成 `agent_narration_brief.md` 时自动写出。它把长 ASR 按句子边界拆成适合 Agent 消化的语义块；中文按字符计数，非 CJK 文本按词数计数，并尽量保留 scene 对齐。

```json
[
  {
    "chunk_id": 0,
    "start": 0.0,
    "end": 28.5,
    "scene_ids": [0, 1],
    "char_count": 642,
    "text": "第一段对白……",
    "segments": [
      {"start": 0.0, "end": 3.5, "text": "第一句。", "char_count": 4}
    ]
  }
]
```

## silence_periods.json

静音窗口列表（适合放解说）：

```json
[
  {"start": 2.0, "end": 8.5, "duration": 6.5, "has_speech": false}
]
```

`has_speech` 标记该窗口是否与检测到的 ASR 语音重叠；下游（pipeline / narration）只把 `has_speech=false` 的窗口当作可放解说的安静窗口。

## timeline_fusion.json

由 CLI 在生成 brief 时自动写出。它把 VLM 场景、ASR 对白和静音窗口按时间轴 overlap 合并，减少写稿时手工推断“这一幕有没有对白/能不能插解说”的成本。

```json
[
  {
    "scene_id": 0,
    "time_range": [0.0, 10.0],
    "visual_description": "两人在门口对峙",
    "depth_analysis": "关系紧张",
    "frame_facts": {"1.0": ["女子回头"]},
    "dialogue_segments": [
      {"start": 2.0, "end": 4.0, "overlap_seconds": 2.0, "text": "你到底是谁"}
    ],
    "dialogue_overlap_seconds": 2.0,
    "narration_slots": [
      {"start": 5.0, "end": 7.0, "duration": 2.0, "char_budget": 5}
    ],
    "recommended_mode": "ducked-bed"
  }
]
```

## narration.json

Agent 撰写的解说词（字段详见 `agent-mode-workflow.md`）。full 模式下使用原视频时间；cut 模式下也先使用原视频时间，CLI 会生成 `narration_mapped.json`：

```json
[
  {"start": 2.5, "end": 7.0, "narration": "解说文本", "pause_after_ms": 250, "overlaps_speech": true}
]
```

## narration_lint.json

`--step script` 或续跑验证 `narration.json` 时生成的预检结果。它检查写稿、时间安全和解说密度。`metrics` 为 full 模式下的密度指标（cut 模式为空对象）。

```json
{
  "ok": false,
  "error_count": 1,
  "warning_count": 1,
  "metrics": {
    "segment_count": 12,
    "narration_coverage": 0.68,
    "narration_seconds": 61.2,
    "timeline_seconds": 90.0,
    "avg_block_chars": 48,
    "original_block_count": 4
  },
  "errors": [
    {"level": "error", "index": 2, "code": "time_overlap", "message": "Segment overlaps the previous narration segment"}
  ],
  "warnings": [
    {"level": "warning", "index": 0, "code": "over_budget", "budget_chars": 28, "actual_chars": 42}
  ]
}
```

常见 code：`invalid_time`、`empty_narration`、`time_overlap`、`outside_clip_plan`、`over_budget`、`incomplete_sentence`、`slot_too_short`、`under_narrated`、`over_narrated`、`fragmented_beats`、`no_original_blocks`。

## clip_plan.json

cut 模式下 Agent 选择要保留的原片片段，数组或 `{ "clips": [...] }` 都可接受。默认片段不能重叠，避免同一原片时间映射到多个输出位置：

```json
{
  "target_duration": "10m",
  "clips": [
    {"start": 12.0, "end": 38.0, "reason": "冲突开端"}
  ]
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `start` | float | 原视频片段开始秒数 |
| `end` | float | 原视频片段结束秒数 |
| `reason` | string | 选择该片段的剧情/信息原因 |

## clip_plan_validated.json

CLI 校验 `clip_plan.json` 后写出，额外包含输出时间轴：

```json
{
  "clips": [
    {
      "clip_id": 0,
      "source_start": 12.0,
      "source_end": 38.0,
      "output_start": 0.0,
      "output_end": 26.0,
      "duration": 26.0,
      "reason": "冲突开端"
    }
  ],
  "total_duration": 26.0,
  "target_duration": 600.0
}
```

## narration_mapped.json

cut 模式下由 CLI 生成，`start/end` 已变成短视频输出时间，`source_start/source_end` 保留原视频时间：

```json
[
  {
    "start": 2.0,
    "end": 7.0,
    "source_start": 14.0,
    "source_end": 19.0,
    "source_clip_id": 0,
    "narration": "解说文本"
  }
]
```

## background_research.json

可选的背景调研结果（由 Agent 使用任意可用搜索/浏览方式整理）：

```json
{
  "synopsis": "剧情概要",
  "characters": {"角色名": "角色简介"},
  "worldbuilding": "世界观设定",
  "episode_context": "集数上下文",
  "character_details": {
    "角色名": {
      "aliases": ["别名/昵称"],
      "role": "主角|配角|反派|次要角色",
      "relationships": ["与XX是夫妻", "与YY是师徒"]
    }
  },
  "plot_arcs": [
    {"name": "线索名称", "description": "简要描述", "status": "进行中|已解决|伏笔"}
  ],
  "cultural_notes": [
    {"item": "文化梗/典故/时代背景", "explanation": "解释"}
  ]
}
```

> `character_details`、`plot_arcs`、`cultural_notes` 为（可选，新增）字段。仅含 `synopsis`、`characters`、`worldbuilding`、`episode_context` 四个原始字段的旧 JSON 仍然有效。

## workflow_state.json

统一步骤状态文件。`.step_*.done` 仍保留用于兼容旧工作目录和手动清理习惯，但新的续跑逻辑会同时写入 `workflow_state.json`，记录视频内容指纹、每步状态、耗时和参数指纹。

```json
{
  "schema_version": 1,
  "input_video": "/path/to/video.mp4",
  "video_fingerprint": "md5...",
  "started_at": "2026-06-06T12:00:00Z",
  "steps": {
    "extract": {
      "status": "done",
      "progress": 100,
      "started_at": "2026-06-06T12:00:01Z",
      "completed_at": "2026-06-06T12:00:05Z",
      "elapsed_s": 4.0,
      "params": {"fps": 1},
      "params_fingerprint": "md5..."
    }
  }
}
```
