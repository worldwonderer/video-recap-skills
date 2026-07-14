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

Agent 撰写的解说词。full 模式下使用原视频时间；**两阶段 cut 编排流程**在第二次暂停前已经剪出 `edited_source.mp4`，因此 `narration.json` 必须直接使用剪后成片的 OUTPUT 时间轴（0..成片总时长），不会再生成或消费 `narration_mapped.json`。只有旧版直接单阶段剪辑路径才会把原视频时间的 narration remap 成 `narration_mapped.json`：

```json
[
  {"start": 2.5, "end": 7.0, "narration": "解说文本", "pause_after_ms": 250, "overlaps_speech": true}
]
```

## narration_lint.json

续跑验证 `narration.json` 时生成的预检结果。它检查写稿、时间安全和解说覆盖。`metrics` 为 full 模式下的诊断指标（cut 模式为空对象），不是要求命中某个旁白比例的创作配额；低覆盖 warning 应检查是否为有意的原声/沉默选择。

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

## style_card.json（Agent 撰写，可选/按 brief 要求）

`style_card.json` 是表达层契约：由 Agent 根据 `--style`、`--context`、素材证据、ASR 和用户偏好信号综合撰写。`--style` 是 freeform verbatim guidance（原样自由文本指导），不是枚举、preset、switch，也不是一组可穷举风格名；不要把它翻译成固定档位。

这个文件记录声音、节奏、回收意图和证据支撑的表达判断；字段可以随项目增减，下游只把它当 JSON object 读取，不要求固定键名。它不负责标题、封面、首句承诺或卖点包装。

```json
{
  "voice": "冷静但有压迫感，少讲大道理，多用人物动作和台词里的证据推进",
  "pacing": "前 15 秒短句建立冲突，中段留原声喘息，结尾回收开头疑问",
  "payoff_intent": "让观众先看到误会，再看到人物选择的代价",
  "subtitle_read_posture": "句子可听、可读，不堆长抽象句",
  "evidence_intent": ["优先引用画面动作", "关键转折保留原声"]
}
```

## packaging_plan.json（Agent 撰写，可选）

`packaging_plan.json` 是内容锁定后的可选包装层契约：标题、封面帧/视觉钩子、首句、观众承诺、卖点和发布包装信息。它帮助 review 判断“包装承诺”和正文前 15 秒是否对齐；不应反过来驱动故事取舍。

它不是文风策略，不覆盖 `style_card.json` 的声音、节奏或表达规则；如果包装需要某个承诺，正文仍要用素材证据兑现。

```json
{
  "title": "一句能对外展示的标题",
  "cover_frame": {"time": 12.4, "reason": "人物第一次正面做出关键选择"},
  "first_line": "开场第一句解说",
  "viewer_promise": "观众看完会明白的冲突/反转/信息增量",
  "selling_points": ["强冲突", "原声高光"],
  "packaging_notes": "发布侧备注，不写文风规则"
}
```

## deslop_qc_requirements.json（工具/brief 生成的运行契约）

`deslop_qc_requirements.json` 是 tool/brief generated run contract：工具或 brief 生成本次运行的 QC 要求，供 `deslop_qc` 读取，不由 Agent 手写。字段为 `schema_version` 与 `style_card_required`。

`style_card_required` 默认 `false`（advisory）：缺少 `style_card.json` 只是 warning，不阻断出片。将来的 opt-in 运行可把它设为 `true`，让 `style_card.json` 成为硬性要求——`deslop_qc` 只读这个字段判断缺少 `style_card.json` 是否是 blocker，不扫描 `agent_narration_brief.md` 的 prompt wording 来推断。如果 requirements 文件缺失或损坏，按 legacy/migration advisory 处理，不作为 hard failure。

该契约不改变 `--style`：`--style` 仍是 freeform verbatim guidance，不增加固定风格档位。它也不改变 `deslop_qc` 边界：仍然是 report-only，不是 AIGC detector，不自动改写。

最小示例：

```json
{
  "schema_version": 1,
  "style_card_required": false
}
```

## deslop_qc.json（CLI 生成，报告型 QC）

`deslop_qc.json` 由本地 deterministic scanner 生成，Agent 不手写。它只是 report-only QC：不是 AIGC detector，不判断文本是不是 AI 写的，不会自动改写。修改仍由 Agent/人工根据报告回到 `narration.json`、`style_card.json` 或字幕源里处理。

报告分两层：

- `blockers`：客观阻断项，会并入 `narration_lint.json` 的 error，例如 requirements 要求但缺少/损坏 `style_card.json`、破折号、占位符泄漏。
- `advisories`：建议项，只提示可读性/口语化风险，例如模板化“不是……而是……”转折、套话密度、抽象总结词、解释链、比喻标记、过长段落；它们不自动阻断，也不自动改写。

```json
{
  "ok": false,
  "contract": "Local readability/QC report only: this is not an AIGC detector, does not claim AI-generation accuracy, and never rewrites text. Corrections remain human/agent rewrite work.",
  "scanner": "deslop_qc.py",
  "style_card_required": true,
  "blocker_count": 1,
  "advisory_count": 1,
  "blockers": [
    {"severity": "blocker", "code": "missing_style_card", "source": "style_card", "index": null, "message": "style_card.json is required by this expression/packaging run but is missing"}
  ],
  "advisories": [
    {"severity": "advisory", "code": "cliche_density", "source": "narration", "index": null, "message": "套话/高频抽象词偏密，建议换成具体行动、选择和后果"}
  ],
  "metrics": {"segments_scanned": 12, "text_units": 860, "sentence_count": 38}
}
```

## clip_plan.json

cut 模式下 Agent 选择要保留的原片片段，数组或 `{ "clips": [...] }` 都可接受。默认片段不能重叠，避免同一原片时间映射到多个输出位置：

```json
{
  "target_duration": "10m",
  "clips": [
    {"start": 12.0, "end": 38.0, "reason": "b01 | hook | knowledge: unknown→threat | POV=主角 | 保留倾听反应 | 入点=问题已问出 | 出点=沉默落地"}
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
      "reason": "b01 | hook | knowledge: unknown→threat | POV=主角 | 保留倾听反应 | 入点=问题已问出 | 出点=沉默落地"
    }
  ],
  "total_duration": 26.0,
  "target_duration": 600.0
}
```

## narration_mapped.json

仅旧版直接单阶段剪辑路径会生成。两阶段 cut 编排流程不使用它：Agent 在第二阶段直接按剪后成片 OUTPUT 时间轴写 `narration.json`。启用旧版路径时，`start/end` 已变成短视频输出时间，`source_start/source_end` 保留原视频时间：

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
