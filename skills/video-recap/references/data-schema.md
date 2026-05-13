# 数据格式（中间 JSON）

所有中间文件均在 pipeline 工作目录（work_dir/）下。

## vlm_analysis.json

每场景的 VLM 分析结果，数组格式：

```json
[
  {
    "scene_id": 1,
    "start": 5.0,
    "end": 15.0,
    "description": "男子闯入房间",
    "depth_analysis": "角色情绪分析...",
    "frame_facts": {
      "5.0": ["男子闯入房间, 头发蓬乱表情紧张"],
      "10.0": ["男子俯身盯着床上男孩, 男孩睁眼惊醒"]
    }
  }
]
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `scene_id` | int | 场景编号 |
| `start` | float | 开始时间（秒） |
| `end` | float | 结束时间（秒） |
| `description` | string | 画面简述（≤80字） |
| `depth_analysis` | string | 深层分析（情绪/关系/潜台词） |
| `frame_facts` | object | 帧级事实，key 为时间戳字符串 |

## asr_result.json

语音转文字结果：

```json
[
  {"start": 0.0, "end": 3.5, "text": "What are you doing here?"}
]
```

## silence_periods.json

静音窗口列表（适合放解说）：

```json
[
  {"start": 2.0, "end": 8.5}
]
```

## narration.json

Agent 撰写的解说词（字段详见 `agent-mode-workflow.md`）：

```json
[
  {"start": 2.5, "end": 7.0, "narration": "解说文本", "pause_after_ms": 600, "overlaps_speech": false}
]
```

## background_research.json

可选的背景调研结果（由 browser-cdp skill 产出）：

```json
{
  "synopsis": "剧情概要",
  "characters": {"角色名": "角色简介"},
  "worldbuilding": "世界观设定",
  "episode_context": "集数上下文"
}
```
