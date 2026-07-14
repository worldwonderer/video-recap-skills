# 单测体系

本仓库的每个 skill 都自带同名顶层模块（例如 `lib.py`），因此测试必须按 skill 分组、分进程运行。

## 分层

1. **纯行为测试**：直接调用解析、校验、时间线、字幕、混音等纯函数，断言结构化返回值和错误边界。
2. **产物测试**：在临时 `work_dir` 运行阶段函数，读取其实际生成的 JSON、字幕、命令或清单。
3. **契约与打包测试**：解析 frontmatter、Markdown 标号、JSON 示例和本地引用，并把单个 skill 复制到隔离目录后导入全部脚本。

## 约束

- 对刻意复制的 skill 实现，只保留一套行为测试；再用字节一致性和隔离导入防止副本漂移。
- 不通过读取 Python 源码并匹配一句文案来证明行为；应调用公开函数或检查真实产物。
- prompt / `SKILL.md` 属于声明式契约时，优先解析其章节、枚举、frontmatter、JSON 示例和本地引用，不散落检查任意短语。
- 对生成的 SRT、ASS、ffmpeg 命令、评审消息等协议文本，字符串断言仍然合理，因为文本本身就是输出契约。
- 新增测试目录时，必须进入 `scripts/test.py::GROUPS`；架构测试会阻止漏跑。
- 完全相同的测试体必须合并或参数化；架构测试只负责阻止精确重复，语义重复仍需代码审查判断。

## 命令

```bash
python3 scripts/test.py                 # 全量，逐组隔离
python3 scripts/test.py script          # 单组
python3 -m pytest tests/script -q       # 调试单组
PYTHON=.venv/bin/python bash scripts/test.sh script  # shell 兼容入口，可显式选择解释器
```

不要直接运行根目录 `pytest` 或 `pytest tests/`，根级 `conftest.py` 会主动拒绝可能产生模块串扰的收集方式。
