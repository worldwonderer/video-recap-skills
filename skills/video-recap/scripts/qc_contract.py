"""Shared shift-left QC report contract for video-recap artifacts.

This module is intentionally standalone: it defines a small schema and local
validation helpers only. It does not call MiMo, read credentials, connect the
pipeline, or attempt automatic fixes.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit


SCHEMA_VERSION = 1

STAGES = frozenset({
    "pre_cut",
    "post_cut",
    "pre_tts",
    "post_tts",
    "pre_assemble",
    "post_render",
    "golden",
})
SEVERITIES = frozenset({"info", "advisory", "warning", "blocker"})
CONFIDENCES = frozenset({"low", "medium", "high", "objective"})
SAMPLE_POLICIES = frozenset({"all", "deterministic", "sampled", "semantic", "aesthetic"})
ARTIFACTS = frozenset({"final_qc.json", "golden_eval.json", "mimo_qc.json", "preflight_qc.json"})

DETERMINISTIC_CATEGORIES = frozenset({
    "missing_artifact",
    "stale_fingerprint",
    "duration",
    "stream",
    "subtitle",
    "tts_placement",
    "schema_invalid",
    "placement",
})
NON_DETERMINISTIC_CATEGORIES = frozenset({"semantic", "aesthetic", "mimo_semantic", "mimo_aesthetic"})
BLOCKING_SEVERITIES = frozenset({"blocker"})
_SECRET_KEY_RE = re.compile(r"(?i)(api[_-]?key|token|secret|password|authorization|credential)")
_SECRET_VALUE_RE = re.compile(r"\b(?:sk|tp)-[A-Za-z0-9_-]{6,}\b")
_URL_SCHEMES_TO_SCRUB = frozenset({"http", "https", "ws", "wss"})
REDACTED = "<redacted>"


def _redact_url(value: str) -> str:
    """Strip URL credentials, query, and fragment while preserving useful host/path."""
    try:
        parts = urlsplit(value)
    except ValueError:
        return value
    if parts.scheme.lower() not in _URL_SCHEMES_TO_SCRUB or not parts.netloc:
        return value
    host = parts.hostname or ""
    if not host:
        return value
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = host
    try:
        port = parts.port
    except ValueError:
        port = None
    if port is not None:
        netloc = f"{netloc}:{port}"
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def redact_secrets(value: Any) -> Any:
    """Return a JSON-safe copy with likely credentials removed.

    Centralized for all video-recap QC reports: redacts secret-looking keys,
    common synthetic key/token values, and URL userinfo/query/fragment fields.
    """
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_s = str(key)
            redacted[key_s] = REDACTED if _SECRET_KEY_RE.search(key_s) else redact_secrets(item)
        return redacted
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    if isinstance(value, tuple):
        return [redact_secrets(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, str):
        return _SECRET_VALUE_RE.sub(REDACTED, _redact_url(value))
    return value


_REQUIRED_FINDING_FIELDS = frozenset({
    "finding_id",
    "stage",
    "severity",
    "blocking",
    "deterministic",
    "confidence",
    "rule_id",
    "decision_reason",
    "location",
    "evidence",
    "sample_policy",
    "model_used",
    "artifact_fingerprints",
    "next_action",
    # Helper fields kept for current rule semantics and compatibility.
    "category",
    "code",
    "message",
    "source",
    "objective_corroboration",
})
_REQUIRED_LOCATION_FIELDS = frozenset({"timecode", "source_span"})
_REQUIRED_REPORT_FIELDS = frozenset({
    "schema_version",
    "artifact",
    "stage",
    "ok",
    "blocker_count",
    "finding_count",
    "findings",
})


class QCContractError(ValueError):
    """Raised when a QC report or finding violates the shared contract."""


def artifact_fingerprint(path: str | Path) -> str:
    """Return a stable sha256 hex digest for an artifact file."""
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def build_finding(
    *,
    stage: str,
    severity: str,
    confidence: str,
    sample_policy: str | Mapping[str, Any],
    category: str,
    code: str,
    message: str,
    finding_id: str | None = None,
    id: str | None = None,
    rule_id: str | None = None,
    decision_reason: str | None = None,
    model_used: str | None = None,
    artifact_fingerprints: Mapping[str, Any] | None = None,
    next_action: str | None = None,
    deterministic: bool | None = None,
    blocking: bool | None = None,
    source: Mapping[str, Any] | None = None,
    location: Mapping[str, Any] | None = None,
    evidence: Mapping[str, Any] | None = None,
    objective_corroboration: Mapping[str, Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Build and validate one normalized QC finding.

    Deterministic objective findings may block. MiMo semantic/aesthetic findings
    default to advisory and may never block. Objective checks belong in the
    deterministic QC producers instead of upgrading subjective model findings.
    """
    if finding_id is None:
        finding_id = id
    if finding_id is None:
        raise QCContractError("finding_id is required")
    if rule_id is None:
        rule_id = code
    if decision_reason is None:
        decision_reason = message
    if model_used is None:
        model_used = "local_deterministic"
    if artifact_fingerprints is None:
        artifact_fingerprints = {}
    if next_action is None:
        next_action = "manual_review"
    if isinstance(sample_policy, str):
        sample_policy = {"type": sample_policy}

    if deterministic is None:
        deterministic = category in DETERMINISTIC_CATEGORIES
    if source is None:
        source = {}
    source = redact_secrets(source)
    if location is None:
        location = {"timecode": None, "source_span": None}
    else:
        location = {"timecode": location.get("timecode"), "source_span": location.get("source_span")}
    if evidence is None:
        evidence = {}
    evidence = redact_secrets(evidence)
    artifact_fingerprints = redact_secrets(artifact_fingerprints)
    objective_corroboration = redact_secrets(objective_corroboration or {})

    is_non_deterministic = (not deterministic) or category in NON_DETERMINISTIC_CATEGORIES
    if is_non_deterministic and blocking is None:
        blocking = False
    elif blocking is None:
        blocking = severity in BLOCKING_SEVERITIES

    if is_non_deterministic and blocking:
        raise QCContractError(f"non-deterministic blocking finding is not allowed: {category}/{code}")

    finding = {
        "finding_id": finding_id,
        "id": finding_id,
        "stage": stage,
        "severity": severity,
        "blocking": bool(blocking),
        "deterministic": bool(deterministic),
        "confidence": confidence,
        "rule_id": rule_id,
        "decision_reason": decision_reason,
        "location": dict(location),
        "evidence": dict(evidence),
        "sample_policy": dict(sample_policy),
        "model_used": model_used,
        "artifact_fingerprints": dict(artifact_fingerprints),
        "next_action": next_action,
        "category": category,
        "code": code,
        "message": message,
        "source": dict(source),
        "objective_corroboration": dict(objective_corroboration),
    }
    finding.update(redact_secrets(extra))
    _validate_finding(finding)
    return finding


