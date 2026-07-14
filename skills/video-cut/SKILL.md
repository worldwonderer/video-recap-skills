---
name: video-cut
user-invocable: false
description: >
 把长视频按 Agent 选择的原片区间剪成短片。作为两阶段创作流程中的剪辑环节，读取 clip_plan.json 与源视频，
 输出 edited_source.mp4；随后 Agent 按输出时间线写 narration.json。单独调用且未传 --no-narration-map 时，
 仍支持旧版单阶段路径，把原片时间的 narration.json 映射为 narration_mapped.json。
 触发词：视频剪辑、剪辑式解说、video cut、clip plan、拼剪。
---

## 1. 定位

本技能只执行 Agent 已经做出的剪辑决定：

1. 校验并补全 `clip_plan.json`，写出带 `clip_id`、原片/输出时间与时长的 `clip_plan_validated.json`。
2. 先把边界吸附到自然停顿，再避开原片硬切附近的闪帧风险。
3. 拼接选定区间，输出 `edited_source.mp4`。
4. 编排流程默认到此停止，由 Agent 按真实输出时间线写 `narration.json`。
5. 旧版单阶段路径还会把原片时间的旁白映射为 `narration_mapped.json`。

相同输入会得到相同输出。缓存仅表示：当 `edited_source.mp4` 新于 `clip_plan.json` 时复用成片。

## 2. 输入契约

`work_dir/clip_plan.json` 可以是数组，也可以是 `{"clips": [...]}`：

```json
{"start": 12.0, "end": 28.5, "reason": "b02 | turn | power: A→B | POV=女主 | 保留反应 | 入点=问题落下 | 出点=沉默结束"}
```

- `start` / `end` 是原片秒数；也接受 `source_start` / `source_end` 或 `in` / `out`。
- 顶层可选 `target_duration`，例如 `"10m"`。
- 多视频项目的每个片段还必须填写 `source_id`。

`work_dir/narration.json` 只在旧版单阶段路径中可选读取；该路径要求旁白使用原片时间。若允许重复或重叠片段，旁白可带 `source_clip_id` 消歧。

## 3. 剪辑意图契约

工具不会替 Agent 做创作选择。写片段前先完成本节的剪辑意图检查，并让每个区间映射到 `recap_story_plan.json` 的一个 beat。

使用现有自由文本 `reason` 保存简洁决定：

```text
beat_id | function | change | POV | preferred moment | 入点 reason | 出点 reason
```

不要因为“事件重要”就保留整段；要保留最能让 change 成立的具体表演、反应、动作或揭示。理解与情绪允许时晚进早出，同时保证台词、动作和技术边界完整。

下面的 `scripts/...` 均相对于本技能目录。若执行器从仓库根目录启动，请给脚本路径加上本技能的绝对目录。脚本不从其他技能目录读取文件；外部输入仅限命令显式传入的视频、参数与 `work_dir` 产物。

## 4. 运行命令

```bash
python3 scripts/cut.py <video> --work-dir <work_dir> \
  [--target-duration 10m] [--clip-padding 0] [--allow-overlap]
```

## 5. 输出契约

- `clip_plan_validated.json`：标准化片段，包含 `clip_id`、`source_start/end`、`output_start/end` 与 `duration`。
- `edited_source.mp4`：按计划拼接后的短视频。
- `narration_mapped.json`：仅旧版单阶段路径生成；编排流程使用 `--no-narration-map`，不会生成该文件。

编排流程下游把 `edited_source.mp4` 当作视频，把 Agent 按输出时间写的 `narration.json` 当作旁白。

## 6. 边界与时间线规则

- 旧版路径中，`clip_plan.json` 与 `narration.json` 都使用原片时间；本工具负责原片 → 输出映射。
- 编排路径中，`narration.json` 直接使用剪后输出时间，不再映射。
- 默认禁止重叠或重复原片区间；`--allow-overlap` 开启后，旁白应填写 `source_clip_id`。
- 旧版映射时，不在任何保留片段内的旁白会被丢弃并记录日志。
- `SCENE_CUT_SNAP` 默认开启：自然停顿吸附后，若边界落在原片硬切附近，source start 会向后、source end 会向前吸附到切点，避免相邻镜头闪一下又切走。默认范围为 `SCENE_CUT_SNAP_MARGIN=0.5` 秒，检测阈值为 `SCENE_CUT_DETECT_THRESHOLD=0.4`。若吸附会把片段压到约 0.5 秒以下则跳过；设 `SCENE_CUT_SNAP=0` 可关闭。

## 7. 能力边界

- 不重新转写或分析视频。
- 不写旁白，也不替 Agent 选择片段；只消费 `clip_plan.json`。
- 除拼接与时间映射所需处理外，不额外重编码。
