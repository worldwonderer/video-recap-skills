# video-recap

视频自动解说 skill。输入一个视频，输出带中文旁白的解说视频。适配 Claude Code。

## 流程总览

```mermaid
flowchart LR
    A["🎬 输入视频"] --> B["前置分析"]
    B --> C["Agent 写解说词 ★"]
    C --> D["TTS + 动态混音"]
    D --> E["🎬 解说视频"]
```

## 效果预览

https://github.com/user-attachments/assets/92698ec6-0d23-4f9f-8825-c3684ef57aff

## 安装

**方式一** 直接告诉 Claude Code：

```
安装这个 skill https://github.com/worldwonderer/video-recap
```

**方式二** 命令行（推荐软链）：

```bash
git clone https://github.com/worldwonderer/video-recap.git /tmp/video-recap-repo
ln -s /tmp/video-recap-repo/skills/video-recap ~/.claude/skills/video-recap
```

安装系统依赖：

```bash
brew install ffmpeg
pip3 install edge-tts
```

配置 VLM API Key（用于画面分析，兼容 OpenAI 格式）：

```bash
export OPENAI_API_KEY=your-key
export OPENAI_API_URL=https://your-proxy/v1/chat/completions  # 可选
```

## 使用

安装后，在 Claude Code 中直接说：

```
帮我为 /path/to/video.mp4 生成解说视频
```

或者指定风格和背景：

```
用轻松幽默风格为这个视频生成解说，背景是欲望都市，男主Big，女主凯莉
```

支持的触发词：`video-recap`、`视频解说`、`视频旁白`、`生成解说`、`视频recap`

Agent 会根据你的描述自动选择风格、TTS 引擎等参数，也可以直接指定。

## 核心特性

| 特性 | 说明 |
|------|------|
| Agent 亲自写解说词 | Claude 根据画面分析和剧情理解直接撰写，不套模板 |
| Zone 模式 | 大段解说 + 原声交替，不是逐场景碎片段 |
| 智能场景检测 | 基于 ffmpeg scdet 自动分割，短场景自动合并 |
| VLM 深度分析 | 识别角色情绪、关系动态、潜台词 |
| 静音感知插入 | 合并相邻安静窗口为解说区，避开对白 |
| 动态 Ducking | 解说时原声大幅压低，不解说时原声满音量 |
| 多风格支持 | 短剧 / 电视剧 / 电影 / 纪录片 / 科普视频 |
| 断点续跑 | 每步结果持久化，中断后 `--resume` 恢复 |

## 依赖

| 依赖 | 用途 | 安装 |
|------|------|------|
| ffmpeg / ffprobe | 帧提取、场景检测、音频处理 | `brew install ffmpeg` |
| edge-tts | TTS 语音合成 | `pip3 install edge-tts` |
| qwen3-asr-rs | 本地 ASR 转录（可选） | 从源码编译 |
| OpenAI 兼容 API | VLM 画面分析 | 需配置 API Key |

## 自定义

所有 prompt 模板在 `skills/video-recap/references/prompt-templates.md`，直接编辑即可调整解说风格和质量。

VLM 模型默认使用 doubao-seed-2-0-pro-260215，可通过 `OPENAI_API_URL` 和 `OPENAI_API_KEY` 配置任意兼容 OpenAI 格式的 API。

## 致谢

- [linux.do](https://linux.do)
- [qwen3-asr-rs](https://github.com/alan890104/qwen3-asr-rs)
