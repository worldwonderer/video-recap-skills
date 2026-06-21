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
from typing import Any, cast

from lib import CONFIG


SCRIPT_DIR = Path(__file__).resolve().parent
DEGRADED_GROUP = "warnings/degraded"


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


def _capability(name: str, summary: str, *, detail: str = "", action: str = "") -> dict[str, str]:
    item = {"name": name, "summary": summary}
    if detail:
        item["detail"] = detail
    if action:
        item["action"] = action
    return item


def _build_capability_menu(checks: dict[str, object]) -> dict[str, list[dict[str, str]]]:
    """Human-ready preflight summary grouped by what can run, what blocks, and what degrades.

    This is intentionally a small rollup over the existing `checks` tree. It does not replace
    the raw machine checks, install anything, or introduce provider ranking.
    """
    system = cast(dict[str, Any], checks["system_tools"])
    api = cast(dict[str, Any], checks["api_config"])
    asr = cast(dict[str, Any], checks["asr"])
    tts = cast(dict[str, Any], checks["tts"])

    menu: dict[str, list[dict[str, str]]] = {
        "ready": [],
        "blocked": [],
        DEGRADED_GROUP: [],
        "optional_upgrades": [],
    }

    ffmpeg_ready = bool(system.get("ffmpeg"))
    ffprobe_ready = bool(system.get("ffprobe"))
    subtitles_ready = bool(system.get("burn_subtitles_ready"))
    api_key_set = bool(api.get("api_key_set"))
    asr_ready = bool(asr.get("available"))
    tts_ready = bool(tts.get("available"))
    vlm_ready = bool(api.get("mimo_video_configured"))
    normal_core_ready = ffmpeg_ready and ffprobe_ready and api_key_set and vlm_ready and tts_ready

    if ffmpeg_ready and ffprobe_ready:
        menu["ready"].append(
            _capability(
                "core_media_tools",
                "ffmpeg and ffprobe are available",
                detail="Local probing, cutting, rendering, and duration checks can run.",
            )
        )
    else:
        if not ffmpeg_ready:
            menu["blocked"].append(
                _capability("ffmpeg", "Missing ffmpeg", action="Install ffmpeg before running the recap pipeline.")
            )
        if not ffprobe_ready:
            menu["blocked"].append(
                _capability("ffprobe", "Missing ffprobe", action="Install ffprobe before running media probing/export.")
            )

    if api_key_set:
        menu["ready"].append(
            _capability(
                "mimo_credentials",
                "MiMo API key is configured",
                detail=f"Source: {api.get('api_key_source')}",
            )
        )
    else:
        menu["blocked"].append(
            _capability(
                "mimo_credentials",
                "Missing MIMO_API_KEY",
                action="Set MIMO_API_KEY; the default ASR / VLM / TTS path depends on it.",
            )
        )

    if vlm_ready:
        menu["ready"].append(
            _capability(
                "mimo_vlm",
                "MiMo VLM/video understanding is configured",
                detail=f"Model: {api.get('vlm_model')}",
            )
        )
    elif api_key_set:
        menu["blocked"].append(
            _capability(
                "mimo_vlm",
                "MiMo VLM/video understanding is not configured",
                action="Set MIMO_VIDEO_API_KEY or the shared MIMO_API_KEY before video understanding.",
            )
        )

    if tts_ready:
        menu["ready"].append(
            _capability(
                "mimo_tts",
                "MiMo TTS is configured",
                detail=f"Voice: {tts.get('mimo_tts_voice')}; model: {tts.get('mimo_tts_model')}",
            )
        )
    elif api_key_set:
        menu["blocked"].append(
            _capability(
                "mimo_tts",
                "MiMo TTS is not configured",
                action="Set MIMO_TTS_API_KEY or the shared MIMO_API_KEY before voiceover.",
            )
        )

    if asr_ready:
        menu["ready"].append(
            _capability(
                "mimo_asr",
                "MiMo ASR is configured",
                detail=f"Language: {asr.get('mimo_asr_language')}; model: {asr.get('mimo_asr_model')}",
            )
        )
    else:
        menu[DEGRADED_GROUP].append(
            _capability(
                "mimo_asr",
                "ASR is unavailable; run only with --skip-asr",
                action=str(asr.get("note") or "Set MIMO_ASR_API_KEY or MIMO_API_KEY to enable ASR."),
            )
        )

    if subtitles_ready:
        menu["ready"].append(
            _capability("subtitle_burn", "Subtitle burn-in is available", detail="ffmpeg has the subtitles/libass filter.")
        )
    elif ffmpeg_ready:
        menu[DEGRADED_GROUP].append(
            _capability(
                "subtitle_burn",
                "Subtitle burn-in is unavailable",
                action="Use --no-burn-subtitles or install an ffmpeg build with the subtitles/libass filter.",
            )
        )

    if not normal_core_ready:
        menu["blocked"].append(
            _capability(
                "default_recap_pipeline",
                "Default recap run is blocked",
                detail="Resolve the blocking items above before a normal run.",
            )
        )
    elif asr_ready and subtitles_ready:
        menu["ready"].append(
            _capability("default_recap_pipeline", "Default recap run is ready", detail="ASR, VLM, TTS, and media tools are configured.")
        )
    else:
        actions = []
        if not asr_ready:
            actions.append("run with --skip-asr")
        if not subtitles_ready:
            actions.append("run with --no-burn-subtitles")
        menu[DEGRADED_GROUP].append(
            _capability(
                "recap_degraded_mode",
                "Recap can run only in an explicit degraded mode",
                detail="; ".join(actions),
            )
        )

    menu["optional_upgrades"].append(
        _capability(
            "jianying_export",
            "Editable JianYing draft export can be requested with --export-jianying",
            detail="No JianYing install is required to write the draft; ffprobe improves media metadata.",
        )
    )
    if subtitles_ready:
        menu["optional_upgrades"].append(
            _capability(
                "burned_subtitles",
                "Burned subtitles are available and enabled by default",
                action="Use --no-burn-subtitles if you prefer external subtitle files.",
            )
        )

    return menu


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
    tools = cast(dict[str, Any], checks["system_tools"])
    api_config = cast(dict[str, Any], checks["api_config"])
    asr_check = cast(dict[str, Any], checks["asr"])
    for name in ("ffmpeg", "ffprobe"):
        if not tools.get(name):
            failures.append(f"Missing system tool: {name}")
    if tools.get("ffmpeg") and not tools.get("ffmpeg_subtitles_filter"):
        warnings.append("ffmpeg lacks subtitles/libass filter; --burn-subtitles will fail")
    if not api_config.get("api_key_set"):
        failures.append("MIMO_API_KEY is not set; ASR / VLM / TTS all require a MiMo key")
    if not asr_check.get("available"):
        warnings.append("ASR not configured (MIMO_API_KEY); pipeline can run with --skip-asr")
    capability_menu = _build_capability_menu(checks)
    return {
        "ok": not failures,
        "repo_root": str(SCRIPT_DIR.parents[2]),
        "checks": checks,
        "capability_menu": capability_menu,
        "failures": failures,
        "warnings": warnings,
    }


