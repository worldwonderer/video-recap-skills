#!/usr/bin/env python3
"""Measure a source video's burned-in subtitle band using only stdlib + ffmpeg.

The tool samples deterministic frames, detects wide bright text-like bands in the
lower half, writes grid/band previews, and emits ``subtitle_positions.json``.  The
coordinates can be passed to recap.py with ``--subtitle-y-top/--subtitle-y-bot``.
"""

import argparse
import json
import random
import shutil
import subprocess
import sys
from pathlib import Path
from statistics import median


_OWNER_MARKER = ".video_recap_subtitle_measure"
_OWNER_MARKER_CONTENT = "video-recap subtitle measurement output v1\n"


def _run(command):
    return subprocess.run(command, capture_output=True, text=True)


def _probe_video(path):
    result = _run([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height,sample_aspect_ratio:format=duration",
        "-of", "json", str(path),
    ])
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"ffprobe failed: {path}")
    data = json.loads(result.stdout or "{}")
    stream = (data.get("streams") or [{}])[0]
    width, height = int(stream.get("width") or 0), int(stream.get("height") or 0)
    duration = float((data.get("format") or {}).get("duration") or 0.0)
    sar = str(stream.get("sample_aspect_ratio") or "1:1")
    if width <= 0 or height <= 0 or duration <= 0:
        raise RuntimeError(f"无法读取视频尺寸/时长: {path}")
    return width, height, duration, sar


def _is_square_sample_aspect_ratio(value):
    try:
        num, den = str(value).split(":", 1)
        return abs(float(num) / float(den) - 1.0) < 1e-9
    except (TypeError, ValueError, ZeroDivisionError):
        return False


def _sample_times(duration, count, start_sec=10.0):
    if count <= 0:
        raise ValueError("frames must be positive")
    lo = min(max(0.0, float(start_sec)), max(0.0, duration - 0.1))
    hi = max(lo, duration - 0.1)
    if hi - lo < 0.05:
        return [lo] * count
    rng = random.Random(42)
    return sorted(lo + rng.random() * (hi - lo) for _ in range(count))


def _read_pgm(path):
    """Read ffmpeg's binary P5 PGM output without Pillow/numpy."""
    data = Path(path).read_bytes()
    pos = 0

    def token():
        nonlocal pos
        while pos < len(data):
            if data[pos] == 35:  # '#': comment
                pos = data.find(b"\n", pos)
                if pos < 0:
                    raise ValueError("unterminated PGM comment")
            elif chr(data[pos]).isspace():
                pos += 1
            else:
                break
        start = pos
        while pos < len(data) and not chr(data[pos]).isspace() and data[pos] != 35:
            pos += 1
        if start == pos:
            raise ValueError("invalid PGM header")
        return data[start:pos]

    if token() != b"P5":
        raise ValueError("only binary P5 PGM is supported")
    width, height, max_value = int(token()), int(token()), int(token())
    if max_value <= 0 or max_value > 255:
        raise ValueError("PGM must use 8-bit samples")
    if pos >= len(data) or not chr(data[pos]).isspace():
        raise ValueError("PGM header is missing pixel separator")
    pos += 1
    if pos < len(data) and data[pos - 1] == 13 and data[pos] == 10:  # CRLF
        pos += 1
    pixels = data[pos:pos + width * height]
    if len(pixels) != width * height:
        raise ValueError("truncated PGM pixels")
    return width, height, pixels


