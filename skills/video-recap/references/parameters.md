# 参数参考

## CLI 参数

| 参数 | 说明 | 默认 | 何时调整 |
|------|------|------|----------|
| `video` | 输入视频文件路径 | (必填) | — |
| `--tts` | TTS 引擎: auto / indextts2 / edge-tts / say | auto | 无网络时用 `say`（macOS 本地）；质量优先用 `edge-tts` |
| `--style` | 解说风格: 短剧 / 电视剧 / 电影 / 纪录片 / 科普视频 | 纪录片 | 按内容类型选择 |
| `--context` | 额外上下文（节目名、角色名等） | "" | 有已知角色名时必填，影响称呼规则 |
| `--model` | 覆盖 VLM/LLM 模型名 | `$OPENAI_MODEL` 或 gpt-4o | 换 VLM 模型时使用 |
| `--agent-mode` | Agent 模式：暂停等 Agent 写解说词 | false | 由 Agent（你）写解说词时必须开启 |
| `--voice` | 覆盖 edge-tts 音色 | 按 style 自动选择 | 不满意默认音色时指定 |
| `--scene-threshold` | 场景检测阈值 0.0-1.0 | 0.1 | 场景切碎→调低；场景漏检→调高 |
| `--resume` | 从已有工作目录继续 | - | 中断后续跑时指定 work_dir 路径 |
| `--burn-subtitles` | 烧录字幕到视频（需重编码） | false | 需要内嵌字幕时开启 |
| `--output, -o` | 输出目录 | 视频所在目录/output | 自定义输出位置 |
| `--step` | 仅执行: extract / detect / asr / analyze / script / tts / assemble | 全部 | 调试单步或重跑特定阶段 |
| `--skip-asr` | 跳过 ASR 转录（需已有缓存） | false | 无本地 ASR 或已有 asr_result.json 时使用 |
| `--fps` | 帧提取 fps（0=自动：≤60s→2fps, ≤5min→1.5fps, >5min→1fps） | 0 | 视频细节多时可调高 |
| `--ducking` | 音频 ducking 模式: sidechaincompress / fixed / none | sidechaincompress | 解说与原声重叠时的音量压低策略 |
| `--vlm-model` | 单独覆盖 VLM 模型名（优先级高于 --model） | `$OPENAI_MODEL` | VLM 和 LLM 用不同模型时设置 |
| `--llm-model` | 单独覆盖 LLM 模型名（优先级高于 --model） | `$OPENAI_MODEL` | VLM 和 LLM 用不同模型时设置 |

## 环境变量

| 变量 | 说明 | 默认 | 何时调整 |
|------|------|------|----------|
| `OPENAI_API_KEY` | API 密钥（必填） | - | 必须设置 |
| `OPENAI_API_URL` | API 端点地址 | `https://api.openai.com/v1/chat/completions` | 用代理或自建 VLM 时设置 |
| `OPENAI_MODEL` | VLM/LLM 模型名 | gpt-4o | 换模型时设置，也可用 `--model` 覆盖 |
| `ASR_BIN` | ASR 二进制路径 | local_transcribe (PATH 搜索) | 用自定义 ASR 时指定路径 |
| `ASR_MODEL_DIR` | ASR 模型目录 | (空) | 本地 ASR 需要指定模型目录时设置 |
