# 内部配置参考

开发者调参参考。agent 正常使用不需要查看此文件。

## 目录

- [ffmpeg volume 表达式](#ffmpeg-volume-表达式)
- [CONFIG 参数参考](#config-参数参考)
- [Zone 参数调优](#zone-参数调优)
- [Ducking 行为说明](#ducking-行为说明)

## ffmpeg volume 表达式

zone ducking 使用 ffmpeg volume 滤镜做动态音量控制。关键技术点：
- **必须用单引号包裹表达式**：`volume='if(between(t,0,12),0.12,1.0)':eval=frame`
- **必须加 `:eval=frame`**：否则 `t` 只在开头求值一次，`between(t,0,12)` 永远返回 1
- **不要用 `\,` 转义逗号**：ffmpeg 的 `\,` 在函数参数内行为不可靠，用单引号保护整个表达式
- **平滑过渡用梯形曲线**：`min(1,max(0,min(t-s,e-t)/fade))` 替代 `if(between())` 的硬切换

## CONFIG 参数参考

脚本顶部 `CONFIG` 字典控制所有行为：

| 参数 | 默认 | 说明 |
|------|------|------|
| `narration_mode` | zone | 解说模式: zone（大段+原声交替）/ scene（逐场景） |
| `zone_min_duration` | 6.0 | 解说区最短秒数 |
| `zone_merge_gap` | 3.0 | 相邻安静窗口合并间隔 |
| `zone_ducking_volume` | 0.12 | zone 模式解说时原声音量 |
| `zone_fade_seconds` | 0.5 | 解说/原声切换淡入淡出时长 |
| `narration_delay_seconds` | 6.0 | 解说延迟放置秒数，让画面先出现再解说 |
| `speech_rate` | 3.5 | TTS 语速（字符/秒） |
| `speech_safety_margin` | 0.85 | 预算保守系数 |
| `fade_ms` | 300 | TTS fade-in/fade-out(ms) |
| `breath_ms` | 600 | 段间呼吸空间(ms) |
| `fill_thresholds` | [0.40, 0.60] | 覆盖率补充阈值 |
| `context_info` | "" | 额外上下文，通过 --context 设置 |
| `silence_noise_threshold` | -25dB | 静音检测噪声阈值 |
| `vlm_workers` | 8 | VLM 并行分析线程数 |
| `tts_workers` | 4 | TTS 并行合成线程数 |
| `skip_narrative_analysis` | true | 跳过叙事结构分析 |

### Phase 1-3 新增参数

| 参数 | 默认 | 说明 |
|------|------|------|
| `vlm_frame_facts` | false | 启用帧级动作描述（Phase 1）。VLM 逐帧输出动作短句，注入解说 prompt |
| `vlm_max_tokens` | 800 | VLM 最大输出 token。500=原始, 800=含帧标签 |
| `narration_auto_rewrite` | false | 启用关键词检测+自动重写（Phase 2）。检测解说与画面对齐度，不匹配时自动约束重写 |
| `asr_temporal_annotation` | false | 启用 ASR 模糊时间标注（Phase 3）。利用帧动作描述中的字幕/对白信息为 ASR 文本添加时间分段 |

**依赖关系**:
- Phase 2 (`narration_auto_rewrite`) 依赖 Phase 1 (`vlm_frame_facts`)。frame_facts 不可用时自动跳过
- Phase 3 (`asr_temporal_annotation`) 依赖 Phase 1 (`vlm_frame_facts`)。无帧级字幕信息时回退到原有分配
- 所有新功能默认关闭，可独立开关回退

Zone 模式通过 `zone_merge_gap` 和 `zone_min_duration` 控制解说区的划分：

- **`zone_merge_gap` (3.0s)**：两个安静窗口间隔小于 3 秒时合并为一个大解说区。调大→更多合并→解说区更少但更长；调小→解说区更多更碎
- **`zone_min_duration` (6.0s)**：合并后短于 6 秒的解说区会被丢弃。这个值决定了解说区的最低质量门槛——太短的区域放不下有意义的解说（6s 约 18 字）
- **经验值**：对话密集的视频安静窗口少且短，可能需要降低 `zone_min_duration` 到 4.0；独白/纪录片类视频安静窗口多，默认值即可
- **`zone_ducking_volume` (0.12)**：解说时原声压到 12%，非解说时恢复 100%。0.12 是测试中"原声可听到但不干扰解说"的平衡点。如果原声音乐很重要可以调到 0.2-0.3

## Ducking 行为说明

视频组装时根据解说段的 `overlaps_speech` 标记和 `narration_mode` 选择混音策略：

| 场景 | 原声音量 | 说明 |
|------|---------|------|
| Zone 模式，解说时段 | 0.12 | 大幅压低，让解说清晰 |
| Zone 模式，非解说时段 | 1.0 | 原声满音量 |
| Scene 模式，解说在安静窗口 | 0.7 | 轻微压低 |
| Scene 模式，解说与对白重叠 | 0.2 | 大幅压低 |

Zone 模式使用梯形淡入淡出（`zone_fade_seconds`），避免音量突变产生爆音。
