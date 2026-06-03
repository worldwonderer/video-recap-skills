# 内部配置参考

> 字段定义见 `scripts/config.py: CONFIG`，本文档只记录调参经验。

开发者调参参考。agent 正常使用不需要查看此文件。

## 目录

- [ffmpeg volume 表达式](#ffmpeg-volume-表达式)
- [CONFIG 参数参考](#config-参数参考)
- [Ducking 行为说明](#ducking-行为说明)
- [成片响度归一](#成片响度归一)

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
| `zone_ducking_volume` | 0.12 | zone 模式解说时原声音量 |
| `zone_fade_seconds` | 0.5 | 解说/原声切换淡入淡出时长 |
| `narration_delay_seconds` | 1.5 | 解说延迟放置秒数，让画面先出现再解说 |
| `speech_rate` | 3.5 | TTS 语速（字符/秒） |
| `speech_safety_margin` | 0.85 | 预算保守系数 |
| `fade_ms` | 300 | TTS fade-in/fade-out(ms) |
| `breath_ms` | 250 | 段间呼吸空间(ms)；连续铺底风格用短停顿 |
| `target_segments_per_minute` | 9.6 | 目标解说密度（段/分钟），写入 brief 并由 lint 检查 |
| `min_segments_per_minute` | 6.24 | 低于此密度时 lint 给 low_density 警告 |
| `max_narration_gap_seconds` | 11.0 | 相邻解说段最大间隔；超过给 long_gap 警告 |
| `context_info` | "" | 额外上下文，通过 --context 设置 |
| `silence_noise_threshold` | -25dB | 静音检测噪声阈值 |
| `vlm_workers` | 8 / `VLM_WORKERS` | VLM 并行分析线程数；代理/WAF 超时时设为 1 |
| `tts_workers` | 4 / `TTS_WORKERS` | TTS 并行合成线程数 |
| `tts_timeout` | 90 / `TTS_TIMEOUT` | 单段 TTS 命令超时秒数 |
| `tts_retries` | 3 / `TTS_RETRIES` | 单段 TTS 失败重试次数 |
| `allow_partial_tts` | false / `ALLOW_PARTIAL_TTS` | 部分 TTS 失败时是否继续组装 |
| `skip_narrative_analysis` | true | 跳过叙事结构分析 |
| `api_provider` | openai / mimo | API 兼容提供方；MiMo 使用 `api-key` 头 |
| `api_url` | normalized chat endpoint | 帧级 VLM 的 OpenAI-compatible chat/completions endpoint |
| `mimo_api_url` | normalized chat endpoint | MiMo 共享 endpoint；默认供 video/TTS 复用 |
| `mimo_video_api_url` | normalized chat endpoint | MiMo 视频理解 endpoint；未单独配置时使用 `mimo_api_url` |
| `mimo_tts_api_url` | normalized chat endpoint | MiMo TTS endpoint；未单独配置时使用 `mimo_api_url` |
| `mimo_video_overview` | false | 是否调用 MiMo `video_url` 对 ffmpeg scene 分片生成概览 |
| `mimo_video_chunk_max_seconds` | 20.0 | MiMo scene 分片最长秒数；分片过大或超时时降低 |
| `mimo_video_chunk_min_seconds` | 1.0 | MiMo scene 分片最短尾段秒数；避免产生极短尾段 |
| `mimo_video_chunk_timeout` | 180 | ffmpeg 裁剪单个 MiMo 分片的超时秒数 |
| `mimo_video_base64_max_mb` | 45 | 单个本地视频分片转 data URL 的安全上限，超过则需降低分片时长或 fps |
| `mimo_disable_thinking` | true | MiMo 非 TTS 请求默认添加 `thinking: {type: disabled}`，避免短 max token 只返回推理内容 |
| `mimo_tts_model` | mimo-v2.5-tts | MiMo TTS 模型 |
| `mimo_tts_voice` | 冰糖 | MiMo TTS 音色 |

解说区音量调优：

- **`zone_ducking_volume` (0.12)**：解说时原声压到 12%，非解说时恢复 100%。0.12 是"原声可听到但不干扰解说"的平衡点。如果原声音乐很重要可以调到 0.2-0.3

## Ducking 行为说明

视频组装时根据解说段的 `overlaps_speech` 标记和 `narration_mode` 选择混音策略：

| 场景 | 原声音量 | 说明 |
|------|---------|------|
| Zone 模式，解说时段 | 0.12 | 大幅压低，让解说清晰 |
| Zone 模式，非解说时段 | 1.0 | 原声满音量 |
| Scene 模式，解说在安静窗口 | 0.7 | 轻微压低 |
| Scene 模式，解说与对白重叠 | 0.2 | 大幅压低 |

Zone 模式使用梯形淡入淡出（`zone_fade_seconds`），避免音量突变产生爆音。

## 成片响度归一

ducking 只负责原声与解说的相对平衡，组装末端再做一次整体响度归一（`loudnorm`），统一成片绝对响度，避免输出偏轻。

| 参数 | 默认 | 说明 |
|------|------|------|
| `final_loudnorm` | true | 是否在末端做整体响度归一（`FINAL_LOUDNORM`） |
| `target_lufs` | -14.0 | 目标综合响度 LUFS（`TARGET_LUFS`）；样片约 -11.9，默认取更安全的 -14 |
| `target_true_peak` | -1.0 | 目标真峰值 dBTP（`TARGET_TRUE_PEAK`） |
| `target_lra` | 11.0 | 目标响度范围 LU（`TARGET_LRA`） |
