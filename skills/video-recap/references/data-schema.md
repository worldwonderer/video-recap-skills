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

Agent 撰写的解说词。full 模式下使用原视频时间；**orchestrated cut 模式（`video-recap --edit-mode cut`）下，第二次暂停时已经先剪出 `edited_source.mp4`，因此 `narration.json` 必须直接使用剪后成片的 OUTPUT 时间轴（0..成片总时长），不会再生成或消费 `narration_mapped.json`。只有 legacy direct `video-cut` 单 pass 路径才会把原视频时间的 narration remap 成 `narration_mapped.json`：

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

`packaging_plan.json` 是包装层契约：标题、封面帧/视觉钩子、首句、观众承诺、卖点和发布包装信息。它帮助 review 判断“包装承诺”和正文前 15 秒是否对齐。

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

`deslop_qc_requirements.json` 是 tool/brief generated run contract：工具或 brief 生成本次运行的 QC 要求，供 `deslop_qc` 读取，不由 Agent 手写。字段包括 `schema_version`、`owner`、`style_card_required`、`packaging_plan_expected`、`deslop_qc.report_only`、`deslop_qc.aigc_detector`、`deslop_qc.auto_rewrite`。

`deslop_qc` 只根据这个运行契约判断缺少 `style_card.json` 是否是 blocker；它不扫描 `agent_narration_brief.md` 的 prompt wording 来推断 `style_card_required`。如果 requirements 文件缺失或损坏，按 legacy/migration advisory 处理，不作为 hard failure。

该契约不改变 `--style`：`--style` 仍是 freeform verbatim guidance，不增加固定风格档位。它也不改变 `deslop_qc` 边界：仍然是 report-only，不是 AIGC detector，不自动改写。

最小示例：

```json
{
  "schema_version": "1.0",
  "owner": "tool_or_brief",
  "style_card_required": true,
  "packaging_plan_expected": true,
  "deslop_qc": {
    "report_only": true,
    "aigc_detector": false,
    "auto_rewrite": false
  }
}
```

## deslop_qc.json（CLI 生成，报告型 QC）

`deslop_qc.json` 由本地 deterministic scanner 生成，Agent 不手写。它只是 report-only QC：不是 AIGC detector，不判断文本是不是 AI 写的，不会自动改写。修改仍由 Agent/人工根据报告回到 `narration.json`、`style_card.json` 或字幕源里处理。

报告分两层：

- `blockers`：客观阻断项，会并入 `narration_lint.json` 的 error，例如 requirements 要求但缺少/损坏 `style_card.json`、破折号、占位符泄漏、模板化“不是……而是……”转折。
- `advisories`：建议项，只提示可读性/口语化风险，例如套话密度、抽象总结词、解释链、比喻标记、过长段落；它们不自动阻断，也不自动改写。

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

## multi_source_manifest.json（多视频 cut）

多视频剪辑模式下，项目级 `work_dir/multi_source_manifest.json` 是 `recap.py` / `cut.py` / `assemble.py` 的来源契约。`source_id` 默认由源文件 SHA-256 派生为 `src_<fingerprint[:12]>`；同一项目里重复 fingerprint 的不同路径会追加短 path hash 后缀。

```json
{
  "schema_version": 1,
  "sources": [
    {
      "source_id": "src_0123456789ab",
      "source_path": "/abs/episode1.mp4",
      "source_name": "episode1.mp4",
      "source_video_fingerprint": "0123456789abcdef...",
      "source_work_dir": "sources/src_0123456789ab",
      "material_id": "episode1-0123456789ab"
    }
  ]
}
```

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

多视频 cut 的 `clip_plan.json` 必须给每个片段加 `source_id`；重叠检测按 `source_id` 分开计算，不同源视频的相同时间范围不互相冲突：

```json
{
  "target_duration": "10m",
  "clips": [
    {"source_id": "src_0123456789ab", "start": 12.0, "end": 38.0, "reason": "episode 1 hook"},
    {"source_id": "src_fedcba987654", "start": 4.0, "end": 22.0, "reason": "episode 2 payoff"}
  ]
}
```

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

多视频 validated clip 会额外保留来源字段，供 pass2 brief、timeline 和剪映导出追溯原素材：

