"""SRT/ASS serialization for narration and original-dialogue subtitles."""

from source_subtitles import _combined_subtitle_entries
from subtitle_core import (
    _normalize_subtitle_text,
    _seconds_to_ass_time,
    _seconds_to_srt_time,
    _style_for_measured_subtitle_band,
    _subtitle_entries,
    _subtitle_style_config,
)

def _generate_srt(narration, work_dir, video_duration=None):
    """将解说脚本转为 SRT 字幕文件，使用实际音频放置时间。video_duration 给定时，原声留白处补烧原声字幕。"""
    srt_lines = []
    entries = (_subtitle_entries(narration) if video_duration is None
               else _combined_subtitle_entries(narration, work_dir, video_duration))
    # entries are already split into short one-line chunks, so no wrapping here.
    for idx, entry in enumerate(entries, start=1):
        start_ts = _seconds_to_srt_time(entry["start"])
        end_ts = _seconds_to_srt_time(entry["end"])
        srt_lines.append(str(idx))
        srt_lines.append(f"{start_ts} --> {end_ts}")
        srt_lines.append(_normalize_subtitle_text(entry["text"]))
        srt_lines.append("")
    srt_path = work_dir / "subtitles.srt"
    srt_path.write_text("\n".join(srt_lines), encoding="utf-8")
    return srt_path


def _escape_ass_text(text):
    """Escape user text for an ASS dialogue Text field."""
    return (
        str(text)
        .replace("\\", "\\\\")
        .replace("{", "\\{")
        .replace("}", "\\}")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\n", "\\N")
    )


def _generate_ass(narration, work_dir, video_duration=None, canvas=None):
    """Generate an ASS subtitle file for readable hard-sub rendering. video_duration given ⇒ also
    burn the original dialogue (from ASR) during the original-audio gaps. canvas ({"width","height"})
    scales the style to the real frame so portrait/竖屏 subtitles are not stretched."""
    style = _style_for_measured_subtitle_band(_subtitle_style_config(canvas), canvas)
    ass_lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        f"PlayResX: {int(style['play_res_x'])}",
        f"PlayResY: {int(style['play_res_y'])}",
        "",
        "[V4+ Styles]",
        (
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
            "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
            "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
            "Alignment, MarginL, MarginR, MarginV, Encoding"
        ),
        (
            "Style: Default,"
            f"{style['font_name']},{style['font_size']},{style['primary_color']},&H000000FF,"
            f"{style['outline_color']},&H64000000,0,0,0,0,100,100,0,0,1,"
            f"{style['outline']},{style['shadow']},{style['alignment']},"
            f"{style['margin_l']},{style['margin_r']},{style['margin_v']},1"
        ),
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    entries = (_subtitle_entries(narration) if video_duration is None
               else _combined_subtitle_entries(narration, work_dir, video_duration))
    # entries are already split into short one-line chunks, so no wrapping here.
    for entry in entries:
        text = _escape_ass_text(_normalize_subtitle_text(entry["text"]))
        ass_lines.append(
            "Dialogue: 0,"
            f"{_seconds_to_ass_time(entry['start'])},{_seconds_to_ass_time(entry['end'])},"
            f"Default,,0,0,0,,{text}"
        )

    ass_path = work_dir / "subtitles.ass"
    ass_path.write_text("\n".join(ass_lines) + "\n", encoding="utf-8")
    return ass_path