def _detect_subtitle_band(width, height, pixels):
    """Return a likely (top, bottom) bright horizontal text band, or None.

    This is deliberately heuristic: previews remain the source of truth. Requiring a
    wide union of bright columns rejects narrow highlights/logos while scaled height
    bounds work across 720p/1080p/portrait sources.
    """
    lower = height // 2
    min_bright = max(3, round(width * 0.01))
    active_rows = []
    for y in range(lower, height):
        row = pixels[y * width:(y + 1) * width]
        if sum(value >= 210 for value in row) >= min_bright:
            active_rows.append(y)
    if not active_rows:
        return None

    groups, start, previous = [], active_rows[0], active_rows[0]
    for y in active_rows[1:]:
        if y - previous > 3:
            groups.append((start, previous))
            start = y
        previous = y
    groups.append((start, previous))

    min_height = max(4, round(height * 0.008))
    max_height = max(12, round(height * 0.09))
    best = None
    for top, bottom in groups:
        band_height = bottom - top + 1
        if not min_height <= band_height <= max_height:
            continue
        # Bright glyph rows usually exclude the dark outline itself; widen slightly so
        # contrast scoring and returned coordinates include the whole rendered text band.
        top = max(lower, top - 1)
        bottom = min(height - 1, bottom + 1)
        band_height = bottom - top + 1
        bright_columns = 0
        dark_pixels = 0
        for x in range(width):
            column_has_bright = False
            for y in range(top, bottom + 1):
                value = pixels[y * width + x]
                column_has_bright = column_has_bright or value >= 210
                dark_pixels += value <= 70
            bright_columns += int(column_has_bright)
        coverage = bright_columns / width
        # Real outlined subtitles normally have some dark pixels around the bright glyphs.
        contrast = dark_pixels / max(1, width * band_height)
        if coverage < 0.12 or contrast < 0.01:
            continue
        score = coverage + contrast * 0.2 + (top / height) * 0.05
        if best is None or score > best[0]:
            best = (score, top, bottom)
    return (best[1], best[2]) if best else None


def _extract_gray_frame(video, timestamp, output):
    result = _run([
        "ffmpeg", "-v", "error", "-y", "-ss", f"{timestamp:.3f}", "-i", str(video),
        "-frames:v", "1", "-vf", "format=gray", str(output),
    ])
    if result.returncode != 0 or not output.exists():
        raise RuntimeError(result.stderr.strip() or f"frame extraction failed at {timestamp:.3f}s")


def _write_preview(video, timestamp, output, band):
    top, bottom = band
    filters = (
        "drawgrid=width=100:height=50:thickness=1:color=yellow@0.45,"
        f"drawbox=x=0:y={top}:w=iw:h={bottom - top + 1}:color=red@0.9:t=2"
    )
    result = _run([
        "ffmpeg", "-v", "error", "-y", "-ss", f"{timestamp:.3f}", "-i", str(video),
        "-frames:v", "1", "-vf", filters, str(output),
    ])
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"preview render failed at {timestamp:.3f}s")


def _write_positions(path, width, height, y_top, y_bot):
    payload = {
        "canvas": {"width": int(width), "height": int(height)},
        "subtitle_y_top": int(y_top),
        "subtitle_y_bot": int(y_bot),
    }
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


def _load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _prompt_coordinate(label, suggested):
    try:
        value = input(f"{label} [{suggested}]: ").strip()
    except EOFError:
        return suggested
    if not value:
        return suggested
    try:
        return int(value)
    except ValueError:
        print(f"无效输入，使用 {suggested}")
        return suggested


