"""Local ffmpeg capability preflight for subtitle burn-in."""

from lib import CONFIG

def _ffmpeg_filters():
    """Return ffmpeg's compiled-in filter names.

    This skill keeps a local capability probe so it remains self-contained; keep the
    parser behavior aligned through tests.
    """
    import shutil
    import subprocess
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return set()
    try:
        result = subprocess.run(["ffmpeg", "-hide_banner", "-filters"],
                                text=True, capture_output=True, timeout=20)
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


def _preflight_burn_subtitles():
    """Fail before the (re-encoding) render when burn-in is on but ffmpeg lacks the libass
    `subtitles` filter. _subtitle_burn_filter burns even the .ass through `subtitles=`, so
    that is the required capability. Defense-in-depth: the orchestrator (recap.py) preflights
    this earlier, but assemble.py can be run standalone. Only fires when ffmpeg EXISTS but
    can't burn — an absent ffmpeg fails the render regardless and would also break the mocked,
    ffmpeg-less test environment."""
    import shutil
    if not CONFIG.get("burn_subtitles", False):
        return
    if shutil.which("ffmpeg") is None:
        return
    if "subtitles" not in _ffmpeg_filters():
        raise SystemExit(
            "字幕烧录已开启，但当前 ffmpeg 不支持 subtitles/libass 滤镜，渲染会在最后一步失败。\n"
            "  解决：安装带 libass 的 ffmpeg，或加 --no-burn-subtitles 关闭烧录（仍输出 .srt 外挂字幕）。")