```json
{
  "clips": [
    {
      "clip_id": 0,
      "source_id": "src_0123456789ab",
      "source_path": "/abs/episode1.mp4",
      "source_start": 12.0,
      "source_end": 38.0,
      "output_start": 0.0,
      "output_end": 26.0,
      "duration": 26.0,
      "reason": "episode 1 hook"
    }
  ]
}
```

## material library（可选，grep 复用）

`--material-library-dir <dir> --save-materials` 会把每个源视频的已分析小文件复制到 `<dir>/materials/<material_id>/`，不复制原始媒体。`--use-materials` 会在 fingerprint 和 settings fingerprint 匹配时把这些 JSON/MD 产物恢复到当前 per-source `work_dir`。

```text
.video-materials/
  materials_index.jsonl          # 追加式 grep journal；旧行可保留为历史
  materials/<material_id>/
    material.json                # 当前权威 metadata
    material.md                  # grep 友好摘要
    artifacts/scenes.json
    artifacts/asr_result.json
    artifacts/vlm_analysis.json
    artifacts/understanding_index.json
```

`materials_index.jsonl` 每次保存追加一行，字段包括 `schema_version`, `event`, `material_id`, `source_name`, `source_path`, `source_video_fingerprint`, `settings_fingerprint`, `summary`, `tags`, `material_dir`, `updated_at`。当前权威状态始终以 `materials/<material_id>/material.json` 为准。MVP 只承诺 `grep -R "关键词" <library>` 这类文件检索；没有 DB、embedding 或语义搜索。

保存时会对凭证形态（`tp-`/`sk-`/`gh*_`/`AKIA`/JWT 与 `KEY=VALUE` 赋值）和凭证命名的 JSON key 做脱敏，但这只是**尽力而为**的兜底，不是保证：陌生格式的密钥仍可能漏过。请从源头避免把密钥写进分析产物——key 从环境变量/`.env` 读取，不需要落进 scenes/ASR/VLM/summary 等 JSON。

## narration_mapped.json

仅 legacy direct `video-cut` 单 pass 路径会生成。orchestrated cut 模式不使用它：Agent 在 pass2 直接按剪后成片 OUTPUT 时间轴写 `narration.json`。当 legacy 路径启用时，`start/end` 已变成短视频输出时间，`source_start/source_end` 保留原视频时间：

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

## original_subtitles.json / user_subtitles.{json,srt,ass}（可选，原声留白字幕）

解说块之间的原声留白会把【原声台词】烧成字幕（assemble 阶段用 `「」` 包裹以区分解说）。来源优先级（高→低）：

1. **`work_dir/user_subtitles.json`**（用户自带，最准）— 数组 `[{start,end,text}]` 默认按**成片 OUTPUT** 时间轴直接使用；也可写成 `{"timeline": "source"|"output", "lines": [...]}`，`source` 表示按**原片**时间轴给出，由 assemble 依 `clip_plan_validated.json` 映射到成片。
2. **`work_dir/user_subtitles.srt` / `.ass`**（用户自带）— 默认按**原片**时间轴解析后映射到成片。
3. **`work_dir/original_subtitles.json`**（Agent 校对，cut pass2 写）— OUTPUT 时间轴 `[{start,end,text}]`，订正 ASR 错字/人名、只写留白里真正出声的句子。
4. **ASR 兜底** — 无上述文件时，用 `asr_result.json` 按留白粗略映射（中点估时，可能偏多偏乱）。

来源 1–3 为「精确来源」：每条按句**区间裁剪**落到所覆盖的留白边界（跨边界会拆分），不走 ASR 兜底的中点估时；over-dense 行截断显示而非丢弃。

