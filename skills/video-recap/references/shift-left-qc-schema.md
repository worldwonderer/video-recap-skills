# 前置 QC 数据契约

`final_qc.json`、`golden_eval.json`、`mimo_qc.json` 与 `preflight_qc.json` 共用一套最小结构，由本技能的 `scripts/qc_contract.py` 实现。

## 字段契约

- `schema_version`：整数 `1`。
- `artifact`：只能是 `final_qc.json`、`golden_eval.json`、`mimo_qc.json` 或 `preflight_qc.json`。
- `stage`：只能是 `pre_cut`、`post_cut`、`pre_tts`、`post_tts`、`pre_assemble`、`post_render`、`golden`。
  - 不得使用 `pre_voiceover`、`post_voiceover`、`pre_export`、`post_export`、`final`、`golden_eval` 或 `mimo_qc` 作为阶段值。
  - `mimo_qc.json` 是产物名；MiMo finding 必须挂在 `post_tts`、`pre_assemble` 等真实阶段上。
- `findings[]`：每项必须包含 `finding_id`、`stage`、`severity`、`blocking`、`deterministic`、`confidence`、`rule_id`、`decision_reason`、`location`、`evidence`、`sample_policy`、`model_used`、`artifact_fingerprints` 与 `next_action`。
- `sample_policy`：至少包含 `type`；其值只能是 `all`、`deterministic`、`sampled`、`semantic` 或 `aesthetic`。
- `location.timecode` 与 `location.source_span` 必须存在；剪辑前阶段可以把任一字段设为 `null`。
- 为兼容与诊断，当前辅助函数还可能写出 `id`、`category`、`code`、`message`、`source` 与 `objective_corroboration`。

## 阻断语义

只有确定性的客观规则可以阻断，例如产物缺失或过期、时长或媒体流错误、字幕与 TTS 放置错误、数据结构无效。

MiMo 语义/审美 finding 以及其他所有非确定性 finding **始终只给建议，不能阻断**。若存在客观佐证，必须由确定性生产者单独写成确定性 finding；它不能把主观模型观察升级成 blocker。运行时不存在 allow-list 或规则表逃生口。

`qc_contract.py` 只负责数据结构：不调用 MiMo、不连接流水线，也不自动修复。独立的 `mimo_qc.py` 只有在显式开启时，才会在 `pre_assemble` 和/或 `post_render` 各发起一次实时请求；它保持 fail-open，且不落盘凭证或抽帧 base64。所有报告构建器都通过 `qc_contract.redact_secrets` 处理元数据与证据：脱敏疑似密钥的键值，并从 URL 移除 userinfo、query 与 fragment，同时保留有用的 host/path 上下文。
