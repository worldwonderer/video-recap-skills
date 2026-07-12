# Shift-left QC schema contract

QC artifacts (`final_qc.json`, `golden_eval.json`, `mimo_qc.json`, `preflight_qc.json`) share one minimal schema, implemented by `skills/video-recap/scripts/qc_contract.py`.

## Contract

- `schema_version`: integer `1`.
- `artifact`: one of `final_qc.json`, `golden_eval.json`, `mimo_qc.json`, `preflight_qc.json`.
- `stage`: exactly one of `pre_cut`, `post_cut`, `pre_tts`, `post_tts`, `pre_assemble`, `post_render`, `golden`.
  - Do not use `pre_voiceover`, `post_voiceover`, `pre_export`, `post_export`, `final`, `golden_eval`, or `mimo_qc` as stage values.
  - `mimo_qc.json` is an artifact; MiMo findings attach to an actual stage such as `post_tts` or `pre_assemble`.
- `findings[]`: every finding carries canonical fields `finding_id`, `stage`, `severity`, `blocking`, `deterministic`, `confidence`, `rule_id`, `decision_reason`, `location`, `evidence`, `sample_policy`, `model_used`, `artifact_fingerprints`, and `next_action`.
- `sample_policy`: object with at least `type`; `type` must be one of `all`, `deterministic`, `sampled`, `semantic`, `aesthetic`.
- `location.timecode` and `location.source_span` must exist. Pre-cut stages may set either to `null`.
- Current helpers may also emit `id`, `category`, `code`, `message`, `source`, and `objective_corroboration` for compatibility and diagnostics.

## Blocking semantics

Deterministic objective rules may block: missing/stale artifacts, duration or stream errors, subtitle and TTS placement errors, and schema-invalid errors.

MiMo semantic/aesthetic findings and every other non-deterministic finding are
**always advisory and non-blocking**. Objective corroboration must be emitted by
the deterministic producer as its own deterministic finding; it can never
upgrade a subjective model observation into a blocker. There is no runtime
allow-list or rule-table escape hatch.

`qc_contract.py` is schema-only: it does not call MiMo, connect the pipeline, or
auto-fix. The separate `mimo_qc.py` feature can make one explicitly enabled live
request at `pre_assemble` and/or `post_render`; it remains fail-open and never
persists credentials or sampled-frame base64. All report builders pass metadata
and evidence through `qc_contract.redact_secrets`, which redacts secret-looking
keys/values and strips URL userinfo/query/fragment while preserving useful
host/path context.
