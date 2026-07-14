---
name: video-script
description: >
 对已完成分析的视频进行导演与剪辑策划，再写带时间戳的中文解说并校验。work_dir 已包含
 agent_narration_brief.md 与 vlm_analysis.json 时使用。适用于故事方向、片段选择、画面/原声/旁白分工、
 解说写作与复核。输入 work_dir 中的理解索引；输出 recap_story_plan.json、visual_audio_board.json、
 cut 模式需要的 clip_plan.json，以及通过校验的 narration.json。触发词：解说词、写解说、视频旁白、
 narration script、写稿、解说文案、剪辑思路、导演思路。
---

## 1. 定位

本技能负责：创作方向、画面/声音计划、旁白写作与校验。Agent 不是 JSON 填写器，而要依次扮演：

1. 导演
2. 故事编辑
3. 画面剪辑师
4. 声音/旁白编辑
5. 第一次观看的观众

Agent 先记录简洁决定，再写时间线产物。`validate.py` 负责对理解索引做机械校验；full 模式还会把旁白对齐到安静窗口。

下面的 `scripts/...` 均相对于本技能目录。若执行器从仓库根目录启动，请给脚本路径加上本技能的绝对目录。本技能不从其他技能目录读取参考文件或辅助脚本；外部输入只来自显式路径与 `work_dir` 产物。

## 2. 读取素材并确认状态

首先阅读：

- `work_dir/agent_narration_brief.md`：场景、时长、安静窗口与字数预算。
- `asr_writing_chunks.json`：长对白的写作分块。
- `timeline_fusion.json`：判断某段是否有对白或静音槽。
- `vlm_analysis.json` / `asr_result.json`：核对具体画面与原声证据。
- brief 顶部列出的 contact sheet：不要只依赖场景摘要；反应、走位、静止和台词前后的具体时刻常常更重要。

full 模式使用原片时间。cut 模式第一阶段只写 `clip_plan.json`；`edited_source.mp4` 产生后，第二阶段才按输出时间写 `narration.json`。

写任何创作产物前，直接读取 `work_dir` 判断当前阶段：

- `recap_run_manifest.json`：确认 `edit_mode`、源视频和本轮设置。
- full 模式：没有 `narration.json` 时进入写稿；存在时先复核再校验。
- cut 第一阶段：尚无 `clip_plan_validated.json` / `edited_source.mp4`，只写 `clip_plan.json`。
- cut 第二阶段：两者都存在，按 `clip_plan_validated.json.clips[]` 中的 `source_start/end` 与 `output_start/end` 核对映射，再写输出时间的旁白。

必须确认旁白没有跨越错误剪辑边界，也没有落进已删除区间。整个判断只依赖 `work_dir` 产物。

## 3. 制定创作方案

先阅读 `references/creative-editing-playbook.md`，再写或更新两个工作产物：

1. **`recap_story_plan.json`**：导演意图、至少两个剪辑假设、选定的 POV / 主线，以及由“变化”定义的 beats。
2. **`visual_audio_board.json`**：每拍的画面任务、具体表演/反应、入点/出点、`audio_owner`、原声锚点与 `narration_job`。

只记录决定、证据锚点、被放弃的备选方案和简短理由，不写冗长思维过程。

### 3.1 导演判断

锁定：

- 观众承诺
- POV
- 戏剧问题
- 起始与结束情绪
- 隐瞒与揭示
- 结尾余味

### 3.2 故事编辑

比较两个真正可行的结构后选择一个。每个 beat 至少改变一项：知识、权力、目标、关系、情绪或风险。若删除后因果、人物和情绪都没有损失，该 beat 通常不应保留。

### 3.3 画面剪辑

选择具体时刻，而不是只选择事件。比较：

- 说话者与倾听者
- 动作与反应
- 早进与晚进
- 早出与多停半秒

在不破坏理解的前提下晚进早出，同时保留不可替代的表演、停顿、失误、动作声和完整台词。

### 3.4 声音与旁白分工

先指定 `audio_owner`，再写字。旁白只允许承担以下 `narration_job`：

- `context`
- `causal_link`
- `foreshadow`
- `interpretation`
- `transition`
- `none`

画面、原声或沉默已经足够时使用 `none`，不要默认铺旁白。

### 3.5 cut 模式第一阶段

cut 模式先根据 `recap_story_plan.json` 与 `visual_audio_board.json` 写原片时间的 `clip_plan.json`，此时不要写 `narration.json`：

```json
{
  "target_duration": "10m",
  "clips": [
    {
      "start": 12.0,
      "end": 38.0,
      "reason": "b01 | hook | knowledge: unknown→threat | POV=主角 | 保留倾听反应 | 入点=问题已问出 | 出点=沉默落地"
    }
  ]
}
```

`reason` 统一使用：

```text
beat_id | function | change | POV | preferred moment | 入点 | 出点
```

片段顺序必须构成一条完整故事线，而不是无序高光。可使用 0–1 个 cold open，随后回到因果清楚的 setup → turn → escalation → payoff。片段长度服从具体时刻，不使用统一秒数模板；片尾必须保留完整台词或动作。

## 4. 撰写旁白

full 模式直接按原片时间写；cut 第二阶段先查看 `edited_source.mp4` 与剪后故事板，补充 `visual_audio_board.json` 的输出时间并重新确认 `audio_owner` / `narration_job`，再按输出时间写：

```json
[
  {
    "start": 5.0,
    "end": 12.0,
    "narration": "解说文本。",
    "pause_after_ms": 250,
    "overlaps_speech": true,
    "emotion": "紧张"
  }
]
```

