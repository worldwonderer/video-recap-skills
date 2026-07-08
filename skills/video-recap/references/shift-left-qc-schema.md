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
- Current helpers may also emit `id`, `category`, `code`, `message`, `source`, and `objective_corroboration` for compatibility and blocking-rule evaluation.

## Blocking semantics

Deterministic objective rules may block: missing/stale artifacts, duration or stream errors, subtitle and TTS placement errors, and schema-invalid errors.

MiMo semantic/aesthetic findings are advisory by default and non-blocking. A non-deterministic finding cannot block unless `shift-left-qc-rules.json` has the same `schema_version` as `qc_contract.SCHEMA_VERSION`, explicitly allows its category/code, and the finding includes objective corroboration.

The contract is schema-only: it does not call MiMo, does not connect the pipeline, does not auto-fix, and never reads or persists API keys. All QC report builders must pass metadata/evidence through `qc_contract.redact_secrets` before writing; this redacts secret-looking keys/values and strips URL userinfo/query/fragment while preserving useful host/path context.
