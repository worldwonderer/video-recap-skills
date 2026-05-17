# 参数参考

## CLI 参数

| 参数 | 说明 | 默认 | 何时调整 |
|------|------|------|----------|
| `video` | 输入视频文件路径 | (必填) | — |
| `--tts` | TTS 引擎: auto / indextts2 / edge-tts / say | auto（优先 edge-tts） | 无网络时用 `say`（macOS 本地）；常规推荐 `edge-tts` |
| `--style` | 解说风格: 短剧 / 电视剧 / 电影 / 纪录片 / 科普视频 | 纪录片 | 按内容类型选择 |
| `--context` | 额外上下文（节目名、角色名等） | "" | 有已知角色名时必填，影响称呼规则 |
| `--model` | 覆盖 VLM/LLM 模型名 | `$OPENAI_MODEL` 或 gpt-4o | 换 VLM 模型时使用 |
| `--agent-mode` | Agent 模式：暂停等 Agent 写解说词 | false | 由 Agent（你）写解说词时必须开启 |
| `--voice` | 覆盖 edge-tts 音色 | `zh-CN-YunxiNeural` | 不满意默认音色时指定 |
| `--scene-threshold` | 场景检测阈值 0.0-1.0 | 0.1 | 场景切碎→调低；场景漏检→调高 |
| `--resume` | 从已有工作目录继续 | - | 中断后续跑时指定 work_dir 路径 |
| `--burn-subtitles` | 烧录字幕到视频（需重编码） | false | 需要内嵌字幕时开启 |
| `--output, -o` | 输出目录 | 视频所在目录/output | 自定义输出位置 |
| `--step` | 仅执行: extract / detect / asr / analyze / script / tts / assemble | 全部 | 调试单步或重跑特定阶段 |
| `--skip-asr` | 跳过 ASR 转录（需已有缓存） | false | 无本地 ASR 或已有 asr_result.json 时使用 |
| `--fps` | 帧提取 fps（0=自动：≤60s→2fps, ≤5min→1.5fps, >5min→1fps） | 0 | 视频细节多时可调高 |
| `--ducking` | 音频 ducking 模式: sidechaincompress / fixed / none | 配置值 `ducking_mode`（当前 fixed） | 解说与原声重叠时的音量压低策略 |
| `--vlm-model` | 单独覆盖 VLM 模型名（优先级高于 --model） | `$OPENAI_MODEL` | VLM 和 LLM 用不同模型时设置 |
| `--llm-model` | 单独覆盖 LLM 模型名（优先级高于 --model） | `$OPENAI_MODEL` | VLM 和 LLM 用不同模型时设置 |
| `--vlm-workers` | VLM 并行线程数 | `$VLM_WORKERS` 或 8 | 代理/WAF 超时（如 524）时设为 1 |
| `--tts-workers` | TTS 并行线程数 | `$TTS_WORKERS` 或 4 | TTS 服务不稳定时调低 |
| `--tts-timeout` | 单段 TTS 超时秒数 | `$TTS_TIMEOUT` 或 90 | 长文本或网络慢时调高 |
| `--allow-partial-tts` | 允许部分 TTS 失败后继续组装 | false | 只想产出可用视频、接受缺少部分解说时开启 |

## 环境变量

| 变量 | 说明 | 默认 | 何时调整 |
|------|------|------|----------|
| `OPENAI_API_KEY` | API 密钥（必填） | - | 必须设置 |
| `OPENAI_API_URL` | API 地址，支持 base URL 或完整 chat/completions 端点 | `https://api.openai.com/v1/chat/completions` | 用代理或自建 VLM 时设置，如 `https://example.com/v1` 会自动补全 `/chat/completions` |
| `OPENAI_MODEL` | VLM/LLM 模型名 | gpt-4o | 换模型时设置，也可用 `--model` 覆盖 |
| `ASR_BIN` | ASR 二进制路径 | local_transcribe (PATH 搜索) | 用自定义 ASR 时指定路径 |
| `ASR_MODEL_DIR` | ASR 模型目录 | (空) | 本地 ASR 需要指定模型目录时设置 |
| `VLM_WORKERS` | VLM 并行线程数 | 8 | 代理/WAF 对并发敏感时设为 1 |
| `TTS_WORKERS` | TTS 并行线程数 | 4 | edge-tts 超时或限流时调低 |
| `TTS_TIMEOUT` | 单段 TTS 命令超时秒数 | 90 | 网络慢时调高 |
| `TTS_RETRIES` | 单段 TTS 重试次数 | 3 | edge-tts 偶发失败时调高 |
| `ALLOW_PARTIAL_TTS` | 部分 TTS 失败是否继续 | false | 调试或应急产出时设为 1 |
