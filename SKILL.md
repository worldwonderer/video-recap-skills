---
name: video-recap
description: >
  视频自动解说 skill。输入视频，输出带中文旁白的解说视频。
  使用场景：(1) 为视频生成中文解说旁白 (2) 视频摘要或解说 (3) 制作 recap 视频。
  触发词: video-recap, 视频解说, 视频旁白, 视频recap, 生成解说, recap
---

## 两种模式

### Auto 模式（默认）

Pipeline 全自动运行：帧提取 → 场景检测 → ASR → 静音检测 → VLM 分析 → **LLM 自动生成解说词** → TTS 合成 → 视频组装。

```bash
OPENAI_API_KEY=xxx OPENAI_API_URL=xxx \
  python3 scripts/video_recap.py <video> \
  --tts edge-tts --style 纪录片 --context "背景信息"
```

### Agent 模式（`--agent-mode`）

Pipeline 在 VLM 分析完成后**暂停**，等待 Agent 亲自撰写解说词，然后手动继续 TTS + 组装。

**解说词必须由 agent（你）亲自撰写，不要调用 LLM API 来生成解说词。**

```bash
# 1. 运行前置 pipeline（会在 Step 5 前暂停）
OPENAI_API_KEY=xxx OPENAI_API_URL=xxx \
  python3 scripts/video_recap.py <video> \
  --agent-mode --tts edge-tts --context "背景信息"

# 2. Agent 写解说词
#    读取 vlm_analysis.json、asr_result.json、silence_periods.json
#    写入 work_dir/narration.json：
#    [{"start": 秒数, "end": 秒数, "narration": "解说文本", "pause_after_ms": 600, "overlaps_speech": false}]

# 3. 标记完成 + 清理缓存 + 重新跑 TTS
touch work_dir/.step_script.done
rm -rf work_dir/tts_segments/ work_dir/.step_tts.done work_dir/.step_assemble.done work_dir/tts_meta.json
python3 scripts/video_recap.py <video> --resume work_dir
```

**每次改完 narration.json 后必须删 `tts_segments/`**，否则会复用旧音频。

## 解说词写作要求（Agent 模式）

- **禁止看图说话**：观众看得见画面，不要描述动作和表情
- **讲故事**：揭示角色意图、潜台词、关系动态
- **大段解说 + 原声交替**：解说区集中在安静窗口，原声区保留原始对白
- **字数预算**：每段字数 ≤ (end - start - 0.6) × 3，超限会被截断
- 如果提供了 `--context`，使用角色名（如 Big、凯莉）

## 参数

| 参数 | 说明 | 默认 |
|------|------|------|
| `video` | 输入视频文件路径 | (必填) |
| `--tts` | TTS 引擎: auto/indextts2/edge-tts/say | auto |
| `--style` | 解说风格: 短剧/电视剧/电影/纪录片/科普视频 | 纪录片 |
| `--context` | 额外上下文（节目名、角色名等） | "" |
| `--model` | 覆盖 VLM/LLM 模型名 | OPENAI_MODEL 环境变量或 gpt-4o |
| `--agent-mode` | Agent 模式：暂停等 Agent 写解说词 | false |
| `--voice` | 覆盖 edge-tts 音色 | 按 style 自动选择 |
| `--scene-threshold` | 场景检测阈值 0.0-1.0 | 0.1 |
| `--resume` | 从已有工作目录继续 | - |
| `--burn-subtitles` | 烧录字幕到视频（需重编码） | false |
| `--output, -o` | 输出目录 | 视频所在目录/output |
| `--step` | 仅执行: extract/detect/asr/analyze/script/tts/assemble | 全部 |

## 环境变量

| 变量 | 说明 | 默认 |
|------|------|------|
| `OPENAI_API_KEY` | API 密钥（必填） | - |
| `OPENAI_API_URL` | API 端点地址 | https://api.openai.com/v1/chat/completions |
| `OPENAI_MODEL` | VLM/LLM 模型名 | gpt-4o |
| `ASR_BIN` | ASR 二进制路径 | local_transcribe (PATH 搜索) |
| `ASR_MODEL_DIR` | ASR 模型目录 | (空) |

## Pipeline 恢复

Pipeline 用 `.step_<name>.done` 标记文件控制跳过：

| 删哪些文件 | 重跑什么 |
|---|---|
| `.step_tts.done` + `tts_meta.json` | 重跑 TTS |
| `.step_tts.done` + `tts_meta.json` + `tts_segments/` | 强制重新合成所有 TTS |
| `.step_assemble.done` | 重跑视频组装 |

**快速 resume**（只改了某段）：删 `tts_segments/narr_00N.wav` + `.step_tts.done` + `tts_meta.json`，然后 `--resume`。

## 输出

- `recap_<视频名>.mp4` — 最终视频
- `subtitles.srt` — SRT 字幕
- 工作目录保留中间文件：`vlm_analysis.json`、`silence_periods.json`、`narration.json` 等

## 参考

- 解说词详细规则和风格模板见 [references/prompt-templates.md](references/prompt-templates.md)
- 用户要求调整 CONFIG 参数、Zone/Ducking 行为时，参考 [references/internal-config.md](references/internal-config.md)