```json
[
  {"start": 2.0, "end": 5.0, "text": "原声台词一句"}
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

## narration_review.json

`video-script/scripts/review.py` 输出的 LLM-as-judge 评审。旧字段 `verdict/summary/findings` 仍然有效；新增一份 **advisory** 的内容效果 scorecard 与改稿清单。**scorecard 不改变 verdict、也不作硬门禁**——硬门禁仍是 `findings` 里的 error（事实矛盾/残句）经 `--require-narration-review` 严格模式拦截。`verdict` 词表为 `PASS|REVISE|FAIL`，`OK` 作为旧值的兼容别名。

```json
{
  "verdict": "PASS|REVISE|FAIL|OK",
  "summary": "总体判断",
  "scorecard": {
    "promise_match": 4, "hook_3s": 4, "first_15s_delivery": 4, "spine_clarity": 4,
    "stakes_escalation": 4, "information_gain": 4, "spoken_language": 4, "sentence_brevity": 4,
    "tts_pacing": 4, "grounding": 4, "original_audio_use": 4, "subtitle_readability": 4
  },
  "hook_candidates_review": [{"candidate": "首句", "type": "suspense", "score": 4, "keep": true}],
  "retention_risk_points": [{"time": "00:28", "risk": "信息重复可能掉人", "fix": "删掉复述画面的句子"}],
  "highest_return_edits": ["最值得先改的一件事"],
  "information_gain_notes": [{"segment": 0, "label": "motive|...|visual_restatement", "note": "证据/改法"}],
  "spoken_language_rewrites": [{"segment": 0, "original": "原句", "rewrite": "口语改写", "why": "为什么更适合听"}],
  "grounding_assertions": [{"segment": 0, "assertion": "人物/关系/因果断言", "source": "visual|asr|research|user_context|unsupported", "risk": "谨慎说明"}],
  "findings": [{"segment": 0, "severity": "warning", "category": "weak_hook", "issue": "问题", "fix": "改法"}]
}
```

> 若 `work_dir` 提供了 `packaging_plan.json` / `recap_story_plan.json` / `visual_audio_board.json`（可选、Agent 撰写），review 会把它们并入评估上下文；缺失时 review 仅基于解说与画面/对白证据评分，行为不受影响。

## tts_meta.json（partial 失败可见性）

`video-voiceover` 正常输出 `{segments, engine, narration}`。当显式允许 partial TTS（`--allow-partial-tts` / `ALLOW_PARTIAL_TTS=1`）让运行在部分段失败后继续时，失败段不会只埋在日志里，而会写入 `partial` 与 `failures[]`：

```json
{
  "segments": [{"index": 0, "start": 0.0, "end": 1.0, "narration": "第一段。", "audio_path": "tts_segments/narr_000.wav"}],
  "engine": "mimo-tts",
  "narration": "narration.json",
  "partial": true,
  "failures": [{"index": 1, "start": 1.0, "end": 2.5, "text": "第二段。", "error": "network timeout"}]
}
```

> 正常（无失败）运行也会带 `"partial": false, "failures": []`。partial 成片只适合预览，不建议直接发布。

## dub_lint.json / dub_review.json

Dub 模式下，`dub_script.json` 在 voiceclone **之前**先经过 deterministic lint，把明显不可发布的脚本挡在昂贵的克隆 TTS 之前。空译文、相邻行重叠、时间越界、`room < 0.4s` 等 **error** 会 `verdict=FAIL` 并阻断 render；`fast_speech`、`trim_risk` 等是 warning，不阻断。

```json
{
  "schema_version": 1,
  "verdict": "PASS|FAIL",
  "blocking": false,
  "errors": [],
  "issues": [{"severity": "warning", "code": "fast_speech", "line": 2, "message": "translation is dense (8.4 chars/s)", "start": 1.0, "end": 2.0}],
  "summary": {"lines": 12, "errors": 0, "warnings": 1, "max_chars_per_second": 8.4, "trim_risk_lines": []}
}
```

`dub_review.json` 是脚本级 review scaffold（确定性派生自 lint，语义忠实/语气仍需 agent/人工判断）：

```json
{
  "schema_version": 1,
  "verdict": "PASS|REVISE|FAIL",
  "checks": {"faithful_to_source": "needs_agent_review", "spoken_chinese": "PASS", "speaker_tone": "needs_agent_review", "timing_fit": "PASS", "platform_fit": "needs_agent_review"},
  "highest_return_edits": [],
  "coverage": {"transcript_windows": 8, "script_lines": 12}
}
```

> CLI：`dub.py --stage lint|review`（无需 video）、`dub.py --print-schema` 打印以上全部 dub artifact 契约；`--stage render` 会在克隆前自动写 `dub_lint.json` / `dub_review.json`，lint 非 PASS 即中止。