字段说明：

| 字段 | 含义 |
|------|------|
| `start` / `end` | full 模式为原片时间；cut 第二阶段为输出时间 |
| `narration` | 解说文本 |
| `pause_after_ms` | 段后停顿，默认 250ms |
| `overlaps_speech` | 是否与原对白重叠；连续铺底窗口通常为 `true`，真正静音槽才为 `false` |
| `emotion` | 整个解说块的 MiMo TTS 情绪/语气标签 |

### 4.1 写作规则

1. **先有 `narration_job`，后有句子**：没有明确任务就不写；旁白不是默认音轨。
2. **按解说块写**：旁白拥有一个 beat 时，用 2–4 个完整句子完成一个连续想法，并在一次 TTS 中合成；不要“一句一停”。
3. **7:3 不是配额**：只在素材判断不足时作为避免墙到墙旁白的粗略首稿参考。实际比例服从 `audio_owner`；强对白、动作声或沉默可以完整拥有一个 beat。
4. **视听接力**：旁白若引出原声，块尾要让观众想听；原声结束后的下一块要承接它造成的变化。
5. **按有效语速控量**：用 `字数 / brief 头部 speech budget` 估算窗口；装不下时删减或拆分叙事任务，不用加速堆字。
6. **不看图说话**：旁白只增加上下文、因果、预期、证据支持的解释或跨越。
7. **人物与证据优先**：优先使用已知角色名；关系、动机、潜台词和结果必须指向 visual / ASR / research / user context，且不能把背景资料伪装成当前画面事实。
8. **写给耳朵听**：使用具体名词和动词，句子完整、口语可听；避免字幕腔、半句、空泛拔高和破折号。

### 4.2 解说结构

- **钩子**：提出正文会真实兑现的问题或利害，不用无关留存话术。
- **主线**：围绕选定 POV 与主线推进，不在每个场景重新开篇。
- **递进**：后续 beat 必须提高风险、改变关系或提供新信息。
- **悬念缺口**：只预告之后真的会回收的后果。
- **收尾**：回答或有意转化开头问题，留下明确余味。
- **衔接**：旁白块与相邻原声属于同一个 beat，前者铺垫、后者呈现、下一块承接。

### 4.3 原声留白字幕

可选写 `original_subtitles.json`，使用成片输出时间：

```json
[{"start": 15.0, "end": 17.0, "text": "原声台词"}]
```

只写留白中实际听得到的台词，订正 ASR 错字与人名，每条尽量控制在一行；被旁白盖住或已经剪掉的句子不要写。省略时，合成阶段会使用保守的 ASR 映射兜底，并在成片中用 `「」` 区分原声对白与旁白。

## 5. 创作自审

在调用 LLM 评审前做以下**反事实检查**：

1. 删除每个 beat：若因果、人物、情绪或承诺没有损失，就删除。
2. 比较说话者/动作与倾听者/反应：保留更符合 POV 和情绪的时刻。
3. 静音旁白：画面与原声仍应承载可见行动、人物行为和关键情绪。
4. 只听声音：旁白应形成可听懂的主线，而不是画面字幕。
5. 把旁白换成原声或沉默：若场景自身更有力量，就让出声音所有权。
6. 检查开头问题是否真实，结尾是否回答或转化它。

只记录并优先修复 1–3 个回报最高的问题；先改结构，再润色句子。

## 6. 评审与校验

### 6.1 建议型语义评审

```bash
python3 scripts/review.py --work-dir <work_dir>
```

评审会自动识别 cut 模式，并在存在已校验剪辑计划时按输出时间线核对；`--timeline source` 可强制使用原片时间。打开 `narration_review.md`，逐项处理 `error`，尤其是 `category=hallucination`。

重复修改并评审，直到：

- `verdict` 为 `PASS` / `OK` 且没有 `error`；或
- 对仍保留的问题做明确 override。

覆盖决定追加到 `work_dir/narration_review_override.md`：

```markdown
### 覆盖记录 — <date>
- 问题：segment 4 / category=hallucination
- 评审意见：“他早已知情”缺少画面/对白依据
- 决定：KEEP — 该事实来自用户提供的当前集背景，而非未来剧情
- 签署：<agent/human>
```

`review.py` 本身只写报告，默认调用策略为建议型、失败开放；若调用方显式开启严格评审，事实矛盾、残句、解析失败或评审不可用可在 TTS 前阻断。覆盖记录只用于审计，`review.py` / `validate.py` 不读取它。

### 6.2 确定性硬校验

```bash
python3 scripts/validate.py --work-dir <work_dir> --mode full
# cut 输出时间线由编排器使用 --mode cut_output
```

命令写出 `narration_lint.json`。full 模式还会根据安静窗口重写 `narration.json` 的时间。修复所有 error 后重复运行，直到校验干净，再继续 TTS 与合成。

片名或题材明确但缺少剧情上下文时，先按本技能的 `references/research-guide.md` 写 `background_research.json`。若理解素材偏薄，brief 中的数量只能当上限：宁可少写、写实，也不要为凑数复述画面。

## 7. 能力边界

- 不运行 ASR / VLM；只消费视频理解索引。
- 不合成 TTS，也不渲染视频。
- 不根据平台分析做优化；先建立内容意图与剪辑一致性。
- `review.py` 不改写 `narration.json`；是否采用严格门禁由调用方决定。
- `validate.py` 不改写文本含义，只检查或对齐时间与安静窗口。