def _status_icon(ok: bool, *, warning: bool = False) -> str:
    if ok:
        return "✓"
    return "!" if warning else "✗"


def _print_human(report: dict[str, object]) -> None:
    checks = cast(dict[str, Any], report["checks"])
    print("video-recap doctor")
    print(f"Repo root: {report['repo_root']}")

    system = cast(dict[str, Any], checks["system_tools"])
    print("\n[system]")
    print(f"{_status_icon(bool(system.get('ffmpeg')))} ffmpeg: {system.get('ffmpeg_path') or 'not found'}")
    print(f"{_status_icon(bool(system.get('ffprobe')))} ffprobe: {system.get('ffprobe_path') or 'not found'}")
    print(
        f"{_status_icon(bool(system.get('ffmpeg_subtitles_filter')), warning=True)} "
        f"ffmpeg subtitles/libass filter: "
        f"{'available' if system.get('ffmpeg_subtitles_filter') else 'missing'}"
    )

    api = cast(dict[str, Any], checks["api_config"])
    print("\n[api]")
    print(f"✓ API provider: {api.get('api_provider')}")
    print(f"✓ API URL: {api.get('api_url')} (source: {api.get('api_url_source')})")
    print(
        f"{_status_icon(bool(api.get('api_key_set')))} "
        f"{api.get('api_key_source')}: {'set' if api.get('api_key_set') else 'not set'}"
    )
    print(f"✓ VLM model: {api.get('vlm_model')} (source: {api.get('vlm_model_source')})")
    print(f"✓ VLM_WORKERS: {api.get('vlm_workers')}")

    asr = cast(dict[str, Any], checks["asr"])
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

    tts = cast(dict[str, Any], checks["tts"])
    print("\n[tts]")
    print(
        f"{_status_icon(bool(tts.get('available')))} MiMo TTS: "
        f"{'configured' if tts.get('mimo_tts_configured') else 'not configured'}"
    )
    print(f"✓ TTS model: {tts.get('mimo_tts_model')} (source: {tts.get('mimo_tts_model_source')})")
    print(f"✓ TTS voice: {tts.get('mimo_tts_voice')} (source: {tts.get('mimo_tts_voice_source')})")
    print(f"✓ TTS API URL: {tts.get('mimo_tts_api_url')} (source: {tts.get('mimo_tts_api_url_source')})")

    menu = cast(dict[str, list[dict[str, str]]], report.get("capability_menu") or {})
    print("\n[capability menu]")
    for group in ("ready", "blocked", DEGRADED_GROUP, "optional_upgrades"):
        print(f"{group}:")
        items = menu.get(group) or []
        if not items:
            print("  - none")
            continue
        for item in items:
            line = f"  - {item.get('name')}: {item.get('summary')}"
            if item.get("detail"):
                line += f" ({item['detail']})"
            print(line)
            if item.get("action"):
                print(f"    action: {item['action']}")

    warnings = cast(list[str], report.get("warnings") or [])
    failures = cast(list[str], report.get("failures") or [])
    if warnings:
        print("\nWarnings:")
        for warning in warnings:
            print(f"- {warning}")
    if failures:
        print("\nStatus: FAILED")
        for failure in failures:
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
