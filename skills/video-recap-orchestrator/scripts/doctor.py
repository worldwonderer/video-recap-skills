#!/usr/bin/env python3
"""Environment doctor for the video-recap skill."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
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


def _check_tts_smoke(voice: str) -> dict[str, object]:
    edge_tts = _command_path("edge-tts")
    if not edge_tts:
        return {"ok": False, "skipped": True, "reason": "edge-tts not found"}
    ffprobe = _command_path("ffprobe")
    if not ffprobe:
        return {"ok": False, "skipped": True, "reason": "ffprobe not found"}
    with tempfile.TemporaryDirectory(prefix="video-recap-tts-smoke-") as tmp:
        media = Path(tmp) / "smoke.mp3"
        try:
            result = _run([
                edge_tts,
                "--voice", voice,
                "--text", "测试一下。",
                "--write-media", str(media),
            ], timeout=60)
        except subprocess.TimeoutExpired:
            # 网络挂起（edge-tts 联网失败）时跳过冒烟测试而非崩溃
            return {"ok": False, "skipped": True, "reason": "edge-tts smoke test timed out"}
        except OSError as exc:
            return {"ok": False, "skipped": True, "reason": f"edge-tts smoke test error: {exc}"}
        if result.returncode != 0:
            return {"ok": False, "error": (result.stderr or result.stdout)[-500:]}
        try:
            probe = _run([
                ffprobe, "-v", "quiet", "-show_entries", "format=duration",
                "-of", "csv=p=0", str(media),
            ])
        except (subprocess.TimeoutExpired, OSError) as exc:
            return {"ok": False, "skipped": True, "reason": f"ffprobe smoke check error: {exc}"}
        try:
            duration = float(probe.stdout.strip())
        except (TypeError, ValueError):
            duration = 0.0
        return {"ok": duration > 0, "duration": duration}


def _asr_status() -> dict[str, object]:
    asr_bin = str(CONFIG.get("asr_bin") or "")
    asr_model_dir = str(CONFIG.get("asr_model_dir") or "")
    model_path = Path(asr_model_dir).expanduser() if asr_model_dir else None
    model_exists = bool(model_path and model_path.exists())
    bin_path = _command_path(asr_bin)
    configured = bool(asr_bin and asr_model_dir)
    available = bool(bin_path and model_exists)
    return {
        "configured": configured,
        "available": available,
        "asr_bin": asr_bin,
        "asr_bin_path": bin_path or "",
        "asr_model_dir": asr_model_dir,
        "asr_model_dir_exists": model_exists,
        "note": "ASR is optional; use --skip-asr or set ASR_BIN/ASR_MODEL_DIR when unavailable.",
    }


def build_report(*, tts_smoke: bool = False) -> dict[str, object]:
    filters = _ffmpeg_filters()
    ffmpeg_path = _command_path("ffmpeg") or ""
    ffprobe_path = _command_path("ffprobe") or ""
    edge_tts_path = _command_path("edge-tts") or ""
    edge_tts_module = importlib.util.find_spec("edge_tts") is not None
    api_url = str(CONFIG.get("api_url") or "")
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
            "edge_tts_command": bool(edge_tts_path),
            "edge_tts_path": edge_tts_path,
            "edge_tts_module": edge_tts_module,
            "mimo_tts_configured": mimo_tts_configured,
            "mimo_tts_api_url": CONFIG.get("mimo_tts_api_url"),
            "mimo_tts_api_url_source": CONFIG.get("mimo_tts_api_url_source", "default"),
            "mimo_tts_voice": CONFIG.get("mimo_tts_voice"),
            "mimo_tts_voice_source": CONFIG.get("mimo_tts_voice_source", "default"),
            "default_engine": CONFIG.get("tts_engine", "auto"),
            "default_engine_source": CONFIG.get("tts_engine_source", "default"),
            "default_voice": CONFIG.get("edge_tts_voice"),
            "available": bool(edge_tts_path or edge_tts_module or mimo_tts_configured),
        },
        "asr": _asr_status(),
        "api_config": {
            "api_provider": CONFIG.get("api_provider", "openai"),
            "api_provider_source": CONFIG.get("api_provider_source", "default"),
            "api_url": api_url,
            "api_url_source": CONFIG.get("api_url_source", "default"),
            "api_key_source": CONFIG.get("api_key_source", "OPENAI_API_KEY"),
            "api_key_set": bool(CONFIG.get("api_key")),
            "vlm_model": CONFIG.get("vlm_model"),
            "vlm_model_source": CONFIG.get("vlm_model_source", "default"),
            "vlm_workers": CONFIG.get("vlm_workers"),
            "mimo_video_configured": mimo_video_configured,
            "mimo_video_api_url": CONFIG.get("mimo_video_api_url"),
            "mimo_video_api_url_source": CONFIG.get("mimo_video_api_url_source", "default"),
            "mimo_video_model": CONFIG.get("mimo_video_model"),
            "mimo_video_model_source": CONFIG.get("mimo_video_model_source", "default"),
            "mimo_tts_configured": mimo_tts_configured,
            "mimo_tts_api_url": CONFIG.get("mimo_tts_api_url"),
            "mimo_tts_api_url_source": CONFIG.get("mimo_tts_api_url_source", "default"),
            "mimo_tts_model": CONFIG.get("mimo_tts_model"),
            "mimo_tts_model_source": CONFIG.get("mimo_tts_model_source", "default"),
        },
        "python": {
            "executable": sys.executable,
            "version": sys.version.split()[0],
        },
    }
    if tts_smoke:
        checks["tts_smoke"] = _check_tts_smoke(str(CONFIG.get("edge_tts_voice") or "zh-CN-YunxiNeural"))

    failures: list[str] = []
    warnings: list[str] = []
    tools = checks["system_tools"]  # type: ignore[index]
    for name in ("ffmpeg", "ffprobe"):
        if not tools.get(name):  # type: ignore[union-attr]
            failures.append(f"Missing system tool: {name}")
    if tools.get("ffmpeg") and not tools.get("ffmpeg_subtitles_filter"):  # type: ignore[union-attr]
        warnings.append("ffmpeg lacks subtitles/libass filter; --burn-subtitles will fail")
    tts = checks["tts"]  # type: ignore[index]
    if not tts.get("available"):  # type: ignore[union-attr]
        failures.append("Missing TTS engine; install edge-tts or configure MiMo TTS")
    if tts_smoke:
        smoke = checks.get("tts_smoke", {})  # type: ignore[assignment]
        if smoke.get("skipped"):  # type: ignore[union-attr]
            # 跳过（缺 edge-tts/ffprobe 或网络挂起）不算硬失败，例如纯 MiMo TTS 环境
            warnings.append(f"edge-tts smoke test skipped: {smoke.get('reason') or 'unavailable'}")  # type: ignore[union-attr]
        elif not smoke.get("ok"):  # type: ignore[union-attr]
            failures.append("edge-tts smoke test failed")
    asr = checks["asr"]  # type: ignore[index]
    if not asr.get("available"):  # type: ignore[union-attr]
        warnings.append("ASR is not fully configured; pipeline can run with --skip-asr or continue without ASR on failure")
    if not checks["api_config"].get("api_key_set"):  # type: ignore[union-attr]
        source = checks["api_config"].get("api_key_source")  # type: ignore[union-attr]
        warnings.append(f"{source} is not set; VLM analysis will fail until configured")
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

    tts = checks["tts"]  # type: ignore[index]
    print("\n[tts]")
    print(
        f"{_status_icon(bool(tts.get('available')))} available engine: "
        f"{tts.get('default_engine')} (source: {tts.get('default_engine_source')})"
    )
    print(f"{_status_icon(bool(tts.get('edge_tts_command')), warning=True)} edge-tts command: {tts.get('edge_tts_path') or 'not found'}")
    print(f"{_status_icon(bool(tts.get('edge_tts_module')), warning=True)} edge-tts module: {tts.get('edge_tts_module')}")
    print(f"{_status_icon(bool(tts.get('mimo_tts_configured')), warning=True)} mimo-tts: {'configured' if tts.get('mimo_tts_configured') else 'not configured'}")
    print(f"✓ default voice: {tts.get('default_voice')}")
    print(f"✓ MiMo TTS voice: {tts.get('mimo_tts_voice')} (source: {tts.get('mimo_tts_voice_source')})")
    print(f"✓ MiMo TTS API URL: {tts.get('mimo_tts_api_url')} (source: {tts.get('mimo_tts_api_url_source')})")

    asr = checks["asr"]  # type: ignore[index]
    print("\n[asr]")
    print(f"{_status_icon(bool(asr.get('asr_bin_path')), warning=True)} ASR_BIN: {asr.get('asr_bin_path') or asr.get('asr_bin') or 'not set'}")
    print(f"{_status_icon(bool(asr.get('asr_model_dir_exists')), warning=True)} ASR_MODEL_DIR: {asr.get('asr_model_dir') or 'not set'}")
    if not asr.get("available"):
        print(f"  note: {asr.get('note')}")

    api = checks["api_config"]  # type: ignore[index]
    print("\n[api]")
    print(f"✓ API provider: {api.get('api_provider')} (source: {api.get('api_provider_source')})")
    print(f"✓ API URL: {api.get('api_url')} (source: {api.get('api_url_source')})")
    print(
        f"{_status_icon(bool(api.get('api_key_set')), warning=True)} "
        f"{api.get('api_key_source')}: {'set' if api.get('api_key_set') else 'not set'}"
    )
    print(f"✓ VLM model: {api.get('vlm_model')} (source: {api.get('vlm_model_source')})")
    print(f"✓ VLM_WORKERS: {api.get('vlm_workers')}")
    print(
        f"{_status_icon(bool(api.get('mimo_video_configured')), warning=True)} "
        f"MiMo video: {'configured' if api.get('mimo_video_configured') else 'not configured'}"
    )
    print(f"✓ MiMo video API URL: {api.get('mimo_video_api_url')} (source: {api.get('mimo_video_api_url_source')})")
    print(f"✓ MiMo video model: {api.get('mimo_video_model')} (source: {api.get('mimo_video_model_source')})")
    print(f"✓ MiMo TTS model: {api.get('mimo_tts_model')} (source: {api.get('mimo_tts_model_source')})")

    if "tts_smoke" in checks:
        smoke = checks["tts_smoke"]  # type: ignore[index]
        print("\n[tts_smoke]")
        skipped = bool(smoke.get("skipped"))
        print(f"{_status_icon(bool(smoke.get('ok')), warning=skipped)} result: {smoke}")

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
    parser.add_argument("--tts-smoke", action="store_true", help="Run a short edge-tts synthesis test")
    args = parser.parse_args()

    report = build_report(tts_smoke=args.tts_smoke)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["ok"] else 1

    _print_human(report)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
