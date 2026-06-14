# video-recap-skills

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Claude Code Plugin](https://img.shields.io/badge/Claude%20Code-Plugin-purple)
![Powered by MiMo](https://img.shields.io/badge/AI-Xiaomi%20MiMo-green)
![Python](https://img.shields.io/badge/Python-3.10%2B-blue)
![Cross-platform](https://img.shields.io/badge/macOS%20%7C%20Linux%20%7C%20Windows-supported-informational)

中文 · [English](README.en.md)

把视频做成中文解说 recap 的 Claude Code 插件。一条流水线串起背景调研、ASR + VLM 场景理解、Agent 写解说词、TTS 配音、字幕和动态混音，由一组小而独立的 skill 拼起来。跑起来只要 ffmpeg 和一个小米 MiMo 的 API Key。

## 演示

https://github.com/user-attachments/assets/92698ec6-0d23-4f9f-8825-c3684ef57aff

## 这是什么

`video-recap-skills` 让 Agent 把已有视频做成短篇解说 recap。它由五个独立 skill 加一个编排器组成，每个 skill 各管一段，彼此不共享代码，只靠 `work_dir` 里的 JSON/MP4 文件传结果。解说词交给 Agent 写，剪辑、配音、混音这些确定的活儿交给脚本。

好上手的地方在于：语音转写、画面理解、语音合成全都走 [小米 MiMo](https://platform.xiaomimimo.com)，本地只装一个 `ffmpeg`。不碰 GPU，不下模型，也不用另起服务，macOS、Linux、Windows 都能跑。

```mermaid
flowchart TD
    research["背景调研<br/>background_research.json"] --> understand
    video(["输入视频"]) --> understand["video-understanding<br/>场景 · ASR · VLM · brief"]
    understand --> script["video-script<br/>Agent 写 narration.json"]
    script --> voiceover["video-voiceover<br/>MiMo TTS"]
    voiceover --> assemble["video-assemble<br/>混音 · 压低 · 字幕"]
    assemble --> output(["Recap 视频"])
    script -. 剪辑模式 .-> cut["video-cut"] -.-> voiceover

    classDef io fill:#eef6ff,stroke:#4f86c6,color:#1f2937;
    classDef opt fill:#f3f4f6,stroke:#9ca3af,color:#374151;
    class video,output io;
    class research,cut opt;
```

## 架构

`video-recap` 是你直接用的编排器。它按子进程依次调起各阶段 skill，轮到写解说词时停下来等 Agent。四个纯工具阶段设了 `user-invocable: false` 藏起来，对外只暴露 `video-recap` 和 `video-script`。

| Skill | 职责 | 输入 → 输出（`work_dir` 契约） |
|---|---|---|
| **video-understanding** | 场景检测 · 抽帧 · ASR（`mimo-v2.5-asr`）· VLM（`mimo-v2.5`）· 时间轴融合 · 生成 brief（可选 `--consolidate` 索引） | `视频` → `scenes / asr_result / vlm_analysis / silence_periods / timeline_fusion / agent_narration_brief.md` |
| **video-script** | 写作规则（SKILL.md）+ 评审（LLM 评委）+ lint/校验 | `brief + 索引` → `narration.json` |
| **video-cut** | 片段计划 → 拼剪源 + 重映射解说（剪辑模式） | `clip_plan.json + 视频` → `edited_source.mp4 + narration_mapped.json` |
| **video-voiceover** | 合成解说音频（MiMo TTS，`mimo-v2.5-tts`） | `narration.json` → `tts_segments/ + tts_meta.json` |
| **video-assemble** | 混音 · 压低原声 · 渲染字幕 · 多轨时间线（可选导出剪映） | `视频 + tts_meta` → `recap_<名>.mp4 + subtitles.srt/.ass + timeline.json` |
| **video-recap** | 编排器 + `--doctor` | `视频` → `recap_<名>.mp4` |

每个 skill 自带一份 `lib.py`（配置和工具都在里面），相互之间没有共享代码文件，JSON 产物就是唯一的接口。各 skill 的完整参数见各自的 `SKILL.md`。

## 为什么用它

一个 key 跑全程。ASR、VLM、TTS 都走小米 MiMo 的 OpenAI 兼容接口，本地只要 `ffmpeg`，不用 GPU 也不用下模型，三个平台都能用。

先调研再分析。把剧情、人物、关系先查清楚写进 `background_research.json`，理解阶段的 VLM 就能照着叫出人名、带着剧情读画面，而不是把人都标成「黑衣男子」。

看得懂画面也听得到对白。`mimo-v2.5-asr` 转写对白，配上场景切分和 `mimo-v2.5` 的画面描述、帧级动作。

可以「整理」成索引。`--consolidate` 把逐场景的 VLM 结果汇总成一份全局的人物 / 关系 / 剧情索引；`--consolidate-asr` 顺手把转写清洗一遍，时间戳不动。

写完先过一道评审。`review.py` 给草稿挑毛病（幻觉、钩子、主线、密度这些），只给建议、留记录；真正卡着不放行的是 `validate.py`。

多轨时间线，可选导出剪映。合成阶段会顺手产出一份后端中立的 `timeline.json`（视频 / 原声 / 解说 / BGM / 字幕 多轨，带 ducking 自动化）。想继续手动精修，就加 `--export-jianying` 把它导成剪映草稿（原片片段 + 各条音轨 + 音量关键帧）。这件事完全可选：核心渲染只靠 `ffmpeg`，不装剪映也照常出片。

原声不丢。解说是把原声压低之后混进去的，不会盖掉对白和环境声。

改稿不用重跑分析。动了 `narration.json`，只重跑配音和组装就行。

能做成剪辑版。`--edit-mode cut` 在 `clip_plan.json` 里挑片段，把长视频压成更短的解说剪辑。

## 安装

### 1. 安装插件

对 Claude Code 说：

```text
安装这个插件：https://github.com/worldwonderer/video-recap-skills
```

### 2. 安装 ffmpeg

```bash
# macOS
brew install ffmpeg
# Debian/Ubuntu
sudo apt install ffmpeg
# Windows（任选其一）
choco install ffmpeg   # 或：scoop install ffmpeg   |   winget install ffmpeg
```

除了 ffmpeg，只要 Python 3.10+。脚本用的都是标准库，加上 `PATH` 上的 `ffmpeg` 就够，流水线本身不用 `pip install`。

### 3. 配置 MiMo API Key

一个 key 同时驱动 ASR、VLM、TTS。只放环境变量，别写进仓库。

```bash
export MIMO_API_KEY=your-mimo-key
```

按量付费的 `sk-*` key 默认走 `https://api.xiaomimimo.com/v1`。Token-Plan 的 `tp-*` key 会自动连到 Token-Plan 集群（默认 `cn`）：

```bash
export MIMO_TOKEN_PLAN_CLUSTER=cn   # cn | sgp | ams
# 也可以直接写死 base URL：export MIMO_API_URL=https://token-plan-cn.xiaomimimo.com/v1
```

其它都有默认值，想改的话，所有环境变量（模型、ASR 分段、音色、响度、字幕等等）列在
[`skills/video-recap/references/config-playbook.md`](skills/video-recap/references/config-playbook.md)。
如果想给三种能力分别配 key 或 URL，用 `MIMO_VIDEO_API_KEY` / `MIMO_TTS_API_KEY` / `MIMO_ASR_API_KEY`（以及对应的 `*_API_URL`），没设的就回退到 `MIMO_API_KEY` / `MIMO_API_URL`。

## 怎么用

它是个 Claude Code skill，用大白话指挥就行。装好插件后把视频丢给它，顺手给点剧情背景：

```text
给 /path/to/video.mp4 做个解说。这是《庆余年》第一集，主角是范闲。
```

Claude Code 会分析视频，照你给的背景写解说，产出带字幕的 `recap_<名>.mp4`。想要别的花样，照样一句话：

```text
把 /path/to/long.mp4 剪成十分钟左右的解说短片，字幕压进画面。
```

背后是编排器把几个阶段串起来跑（理解 → 写稿 →（剪辑）→ 配音 → 组装），中间停下来让 Agent 写 `narration.json`；剪辑模式、压字幕这些只是它顺手带上的参数。想看具体环节、或自己单跑某一段，看对应的 `skills/<skill>/SKILL.md`。

第一次跑之前先自检一下环境：

```bash
python3 skills/video-recap/scripts/recap.py --doctor
```

## 输出

- `recap_<video>.mp4`：成片。`subtitles.srt`（加 `--burn-subtitles` 时还有 `subtitles.ass`）
- `work_dir/agent_narration_brief.md`：给 Agent 的时间和场景 brief
- `work_dir/narration.json`：解说脚本。`work_dir/narration_lint.json`：时间诊断
- `work_dir/narration_review.md`：评审意见（可选，只是建议）
- `work_dir/vlm_analysis.json`、`asr_result.json`、`silence_periods.json`、`timeline_fusion.json`：理解产物
- `work_dir/understanding_index.json` / `asr_clean.json`：`--consolidate` 的产物
- `work_dir/clip_plan.json`、`edited_source.mp4`、`narration_mapped.json`：剪辑模式产物
- `work_dir/mimo_video_overview.json`：MiMo 分片理解（`--mimo-video-overview`，可选）
- `work_dir/tts_segments/`、`tts_meta.json`：TTS 音频和放置信息

## 参考文档

- 各 skill 的契约：每个 `skills/<skill>/SKILL.md`（写作规则在 video-script 的 SKILL.md 里）
- [数据结构](skills/video-recap/references/data-schema.md) · [配置手册](skills/video-recap/references/config-playbook.md) · [多轨时间线 / 剪映导出](skills/video-recap/references/timeline-and-jianying.md)
- [背景调研指南](skills/video-understanding/references/research-guide.md) · [VLM prompt 模板](skills/video-understanding/references/prompt-templates.md)

## 致谢

- [linux.do](https://linux.do)
- 剪映草稿导出参考了 [pyJianYingDraft](https://github.com/GuanYixuan/pyJianYingDraft)、[capcut-mate](https://github.com/Hommy-master/capcut-mate)（均 Apache-2.0）的草稿结构，未内置其代码。

## 许可

MIT，见 [LICENSE](LICENSE)。
