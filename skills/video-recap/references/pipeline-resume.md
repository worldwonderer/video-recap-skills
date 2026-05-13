# Pipeline 恢复

Pipeline 用 `.step_<name>.done` 标记文件控制跳过已完成的步骤。

## 标记文件对应步骤

| 标记文件 | 步骤 |
|----------|------|
| `.step_extract.done` | 帧提取 |
| `.step_detect.done` | 场景检测 |
| `.step_asr.done` | ASR 转录 |
| `.step_silence.done` | 静音检测 + 解说区识别 |
| `.step_vlm.done` | VLM 视觉分析 |
| `.step_narrative.done` | 叙事结构分析（可跳过） |
| `.step_script.done` | 解说词撰写 |
| `.step_tts.done` | TTS 合成 |
| `.step_assemble.done` | 视频组装 |

## 常见恢复配方

### 1. 只改了解说词（narration.json）

```bash
# 删 TTS 缓存，重新合成
rm -rf work_dir/tts_segments/ work_dir/.step_tts.done \
  work_dir/.step_assemble.done work_dir/tts_meta.json
python3 scripts/video_recap.py <video> --resume work_dir
```

### 2. 换音色（--voice）

```bash
rm -rf work_dir/tts_segments/ work_dir/.step_tts.done \
  work_dir/.step_assemble.done work_dir/tts_meta.json
python3 scripts/video_recap.py <video> --resume work_dir --voice zh-CN-YunxiNeural
```

### 3. 改字幕样式或重烧录

```bash
rm work_dir/.step_assemble.done
python3 scripts/video_recap.py <video> --resume work_dir --burn-subtitles
```

### 4. 换 VLM 模型重分析

```bash
rm work_dir/.step_vlm.done work_dir/.step_narrative.done work_dir/.step_script.done \
  work_dir/tts_segments/ work_dir/.step_tts.done \
  work_dir/.step_assemble.done work_dir/tts_meta.json
OPENAI_MODEL=新模型 python3 scripts/video_recap.py <video> --resume work_dir --agent-mode
```

### 5. 完全重来

```bash
rm -rf work_dir/
python3 scripts/video_recap.py <video> --agent-mode --tts edge-tts --context "背景"
```

## 快速 resume（只改了某段）

删 `tts_segments/narr_00N.wav` + `.step_tts.done` + `tts_meta.json`，然后 `--resume`。
只重合成被删的段，其余保留。
