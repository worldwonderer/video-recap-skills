# 参数参考

## CLI 参数

| 参数 | 说明 | 默认 | 何时调整 |
|------|------|------|----------|
| `video` | 输入视频文件路径 | (必填，`--doctor` 除外) | — |
| `--tts` | TTS 引擎: auto / indextts2 / edge-tts / say | auto（优先 edge-tts） | 常规推荐 `edge-tts`；无网络时用 macOS `say` |
| `--context` | 额外上下文（节目名、角色名等） | "" | 有已知角色名时填写 |
| `--model` | 覆盖 VLM 模型名 | `$OPENAI_MODEL` 或 `doubao-seed-2-0-lite-260428` | 换 VLM 模型时使用 |
| `--vlm-model` | 单独覆盖 VLM 模型名（优先级高于 `--model`） | `$OPENAI_MODEL` | 临时指定视觉分析模型 |
| `--voice` | 覆盖 edge-tts 音色 | `zh-CN-YunxiNeural` | 不满意默认音色时指定 |
| `--scene-threshold` | 场景检测阈值 0.0-1.0 | 0.1 | 场景切碎→调低；场景漏检→调高 |
| `--resume` | 从已有工作目录继续 | - | 写好 `narration.json` 后续跑 |
| `--burn-subtitles` | 压制解说字幕到视频（需重编码） | false | 需要内嵌字幕时开启；仍会导出 `subtitles.srt`；要求 ffmpeg 带 `subtitles`/libass 滤镜 |
| `--output, -o` | 输出目录 | 视频所在目录/output | 自定义输出位置 |
| `--step` | 仅执行: extract / detect / asr / analyze / script / tts / assemble | 全部 | 调试单步或重跑特定阶段；script 会验证已有 `narration.json` 并写 `narration_lint.json` |
| `--skip-asr` | 跳过 ASR 转录 | false | 无本地 ASR 或已有 `asr_result.json` 时使用 |
| `--fps` | 帧提取 fps（0=自动：≤60s→2fps, ≤5min→1.5fps, >5min→1fps） | 0 | 视频细节多时可调高 |
| `--ducking` | 音频 ducking 模式: sidechaincompress / fixed / none | 配置值 `ducking_mode`（当前 fixed） | 解说与原声重叠时的音量压低策略 |
| `--vlm-workers` | VLM 并行线程数 | `$VLM_WORKERS` 或 8 | 代理/WAF 超时（如 524）时设为 1 |
| `--tts-workers` | TTS 并行线程数 | `$TTS_WORKERS` 或 4 | TTS 服务不稳定时调低 |
| `--tts-timeout` | 单段 TTS 超时秒数 | `$TTS_TIMEOUT` 或 90 | 长文本或网络慢时调高 |
| `--allow-partial-tts` | 允许部分 TTS 失败后继续组装 | false | 应急产出时开启 |
| `--edit-mode` | 成片模式: full / cut | full | `cut` 表示 Agent 选择画面片段，先剪短再解说 |
| `--target-duration` | cut 模式目标成片时长，如 `600` / `10m` / `00:10:00` | - | 作为选片规划目标；超出较多会警告 |
| `--clip-padding` | cut 模式每个片段两端扩展秒数 | 0 | 片段切得太紧时加 0.5-1.0 |
| `--allow-clip-overlap` | cut 模式允许重复/重叠使用原片 | false | 少数需要重复画面时开启；对应解说建议写 `source_clip_id` |
| `--doctor` | 检查运行依赖和配置 | false | 首次安装或排查环境时使用；包含 ffmpeg 字幕滤镜、ASR、API、TTS 状态 |
| `--doctor-tts-smoke` | doctor 时试合成一小段 edge-tts | false | 验证 TTS 网络/音色是否可用 |

## 环境变量

| 变量 | 说明 | 默认 | 何时调整 |
|------|------|------|----------|
| `OPENAI_API_KEY` | API 密钥（VLM 分析必填） | - | 必须设置 |
| `OPENAI_API_URL` | API 地址，支持 base URL 或完整 chat/completions 端点 | `https://api.openai.com/v1/chat/completions` | 用代理或自建 VLM 时设置，如 `https://example.com/v1` 会自动补全 `/chat/completions` |
| `OPENAI_MODEL` | VLM 模型名 | `doubao-seed-2-0-lite-260428` | 换模型时设置，也可用 `--model` 覆盖 |
| `ASR_BIN` | ASR 二进制路径 | local_transcribe (PATH 搜索) | 用自定义 ASR 时指定路径 |
| `ASR_MODEL_DIR` | ASR 模型目录 | (空) | 本地 ASR 需要指定模型目录时设置 |
| `VLM_WORKERS` | VLM 并行线程数 | 8 | 代理/WAF 对并发敏感时设为 1 |
| `TTS_WORKERS` | TTS 并行线程数 | 4 | edge-tts 超时或限流时调低 |
| `TTS_TIMEOUT` | 单段 TTS 命令超时秒数 | 90 | 网络慢时调高 |
| `TTS_RETRIES` | 单段 TTS 重试次数 | 3 | edge-tts 偶发失败时调高 |
| `ALLOW_PARTIAL_TTS` | 部分 TTS 失败是否继续 | false | 调试或应急产出时设为 1 |
| `EDIT_MODE` | 默认成片模式: full / cut | full | 常用剪辑式解说时可设为 `cut` |
| `TARGET_DURATION` | cut 模式默认目标成片时长 | - | 例如 `10m` |
| `CLIP_PADDING` | cut 模式默认片段 padding 秒数 | 0 | 例如 `0.5` |
| `ALLOW_CLIP_OVERLAP` | cut 模式是否允许重复/重叠使用原片 | false | 重复画面需要明确映射时设为 1 |
| `FORCE_VIDEO_REENCODE` | 组装阶段是否强制重编码视频 | false | 输出容器时间戳异常时设为 1 |
| `SUBTITLE_FONT_NAME` | 压制字幕字体名 | Arial | 需要指定本机字体时调整 |
| `SUBTITLE_FONT_SIZE` | 压制字幕字号 | 42 | 字幕过大/过小时调整 |
| `SUBTITLE_MARGIN_V` | 压制字幕底部边距 | 48 | 字幕位置太低/太高时调整 |
| `SUBTITLE_MAX_CHARS` | 字幕单行换行字数 | 20 | 字幕过长时调小 |
