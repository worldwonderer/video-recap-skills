# 背景调研指南（browser-cdp）

Agent 写解说词前的可选调研步骤。用 browser-cdp 搜索视频背景信息，丰富解说深度。

## 通用流程

```bash
# 1. 启动 CDP Chrome
bash {browser-cdp-scripts}/setup_cdp_chrome.sh 9222

# 2. 搜索：打开 Google → 等待加载 → eval 提取文本
agent-browser --cdp 9222 open "https://www.google.com/search?q=关键词"
agent-browser --cdp 9222 wait 3000
agent-browser --cdp 9222 eval 'document.body.innerText.substring(0,6000)'

# 3. 将结果写入 JSON
# 写入 work_dir/background_research.json，schema 见 data-schema.md

# 4. 清理
agent-browser --cdp 9222 close
```

**核心模式**：每次搜索 → 提取正文 → 归入对应字段 → 写入 JSON。

## 按视频类型搜索策略

### 短剧/电视剧（3 次搜索）

| 搜索关键词 | 目标字段 |
|-----------|---------|
| `{作品名} 剧情 介绍 人物` | synopsis, characters |
| `{作品名} 人物 关系` | characters（关系补充） |
| `{作品名} 第{N}集 剧情` | episode_context |

### 电影（2 次搜索）

| 搜索关键词 | 目标字段 |
|-----------|---------|
| `{电影名} 剧情 简介` | synopsis |
| `{电影名} 影评 解读` | cultural_notes |

### 纪录片（2 次搜索）

| 搜索关键词 | 目标字段 |
|-----------|---------|
| `{主题} 背景 知识` | worldbuilding |
| `{主题} 最新 进展` | cultural_notes |

### 科普视频（2 次搜索）

| 搜索关键词 | 目标字段 |
|-----------|---------|
| `{核心概念} 解释` | synopsis |
| `{核心概念} 最新 研究` | cultural_notes |

## 错误处理

| 场景 | 处理 |
|------|------|
| CDP 连接失败 | 跳过调研，直接写解说词 |
| 搜索无结果 | 换一组关键词重试一次，仍无结果则跳过 |
| 页面非中文 | URL 加 `&hl=zh-CN` 重试，仍非中文则跳过 |

**原则**：调研是可选的，任何失败都不阻塞解说词生成流程。

## 写入格式

写入 `work_dir/background_research.json`，schema 见 `data-schema.md`。
只填搜索到的字段，未搜到的字段省略（不要写 null 或空字符串）。
