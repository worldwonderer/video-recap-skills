"""Normalize MiMo QC observations into the local contract."""

from __future__ import annotations


import importlib.util

import json


import re


from pathlib import Path

from typing import Any, Mapping

import qc_contract

from mimo_qc_evidence import _fingerprint_value, _redact, _summarize, safe_mimo_config
from mimo_qc_payload import _strip_json_fence

_LOCAL_LIB_PATH = Path(__file__).with_name("lib.py")

_LOCAL_LIB_SPEC = importlib.util.spec_from_file_location(
    "video_recap_mimo_qc_lib", _LOCAL_LIB_PATH
)

if (
    _LOCAL_LIB_SPEC is None or _LOCAL_LIB_SPEC.loader is None
):  # pragma: no cover - import invariant
    raise ImportError(f"cannot load local MiMo QC client: {_LOCAL_LIB_PATH}")

_LOCAL_LIB = importlib.util.module_from_spec(_LOCAL_LIB_SPEC)

_LOCAL_LIB_SPEC.loader.exec_module(_LOCAL_LIB)

ARTIFACT_NAME = "mimo_qc.json"

DEFAULT_STAGE = "pre_assemble"

MAX_OBSERVATIONS = 12

MAX_MESSAGE_CHARS = 800


def _extract_observations(model_output: Any) -> list[Mapping[str, Any]]:
    model_output = _redact(model_output)
    if isinstance(model_output, str):
        try:
            model_output = json.loads(_strip_json_fence(model_output))
        except (TypeError, ValueError):
            return [
                {
                    "code": "freeform_observation",
                    "message": model_output,
                    "confidence": "low",
                }
            ]
    if isinstance(model_output, list):
        return [item for item in model_output if isinstance(item, Mapping)][
            :MAX_OBSERVATIONS
        ]
    if not isinstance(model_output, Mapping):
        return []
    for key in ("observations", "findings"):
        if isinstance(model_output.get(key), list):
            return [item for item in model_output[key] if isinstance(item, Mapping)][
                :MAX_OBSERVATIONS
            ]
    try:
        return _extract_observations(model_output["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError):
        return [model_output] if model_output else []


def _norm_choice(raw: Any, allowed: set[str], default: str) -> str:
    value = str(raw or "").strip().lower()
    return value if value in allowed else default


def _caption_match_text(value: Any) -> str:
    return re.sub(r"[^0-9A-Za-z\u3400-\u9fff]+", "", str(value or "")).lower()


def _generated_subtitle_corpus(payload: Mapping[str, Any]) -> str:
    group = (
        (payload.get("evidence") or {}).get("generated_subtitles")
        if isinstance(payload.get("evidence"), Mapping)
        else None
    )
    strings = []

    def visit(value: Any) -> None:
        if isinstance(value, str):
            strings.append(value)
        elif isinstance(value, Mapping):
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(group or {})
    return _caption_match_text(" ".join(strings))


def _misclassified_generated_caption(
    observation: Mapping[str, Any], payload: Mapping[str, Any], stage: str
) -> bool:
    """Drop an objectively contradicted 'source subtitle visible' observation.

    MiMo occasionally calls the intended recap cue on an opaque mask band a leftover source
    subtitle even when its own ``visible_text`` exactly matches generated_subtitles.  This is not
    a subjective disagreement: the artifact provides direct provenance for that text.
    """
    if stage != "post_render":
        return False
    code_and_message = (
        f"{observation.get('code', '')} {observation.get('message', '')}".lower()
    )
    if not any(
        token in code_and_message
        for token in ("source_subtitle", "source subtitle", "源字幕")
    ):
        return False
    if any(
        token in code_and_message for token in ("overlap", "double", "两层", "重叠")
    ):
        return False
    evidence = observation.get("evidence")
    visible = evidence.get("visible_text") if isinstance(evidence, Mapping) else None
    visible = _caption_match_text(visible)
    corpus = _generated_subtitle_corpus(payload)
    return len(visible) >= 4 and bool(corpus) and visible in corpus


def normalize_observations(
    model_output: Any,
    *,
    stage: str = DEFAULT_STAGE,
    payload: Mapping[str, Any] | None = None,
    model_config: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Normalize bounded model output into permanently non-blocking findings."""
    payload = _redact(payload or {})
    cfg = safe_mimo_config(model_config)
    model_used = str(cfg.get("model") or "mimo-qc-offline-fixture")
    findings = []
    for index, observation in enumerate(_extract_observations(model_output), start=1):
        if _misclassified_generated_caption(observation, payload, stage):
            continue
        obs = _summarize(observation, max_items=10, max_string=MAX_MESSAGE_CHARS)
        if not isinstance(obs, Mapping):
            continue
        category_hint = str(
            obs.get("category") or obs.get("type") or "semantic"
        ).lower()
        category = "mimo_aesthetic" if "aesthetic" in category_hint else "mimo_semantic"
        code = str(
            obs.get("code") or obs.get("rule_id") or f"mimo_observation_{index}"
        ).strip()
        code = (code or f"mimo_observation_{index}")[:96]
        message = str(
            obs.get("message")
            or obs.get("summary")
            or obs.get("text")
            or "MiMo QC observation"
        )
        message = message.strip()[:MAX_MESSAGE_CHARS] or "MiMo QC observation"
        confidence = _norm_choice(
            obs.get("confidence"), {"low", "medium", "high"}, "low"
        )
        sample_type = _norm_choice(
            obs.get("sample_policy") or obs.get("sample_policy_type"),
            {"semantic", "aesthetic", "sampled"},
            "aesthetic" if category == "mimo_aesthetic" else "semantic",
        )
        raw_evidence = obs.get("evidence")
        supplied_evidence: Mapping[str, Any] = (
            raw_evidence if isinstance(raw_evidence, Mapping) else {}
        )
        evidence = {
            **dict(supplied_evidence),
            "model": model_used,
            "config": cfg,
            "fingerprint": {
                "evidence": payload.get("evidence_fingerprint"),
                "observation": _fingerprint_value(observation),
            },
        }
        findings.append(
            qc_contract.build_finding(
                finding_id=str(
                    obs.get("finding_id")
                    or obs.get("id")
                    or f"mimo-{stage}-{index:03d}"
                )[:160],
                stage=stage,
                severity="advisory",
                confidence=confidence,
                sample_policy={"type": sample_type},
                category=category,
                code=code,
                message=message,
                deterministic=False,
                blocking=False,
                source={"artifact": ARTIFACT_NAME, "adapter": "mimo_qc.py"},
                location=obs.get("location")
                if isinstance(obs.get("location"), Mapping)
                else {},
                evidence=_redact(evidence),
                model_used=model_used,
                artifact_fingerprints={
                    "payload": str(payload.get("payload_fingerprint") or "fixture"),
                    "evidence": str(payload.get("evidence_fingerprint") or "fixture"),
                },
                next_action="human_review",
                decision_reason=message,
            )
        )
    return findings
