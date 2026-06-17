---
name: video-script
description: >
 Write a timestamped Chinese narration script (解说词 / 旁白) for an already-analyzed video,
 then lint/validate it. Use after video-understanding has produced agent_narration_brief.md +
 vlm_analysis.json, when you need to author the recap narration (style, anti-hallucination,
 字数公式, density, hook/throughline). Input: the understanding index in work_dir. Output:
 narration.json (validated). 触发词: 解说词, 写解说, 视频旁白, narration script, 写稿, 解说文案.
---

## What this does

Authoring + validation of the narration script. The **agent writes `work_dir/narration.json`**
following the rules below; then `validate.py` lints it against the understanding index, and in
full mode time-aligns it to quiet windows.

## Step 1 — read the brief

Read `work_dir/agent_narration_brief.md` (scenes, durations, quiet windows, char budget) first.
Digest long dialogue via `asr_writing_chunks.json`; judge "is there speech/a silent slot here?"
via `timeline_fusion.json`. Check raw `vlm_analysis.json` / `asr_result.json` for details.
All timestamps are **original-video time**.

## Step 2 — write narration.json

```json
[
  {"start": 5.0, "end": 12.0, "narration": "解说文本。", "pause_after_ms": 250, "overlaps_speech": true}
]
```

| Field | Meaning |
|------|------|
| `start` / `end` | narration start/end seconds (original-video time) |
| `narration` | narration text |
| `pause_after_ms` | pause after segment, default 250 (keeps a tight rhythm) |
| `overlaps_speech` | overlaps original dialogue; default `true` for continuous-bed style, `false` only in true silence |

Optionally also author `original_subtitles.json` — `[{start,end,text}]` (OUTPUT time) — the calibrated
original dialogue burned during the original-audio gaps (ASR errors/names fixed, only what is actually
spoken there). Rendered in `「」` to set it apart from narration. If omitted, assemble falls back to a
conservative auto-ASR mapping. See the brief's `原声留白字幕` section.

### 写作规则（连续原声铺底的高密度 recap）

1. **连续解说**：沿整条时间轴用短促 beat 连续解说，原声作为压低的背景一直存在。
2. **达到密度目标**：按 brief 头部给出的目标（约 9.6 段/分钟，最低 6.24），相邻 beat 间隔不要超过 11 秒。
3. **默认重叠原声**：`overlaps_speech` 默认 true；只有刻意放进真正静音空档的 beat 才设 false。
4. **每段短小**：约一句短句（1-2 行字幕），宁短不长。字数 ≤ `(end - start - 0.25) × 3`。
5. **不要看图说话**：观众看得见动作表情，解说讲动机、关系、潜台词和剧情意义（基于画面**可见的证据**，不要编造）。
6. **用已知角色名**：`--context` 或 `background_research.json` 给出角色名时优先使用。
7. **完整句子**：以句号 / 问号 / 感叹号结束，不写半句。

### 解说手法（区分“解说”和“字幕”）

- **钩子**：开头 1-2 个 beat 制造悬念或利害，不是交代场景。
- **主线**：选一条主线（目标 / 关系 / 悬念），每个 beat 都推进它。
- **递进**：信息和张力逐步升级。**悬念缺口**：提前埋后果，后面回收。
- **收尾**：最后 1-2 个 beat 给出结果或反转，不要泛泛收场。
- **衔接**：解说块和它旁边的原声留白是同一个 beat——块尾给原声铺垫，下一块承接原声刚呈现的内容，别和原声各说各的。
- **给信息而非念画面**；**去废词**：用具体名词动词，删空泛形容。

## Step 2.5 — review GATE (advisory, logged, overridable)

A separate **quality** pass (LLM-as-judge), distinct from the mechanical lint below. Needs the chat API key (same as VLM).

1. Run: `python3 scripts/review.py --work-dir <work_dir>`
2. Open `narration_review.md`. For every `error` finding (ESPECIALLY `category=hallucination` — a claim
   not grounded in the visual/ASR evidence), revise `narration.json` and re-run review until either:
   - (a) verdict == `OK` with zero `error` findings, OR
   - (b) you consciously **OVERRIDE** a remaining finding (next step).
3. To OVERRIDE: append a block to `work_dir/narration_review_override.md` naming WHICH finding
   (segment + category), WHY it is acceptable, and who signed off. Unaddressed `error` findings with
   no override entry mean the draft is **NOT ready**.
4. Only then proceed to Step 3 (`validate.py` — the hard gate).

**GATE rule:** review NEVER blocks the tooling (it leans on a flaky chat API and a re-render is cheap).
`validate.py` is the deterministic hard gate. The override log makes "we saw the finding and chose to
ship it" auditable — review.py / validate.py never read it; it is a record for the human in the loop.

Override block shape — `work_dir/narration_review_override.md` (append-only):

```
## Override — <date>
- Finding: segment 4 / category=hallucination
- Reviewer said: "‘他早已知情’无画面/对白依据"
- Decision: KEEP — grounded in the --context synopsis (s2 reveal); reviewer lacked that context.
- Signed: <agent/human>
```

## Step 3 — validate

```bash
python3 scripts/validate.py --work-dir <work_dir> --mode full   # or --mode cut
```

Writes `narration_lint.json`; in `full` mode rewrites `narration.json` with quiet-window alignment.
Fix any lint errors and re-run until clean.

## Cut mode (long video → short recap)

Before narration, write `work_dir/clip_plan.json` (original-time source ranges to keep), optionally
self-review it in `clip_plan_review.md` (agent-only; the tooling does not read it), then write
`narration.json` with timestamps that fall inside the kept clips. The video-cut skill maps both to
the shortened timeline. Validate with `--mode cut`.

```json
{"target_duration": "10m", "clips": [{"start": 12.0, "end": 38.0, "reason": "冲突开端"}]}
```

**剪辑模式写作要点（解说要对上剪后的画面，不是原片）：**
- **按成片时长写**：解说的 beat 数量对齐**目标成片**（brief 头部已按目标时长算好），不是原片时长。中点落在任何保留片段之外的 beat 会被丢弃——别在被剪掉的段落上写解说。
- **每段落在单个片段内**：一个 beat 的 `[start, end]` 不要跨片段边界，否则会被裁到片段长度，配音就会念到剪掉的画面（`--mode cut` 会以 `crosses_clip_boundary` 警告）。
- **按成片顺序讲**：beat 按它们所属片段在成片里出现的先后排序，让解说在剪后画面上连成一条线。
- **重复源区间**：clips 复用了重叠的源区间时，给 beat 标 `source_clip_id`，确保映射到正确片段。

> 片名/题材明确但缺乏剧情上下文时，先按 [背景调研指南](../video-understanding/references/research-guide.md) 写 `background_research.json` 再写解说——否则解说只能"看图说话"。brief 在 substrate 偏薄时会把密度目标降为上限而非配额：宁可少写、写实，也不要为凑数堆画面描述。

## What this skill does NOT do
- Does NOT run ASR/VLM or analyze the video — it consumes the understanding index.
- Does NOT synthesize audio or render video.
- `review.py` does NOT edit narration.json and does NOT block the pipeline — it is advisory.
- `validate.py` does NOT rewrite the meaning of the text — it only checks/aligns timing and quiet windows.
