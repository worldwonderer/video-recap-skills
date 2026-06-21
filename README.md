# video-recap-skills

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Claude Code Plugin](https://img.shields.io/badge/Claude%20Code-Plugin-purple)
![Powered by Xiaomi MiMo](https://img.shields.io/badge/AI-Xiaomi%20MiMo-green)

中文 · [English](README.en.md)

**在 claude code 仅需一句话把视频剪辑成解说视频。** 本地只要 `ffmpeg` 加小米 MiMo Token Plan 的 API Key，不用 GPU、不用下载模型，macOS / Linux / Windows 均可运行。

## 演示

<video src="https://github.com/user-attachments/assets/aa96bd1d-ce4b-42bd-a7df-439aeb63dd18" width="640" controls></video>

成片之外，还能一键导出**剪映草稿**手动精修，原片、解说、BGM、字幕：

<img alt="导出的剪映草稿：原片、解说、BGM、字幕" src="docs/jianying-export.png" width="100%">

## 这是什么

```mermaid
flowchart LR
    research["背景调研"] --> understand
    video(["视频"]) --> understand["理解<br/>场景·ASR·VLM"] --> script["写稿<br/>Agent"] --> voiceover["配音<br/>MiMo TTS"] --> assemble["组装<br/>混音·字幕"] --> output(["Recap"])
    understand -. 剪辑模式：先剪后配 .-> cut["剪辑<br/>先剪成片"] -.-> script
    classDef io fill:#eef6ff,stroke:#4f86c6,color:#1f2937;
    classDef opt fill:#f3f4f6,stroke:#9ca3af,color:#374151;
    class video,output io;
    class research,cut opt;
```

## 为什么用它

- **一个 key 跑全程。** ASR、VLM、TTS 全走[小米 MiMo](https://platform.xiaomimimo.com)，本地除了 `ffmpeg` 没别的依赖。
- **该查资料时先查。** 片名/剧情明确或 brief 提示素材偏薄时，把人物关系、剧情背景存进 `background_research.json`，VLM 才更容易认出谁是谁。
- **解说成块，原声也成块。** 解说一段段连着讲、整块一次配音，段间留白把精彩原声整段放回满音量——大致七三开。
- **先剪后配，画面对齐。** `--edit-mode cut` 先把长视频剪成成片，再对着成片写解说，时间轴天然对齐。
- **能接着在剪映里改。** 可选导出多轨剪映草稿，原片、解说、BGM、字幕各占一轨。

## 安装

**① 装插件**——复制到 claude code：

```text
安装这个插件：https://github.com/worldwonderer/video-recap-skills
```

**② 装 ffmpeg**（不用 `pip install`：纯标准库 + `PATH` 上的 `ffmpeg`，Python 3.10+）：

```bash
brew install ffmpeg                        # macOS
sudo apt install ffmpeg                     # Debian/Ubuntu
choco install ffmpeg                        # Windows（或 scoop / winget install ffmpeg）
```

字幕默认烧进画面，需要带 **libass（`subtitles` 滤镜）** 的 ffmpeg——上面这些包基本都自带。如果你的 ffmpeg 没编 libass，开跑前会立刻报错并提示（也可以加 `--no-burn-subtitles` 输出未遮黑条的 MP4 + `.srt` 外挂字幕）。用 `python3 skills/video-recap/scripts/recap.py --doctor` 自检。

**③ 配 MiMo API Key**（一个 key 同时驱动 ASR / VLM / TTS；先在 [platform.xiaomimimo.com](https://platform.xiaomimimo.com) 注册获取）：

```bash
export MIMO_API_KEY=your-mimo-key
# tp-* 的 Token-Plan key 会自动连集群，可选 cn | sgp | ams：
export MIMO_TOKEN_PLAN_CLUSTER=cn
```

按量付费的 `sk-*` key 默认走 `https://api.xiaomimimo.com/v1`。其它都有默认值；想分别配 key/URL 或改模型、音色、响度、字幕等，可见
[配置手册](skills/video-recap/references/config-playbook.md)。

## 怎么用

把视频丢给它，顺手给点视频背景：

```text
给 /path/to/video.mp4 做个解说。这是《庆余年》第一集，主角是范闲。
```

它会分析视频、照背景写解说，产出带字幕的 `recap_<名>.mp4`。

```text
把 /path/to/long.mp4 剪成十分钟左右的解说短片，字幕压进画面。
```

背后是编排器把几个阶段串起来跑，中间停下来让 Agent 写解说（剪辑模式会停两次：先写 `clip_plan.json` 挑片段，剪成成片后再对着成片写 `narration.json`）。第一次跑前可先自检环境：

```bash
python3 skills/video-recap/scripts/recap.py --doctor
```

## 架构

| Skill | 职责 | 输入 → 输出（`work_dir` 契约） |
|---|---|---|
| **video-understanding** | 场景检测 · 抽帧 · ASR（`mimo-v2.5-asr`）· VLM（`mimo-v2.5`）· 时间轴融合 · 生成 brief（`--consolidate` 索引默认开） | `视频` → `scenes / asr_result / vlm_analysis / silence_periods / timeline_fusion / agent_narration_brief.md` |
| **video-script** | 写作规则（SKILL.md）+ 评审（LLM 评委）+ lint/校验 | `brief + 索引` → `narration.json` |
| **video-cut** | 片段计划 → 拼剪成片（剪辑模式先剪后配，解说按成片时间轴写，无需重映射） | `clip_plan.json + 视频` → `edited_source.mp4` |
| **video-voiceover** | 合成解说音频（MiMo TTS，`mimo-v2.5-tts`） | `narration.json` → `tts_segments/ + tts_meta.json` |
| **video-assemble** | 混音 · 压低原声 · 渲染字幕 · 多轨时间线（可选导出剪映） | `视频 + tts_meta` → `recap_<名>.mp4 + subtitles.srt/.ass + timeline.json` |
| **video-recap** | 编排器 + `--doctor` | `视频` → `recap_<名>.mp4` |

## 输出

- `recap_<名>.mp4`：成片（固定输出名，每次运行原地覆盖）。`subtitles.srt`（默认烧录字幕，同时产出 `subtitles.ass`；`--no-burn-subtitles` 关闭）
- `work_dir/narration.json`：解说脚本（`narration_lint.json` 时间诊断、`narration_review.md` 评审意见）
- `work_dir/agent_narration_brief.md`：给 Agent 的时间和场景 brief
- `work_dir/vlm_analysis.json` · `asr_result.json` · `silence_periods.json` · `timeline_fusion.json`：理解产物
- `work_dir/clip_plan.json` · `edited_source.mp4` · `recap_phase.json`：剪辑模式产物（解说在成片时间轴上写，`recap_phase.json` 记录剪/配进度供断点续跑）
- `work_dir/timeline.json` · `work_dir/assembly_manifest.json` · `tts_segments/` · `tts_meta.json`：多轨时间线、渲染记录与 TTS 音频

## 自带原声字幕（可选，更准）

解说块之间的原声留白会把【原声台词】烧成字幕（用 `「」` 和解说区分开）。默认这份字幕由 Agent 校对、ASR 兜底——但 ASR 时间偏粗，偶尔会和原声对不上。想要更准，直接放一份字幕文件到 `work_dir`，它会作为**首选来源**：

- `work_dir/user_subtitles.json`：`[{"start": 秒, "end": 秒, "text": "台词"}]`，按**成片**时间轴直接使用；或包一层 `{"timeline": "source", "lines": [...]}` 用**原片**时间轴，系统按剪辑计划自动映射到成片。
- `work_dir/user_subtitles.srt` / `.ass`：默认按**原片**时间轴解析并映射到成片。

优先级：**你的字幕文件 › Agent 校对的 `original_subtitles.json` › ASR 兜底**。来源准确时按句精确落到对应留白，不再用粗略的估时。

## 参考文档

- 各 skill 的契约：每个 `skills/<skill>/SKILL.md`（写作规则在 video-script 的 SKILL.md 里）
- [数据结构](skills/video-recap/references/data-schema.md) · [配置手册](skills/video-recap/references/config-playbook.md) · [多轨时间线 / 剪映导出](skills/video-recap/references/timeline-and-jianying.md)
- [背景调研指南](skills/video-understanding/references/research-guide.md) · [VLM prompt 模板](skills/video-understanding/references/prompt-templates.md)

## 致谢

- [linux.do](https://linux.do)
- 剪映草稿导出参考了 [pyJianYingDraft](https://github.com/GuanYixuan/pyJianYingDraft)、[capcut-mate](https://github.com/Hommy-master/capcut-mate)（均 Apache-2.0）的草稿结构。

## 许可

MIT，见 [LICENSE](LICENSE)。