def build_report(
    *,
    artifact: str,
    stage: str,
    findings: Sequence[Mapping[str, Any]] | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build and validate a minimal report for a supported QC artifact."""
    normalized_findings = [redact_secrets(dict(f)) for f in (findings or [])]
    blocker_count = sum(1 for f in normalized_findings if f.get("blocking") is True)
    report = {
        "schema_version": SCHEMA_VERSION,
        "artifact": artifact,
        "stage": stage,
        "ok": blocker_count == 0,
        "blocker_count": blocker_count,
        "finding_count": len(normalized_findings),
        "findings": normalized_findings,
        "metadata": redact_secrets(dict(metadata or {})),
    }
    validate_report(report)
    return report


def validate_report(report: Mapping[str, Any]) -> bool:
    """Validate report shape, value domains, and blocking semantics."""
    if not isinstance(report, Mapping):
        raise QCContractError("report must be an object")
    missing = _REQUIRED_REPORT_FIELDS - set(report)
    if missing:
        raise QCContractError(f"report missing required fields: {sorted(missing)}")
    if report["schema_version"] != SCHEMA_VERSION:
        raise QCContractError(f"unsupported schema_version: {report['schema_version']!r}")
    if report["artifact"] not in ARTIFACTS:
        raise QCContractError(f"unsupported artifact: {report['artifact']!r}")
    if report["stage"] not in STAGES:
        raise QCContractError(f"unsupported stage: {report['stage']!r}")
    if not isinstance(report["findings"], list):
        raise QCContractError("findings must be a list")
    for finding in report["findings"]:
        _validate_finding(finding)
    blocker_count = sum(1 for f in report["findings"] if f.get("blocking") is True)
    if report["blocker_count"] != blocker_count:
        raise QCContractError("blocker_count does not match findings")
    if report["finding_count"] != len(report["findings"]):
        raise QCContractError("finding_count does not match findings")
    if report["ok"] != (blocker_count == 0):
        raise QCContractError("ok must be true exactly when blocker_count is zero")
    return True


def _validate_finding(finding: Mapping[str, Any]) -> None:
    if not isinstance(finding, Mapping):
        raise QCContractError("finding must be an object")
    missing = _REQUIRED_FINDING_FIELDS - set(finding)
    if missing:
        raise QCContractError(f"finding missing required fields: {sorted(missing)}")
    if finding["stage"] not in STAGES:
        raise QCContractError(f"unsupported finding stage: {finding['stage']!r}")
    if finding["severity"] not in SEVERITIES:
        raise QCContractError(f"unsupported severity: {finding['severity']!r}")
    if finding["confidence"] not in CONFIDENCES:
        raise QCContractError(f"unsupported confidence: {finding['confidence']!r}")
    sample_policy = finding["sample_policy"]
    if not isinstance(sample_policy, Mapping):
        raise QCContractError("sample_policy must be an object")
    sample_policy_type = sample_policy.get("type")
    if sample_policy_type not in SAMPLE_POLICIES:
        raise QCContractError(f"unsupported sample_policy.type: {sample_policy_type!r}")
    if not isinstance(finding["deterministic"], bool):
        raise QCContractError("deterministic must be boolean")
    if not isinstance(finding["blocking"], bool):
        raise QCContractError("blocking must be boolean")
    if finding["blocking"] and finding["severity"] != "blocker":
        raise QCContractError("blocking findings must use severity='blocker'")
    location = finding["location"]
    if not isinstance(location, Mapping):
        raise QCContractError("location must be an object")
    missing_location = _REQUIRED_LOCATION_FIELDS - set(location)
    if missing_location:
        raise QCContractError(f"location missing required fields: {sorted(missing_location)}")
    if finding["source"] is None or not isinstance(finding["source"], Mapping):
        raise QCContractError("source must be an object")
    if finding["evidence"] is None or not isinstance(finding["evidence"], Mapping):
        raise QCContractError("evidence must be an object")
    if finding["artifact_fingerprints"] is None or not isinstance(finding["artifact_fingerprints"], Mapping):
        raise QCContractError("artifact_fingerprints must be an object")
    if finding["objective_corroboration"] is None or not isinstance(finding["objective_corroboration"], Mapping):
        raise QCContractError("objective_corroboration must be an object")
    for field in ("finding_id", "rule_id", "decision_reason", "model_used", "next_action"):
        if not isinstance(finding[field], str) or not finding[field]:
            raise QCContractError(f"{field} must be a non-empty string")

    is_non_deterministic = (not finding["deterministic"]) or finding["category"] in NON_DETERMINISTIC_CATEGORIES
    if is_non_deterministic and finding["blocking"]:
        raise QCContractError(
            f"non-deterministic blocking finding is not allowed: {finding['category']}/{finding['code']}"
        )