def _prepare_output_dir(out_dir):
    """Reset only artifacts in a directory previously claimed by this tool."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    marker = out_dir / _OWNER_MARKER
    if marker.is_symlink() or (marker.exists() and not marker.is_file()):
        raise RuntimeError(f"输出目录所有权标记类型不安全: {marker}")
    if marker.is_file():
        if marker.read_text(encoding="utf-8") != _OWNER_MARKER_CONTENT:
            raise RuntimeError(f"输出目录所有权标记无效: {marker}")
    else:
        # Never claim an existing user directory merely because our conventional artifact
        # names are absent. A typo such as --out-dir ~/Videos must fail without side effects.
        if any(out_dir.iterdir()):
            raise RuntimeError(f"拒绝认领非空且未标记的输出目录: {out_dir}")
        marker.write_text(_OWNER_MARKER_CONTENT, encoding="utf-8")
    for name in ("frames", "preview"):
        managed_dir = out_dir / name
        if managed_dir.is_symlink() or managed_dir.is_file():
            managed_dir.unlink()
        elif managed_dir.is_dir():
            shutil.rmtree(managed_dir)
    positions = out_dir / "subtitle_positions.json"
    if positions.is_symlink() or positions.is_file():
        positions.unlink()
    frames_dir, preview_dir = out_dir / "frames", out_dir / "preview"
    frames_dir.mkdir()
    preview_dir.mkdir()
    return frames_dir, preview_dir


def main(argv=None):
    parser = argparse.ArgumentParser(description="检测并标注原视频烧录字幕的 Y 坐标")
    parser.add_argument("video")
    parser.add_argument("--out-dir", default=None)
    parser.add_argument("--frames", type=int, default=50)
    parser.add_argument("--start-sec", type=float, default=10.0)
    parser.add_argument("--accept-detected", action="store_true",
                        help="non-interactive: accept median detected coordinates")
    args = parser.parse_args(argv)

    video = Path(args.video).expanduser().resolve()
    if not video.is_file():
        parser.error(f"视频不存在: {video}")
    if args.frames <= 0:
        parser.error("--frames must be positive")
    _storage_width, _storage_height, duration, sar = _probe_video(video)
    if not _is_square_sample_aspect_ratio(sar):
        parser.error(
            f"当前坐标测量仅支持方形像素视频 (SAR 1:1)；当前 SAR={sar}"
        )
    out_dir = Path(args.out_dir).expanduser().resolve() if args.out_dir else video.parent / ".subtitle_measure"
    frames_dir, preview_dir = _prepare_output_dir(out_dir)

    detections = []
    canvas_width = canvas_height = None
    for index, timestamp in enumerate(_sample_times(duration, args.frames, args.start_sec)):
        pgm = frames_dir / f"frame_{index:03d}_{timestamp:.2f}s.pgm"
        _extract_gray_frame(video, timestamp, pgm)
        frame_w, frame_h, pixels = _read_pgm(pgm)
        if canvas_width is None:
            canvas_width, canvas_height = frame_w, frame_h
        elif (frame_w, frame_h) != (canvas_width, canvas_height):
            raise RuntimeError(
                f"抽帧尺寸不一致: {(frame_w, frame_h)} != {(canvas_width, canvas_height)}"
            )
        band = _detect_subtitle_band(frame_w, frame_h, pixels)
        pgm.unlink(missing_ok=True)
        if band:
            preview = preview_dir / f"frame_{index:03d}_{timestamp:.2f}s.png"
            _write_preview(video, timestamp, preview, band)
            detections.append(band)

    if not detections:
        raise SystemExit("未检测到可靠字幕带；可增加 --frames 或降低 --start-sec 后重试")
    suggested_top = round(median(top for top, _ in detections))
    suggested_bot = round(median(bottom for _, bottom in detections))
    width, height = int(canvas_width), int(canvas_height)
    print(f"检测到字幕帧 {len(detections)}/{args.frames}，预览: {preview_dir}")
    print(f"建议字幕带: y=[{suggested_top}, {suggested_bot}]")
    if args.accept_detected:
        y_top, y_bot = suggested_top, suggested_bot
    else:
        print("请查看红框预览；直接回车接受建议值。")
        y_top = _prompt_coordinate("字幕上沿 y_top", suggested_top)
        y_bot = _prompt_coordinate("字幕下沿 y_bot", suggested_bot)
    if not 0 <= y_top < y_bot <= height:
        raise SystemExit(f"坐标无效，必须满足 0 <= top < bot <= {height}")

    positions = out_dir / "subtitle_positions.json"
    _write_positions(positions, width, height, y_top, y_bot)
    print(f"坐标文件: {positions}")
    print(f"使用: python3 skills/video-recap/scripts/recap.py {video} "
          f"--subtitle-y-top {y_top} --subtitle-y-bot {y_bot}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
