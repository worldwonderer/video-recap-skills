---
name: video-voiceover
user-invocable: false
description: >
 把带时间戳的 narration.json 合成为中文解说音频。使用 MiMo TTS（mimo-v2.5-tts）逐段生成语音，
 按时间窗动态适配语速并处理响度；输入输出时间线上的旁白，产出 tts_segments 与 tts_meta.json。
 旧版直接剪辑路径也可显式传入 narration_mapped.json。触发词：配音、语音合成、TTS、解说配音、
 voiceover、text to speech、旁白配音。
---

## 1. 定位

本技能读取带时间戳的旁白稿，为每一段生成独立音频，并把语音适配到对应时间窗，随后记录下游合成所需的放置元数据。
当前唯一引擎是 MiMo TTS（`mimo-v2.5-tts`）。

## 2. 环境要求

```bash
export MIMO_API_KEY=***  # 也可使用仅供 TTS 的 MIMO_TTS_API_KEY
```

下面的 `scripts/...` 均相对于本技能目录。若执行器从仓库根目录启动，请给脚本路径加上本技能的绝对目录。
脚本不从其他技能目录读取文件；外部输入仅限命令显式传入的稿件、音频、参数与 `work_dir` 产物。

## 3. 输入契约

默认输入为 `work_dir/narration.json`。每段必须包含 `start`、`end` 与 `narration`，可选字段包括
`pause_after_ms` 和 `overlaps_speech`。时间统一表示音频最终放置的**输出时间线秒数**。

编排式 cut 流程直接使用输出时间的 `narration.json`。只有旧版直接剪辑路径需要显式传入
`narration_mapped.json`。

## 4. 运行命令

```bash
python3 scripts/voiceover.py --work-dir <work_dir> --narration <narration.json> \
  [--mimo-voice 冰糖 | --voice-ref <reference-audio>]
```

单独运行且省略 `--narration` 时，默认读取 `work_dir/narration.json`。旧版路径如需映射后的稿件，必须显式传入：

```bash
python3 scripts/voiceover.py --work-dir <work_dir> \
  --narration <work_dir/narration_mapped.json>
```

## 5. 输出契约

- `tts_segments/*.wav`：每段旁白对应一个音频文件。
- `tts_meta.json`：包含 `segments`、`engine` 与 `narration`。每段记录 `audio_path`、时间、
  `pause_after_ms` 和放置字段。
- 干净运行写入 `partial: false` 与 `failures: []`。
- 使用 `--allow-partial-tts` 跳过失败段时，写入 `partial: true` 和
  `failures: [{index,start,end,text,error}]`，让缺失语音保持可见。

## 6. 运行规则

- 重跑只复用内容与 TTS 设置均匹配的分段音频；修改旁白或合成参数后，只重生成受影响的 WAV。
- `--voice-ref` 仅用于 full/cut 解说克隆，切换到 `mimo-v2.5-tts-voiceclone`。仅在确需新合成时惰性规范化一次；
- dub voiceclone 原始 WAV 也会用模型、提示、台词和参考音频指纹缓存；匹配重跑不再重复请求或计费，`dub_manifest.json` 逐行记录 `tts_cache=hit|miss`；
  参考音频内容或预处理指纹变化会使旧缓存失效。仅在获得授权后使用，参考音频会发送到 MiMo。
- `TTS_WORKERS`、`TTS_TIMEOUT`、`TTS_RETRIES`、`ALLOW_PARTIAL_TTS` 用于调整并发、超时、重试与部分成功策略。
- dub 模式有独立的确定性门禁：`dub_lint.json` 会在语音克隆前阻止空行、重叠或越界译文；
  `dub_review.json` 用于记录忠实度、语气、时长和平台适配复核。可通过
  `dub.py --stage lint|review` 或 `dub.py --print-schema` 单独调用。

## 7. 能力边界

- 不撰写或修改旁白文本。
- 不混流、不压低原声、不渲染字幕。
- 不分析视频，也不选择时间点；只为输入稿件中的既定分段配音。
