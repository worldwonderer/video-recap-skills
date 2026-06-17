#!/usr/bin/env python3
"""Environment doctor for the video-recap skill bundle.

The whole pipeline runs on ffmpeg + a single MiMo API key (ASR + VLM + TTS all use MiMo).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from lib import CONFIG


SCRIPT_DIR = Path(__file__).resolve().parent


def _command_path(name: str) -> str | None:
    """Return resolved command path, accepting absolute/relative executable paths."""
    if not name:
        return None
    if os.path.sep in name or (os.path.altsep and os.path.altsep in name):
        path = Path(name).expanduser()
        return str(path) if path.exists() and os.access(path, os.X_OK) else None
    return shutil.which(name)


def _run(cmd: list[str], *, timeout: int = 20) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)


def _ffmpeg_filters() -> set[str]:
    ffmpeg = _command_path("ffmpeg")
    if not ffmpeg:
        return set()
    try:
        result = _run([ffmpeg, "-hide_banner", "-filters"], timeout=20)
    except (OSError, subprocess.SubprocessError):
        return set()
    if result.returncode != 0:
        return set()
    filters = set()
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] and parts[0][0] in ".TSCAPN|":
            filters.add(parts[1])
    return filters


def ffmpeg_has_subtitles_filter() -> bool:
    """True when this ffmpeg can burn subtitles — its filter list includes the libass
    `subtitles` filter. The render burns even the .ass file through `subtitles=` (see
    video-assemble assemble.py:_subtitle_burn_filter), so this — not the `ass` filter — is
    the exact capability `--burn-subtitles` needs. Reused by the orchestrator preflight
    (recap.py) to fail fast before any API spend."""
    return "subtitles" in _ffmpeg_filters()


def _asr_status() -> dict[str, object]:
    configured = bool(CONFIG.get("mimo_asr_api_key"))
    return {
        "configured": configured,
        "available": configured,
        "mimo_asr_model": str(CONFIG.get("mimo_asr_model") or ""),
        "mimo_asr_api_url": str(CONFIG.get("mimo_asr_api_url") or ""),
        "mimo_asr_api_url_source": CONFIG.get("mimo_asr_api_url_source", "default"),
        "mimo_asr_language": str(CONFIG.get("mimo_asr_language") or "auto"),
        "mimo_asr_api_key_source": CONFIG.get("mimo_asr_api_key_source", "MIMO_API_KEY"),
        "note": "ASR uses MiMo (mimo-v2.5-asr); set MIMO_API_KEY, or run with --skip-asr.",
    }


def build_report() -> dict[str, object]:
    filters = _ffmpeg_filters()
    ffmpeg_path = _command_path("ffmpeg") or ""
    ffprobe_path = _command_path("ffprobe") or ""
    mimo_video_configured = bool(CONFIG.get("mimo_video_api_key"))
    mimo_tts_configured = bool(CONFIG.get("mimo_tts_api_key"))
    subtitle_filter = "subtitles" in filters
    ass_filter = "ass" in filters
    checks: dict[str, object] = {
        "system_tools": {
            "ffmpeg": bool(ffmpeg_path),
            "ffmpeg_path": ffmpeg_path,
            "ffprobe": bool(ffprobe_path),
            "ffprobe_path": ffprobe_path,
            "ffmpeg_subtitles_filter": subtitle_filter,
            "ffmpeg_ass_filter": ass_filter,
            "burn_subtitles_ready": bool(ffmpeg_path and subtitle_filter),
        },
        "tts": {
            "mimo_tts_configured": mimo_tts_configured,
            "mimo_tts_api_url": CONFIG.get("mimo_tts_api_url"),
            "mimo_tts_api_url_source": CONFIG.get("mimo_tts_api_url_source", "default"),
            "mimo_tts_model": CONFIG.get("mimo_tts_model"),
            "mimo_tts_model_source": CONFIG.get("mimo_tts_model_source", "default"),
            "mimo_tts_voice": CONFIG.get("mimo_tts_voice"),
            "mimo_tts_voice_source": CONFIG.get("mimo_tts_voice_source", "default"),
            "available": mimo_tts_configured,
        },
        "asr": _asr_status(),
        "api_config": {
            "api_provider": CONFIG.get("api_provider", "mimo"),
            "api_url": str(CONFIG.get("api_url") or ""),
            "api_url_source": CONFIG.get("api_url_source", "default"),
            "api_key_source": CONFIG.get("api_key_source", "MIMO_API_KEY"),
            "api_key_set": bool(CONFIG.get("api_key")),
            "vlm_model": CONFIG.get("vlm_model"),
            "vlm_model_source": CONFIG.get("vlm_model_source", "default"),
            "vlm_workers": CONFIG.get("vlm_workers"),
            "mimo_video_configured": mimo_video_configured,
            "mimo_video_api_url": CONFIG.get("mimo_video_api_url"),
            "mimo_video_model": CONFIG.get("mimo_video_model"),
            "mimo_video_model_source": CONFIG.get("mimo_video_model_source", "default"),
        },
        "python": {
            "executable": sys.executable,
            "version": sys.version.split()[0],
        },
    }

    failures: list[str] = []
    warnings: list[str] = []
    tools = checks["system_tools"]  # type: ignore[index]
    for name in ("ffmpeg", "ffprobe"):
        if not tools.get(name):  # type: ignore[union-attr]
            failures.append(f"Missing system tool: {name}")
    if tools.get("ffmpeg") and not tools.get("ffmpeg_subtitles_filter"):  # type: ignore[union-attr]
        warnings.append("ffmpeg lacks subtitles/libass filter; --burn-subtitles will fail")
    if not checks["api_config"].get("api_key_set"):  # type: ignore[union-attr]
        failures.append("MIMO_API_KEY is not set; ASR / VLM / TTS all require a MiMo key")
    if not checks["asr"].get("available"):  # type: ignore[union-attr]
        warnings.append("ASR not configured (MIMO_API_KEY); pipeline can run with --skip-asr")
    return {
        "ok": not failures,
        "repo_root": str(SCRIPT_DIR.parents[2]),
        "checks": checks,
        "failures": failures,
        "warnings": warnings,
    }


def _status_icon(ok: bool, *, warning: bool = False) -> str:
    if ok:
        return "✓"
    return "!" if warning else "✗"


def _print_human(report: dict[str, object]) -> None:
    checks = report["checks"]  # type: ignore[index]
    print("video-recap doctor")
    print(f"Repo root: {report['repo_root']}")

    system = checks["system_tools"]  # type: ignore[index]
    print("\n[system]")
    print(f"{_status_icon(bool(system.get('ffmpeg')))} ffmpeg: {system.get('ffmpeg_path') or 'not found'}")
    print(f"{_status_icon(bool(system.get('ffprobe')))} ffprobe: {system.get('ffprobe_path') or 'not found'}")
    print(
        f"{_status_icon(bool(system.get('ffmpeg_subtitles_filter')), warning=True)} "
        f"ffmpeg subtitles/libass filter: "
        f"{'available' if system.get('ffmpeg_subtitles_filter') else 'missing'}"
    )

    api = checks["api_config"]  # type: ignore[index]
    print("\n[api]")
    print(f"✓ API provider: {api.get('api_provider')}")
    print(f"✓ API URL: {api.get('api_url')} (source: {api.get('api_url_source')})")
    print(
        f"{_status_icon(bool(api.get('api_key_set')))} "
        f"{api.get('api_key_source')}: {'set' if api.get('api_key_set') else 'not set'}"
    )
    print(f"✓ VLM model: {api.get('vlm_model')} (source: {api.get('vlm_model_source')})")
    print(f"✓ VLM_WORKERS: {api.get('vlm_workers')}")

    asr = checks["asr"]  # type: ignore[index]
    print("\n[asr]")
    print(
        f"{_status_icon(bool(asr.get('available')), warning=True)} "
        f"MiMo ASR: {'configured' if asr.get('available') else 'not configured'} "
        f"(key: {asr.get('mimo_asr_api_key_source')})"
    )
    print(f"✓ ASR model: {asr.get('mimo_asr_model')}")
    print(f"✓ ASR API URL: {asr.get('mimo_asr_api_url')} (source: {asr.get('mimo_asr_api_url_source')})")
    print(f"✓ ASR language: {asr.get('mimo_asr_language')}")
    if not asr.get("available"):
        print(f"  note: {asr.get('note')}")

    tts = checks["tts"]  # type: ignore[index]
    print("\n[tts]")
    print(
        f"{_status_icon(bool(tts.get('available')))} MiMo TTS: "
        f"{'configured' if tts.get('mimo_tts_configured') else 'not configured'}"
    )
    print(f"✓ TTS model: {tts.get('mimo_tts_model')} (source: {tts.get('mimo_tts_model_source')})")
    print(f"✓ TTS voice: {tts.get('mimo_tts_voice')} (source: {tts.get('mimo_tts_voice_source')})")
    print(f"✓ TTS API URL: {tts.get('mimo_tts_api_url')} (source: {tts.get('mimo_tts_api_url_source')})")

    if report.get("warnings"):
        print("\nWarnings:")
        for warning in report["warnings"]:  # type: ignore[index]
            print(f"- {warning}")
    if report.get("failures"):
        print("\nStatus: FAILED")
        for failure in report["failures"]:  # type: ignore[index]
            print(f"- {failure}")
    else:
        print("\nStatus: OK")


def main() -> int:
    parser = argparse.ArgumentParser(description="Check video-recap runtime prerequisites.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    args = parser.parse_args()

    report = build_report()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["ok"] else 1

    _print_human(report)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
