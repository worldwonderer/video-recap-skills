import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "measure_subtitle", ROOT / "tools" / "measure_subtitle.py"
)
measure = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(measure)


def test_read_pgm_and_detect_horizontal_subtitle_band(tmp_path):
    width = height = 100
    pixels = bytearray([120] * (width * height))
    # A bright, wide text-like run with a dark outline in the lower half.
    for y in range(70, 80):
        for x in range(18, 82):
            pixels[y * width + x] = 20 if y in {70, 79} else 235
    pgm = tmp_path / "frame.pgm"
    pgm.write_bytes(f"P5\n# fixture\n{width} {height}\n255\n".encode() + pixels)

    read_w, read_h, read_pixels = measure._read_pgm(pgm)
    band = measure._detect_subtitle_band(read_w, read_h, read_pixels)

    assert (read_w, read_h) == (100, 100)
    assert band is not None
    assert 70 <= band[0] <= 72
    assert 77 <= band[1] <= 80


def test_detect_subtitle_band_rejects_narrow_glint():
    width = height = 100
    pixels = bytearray([100] * (width * height))
    for y in range(70, 80):
        for x in range(48, 52):
            pixels[y * width + x] = 240

    assert measure._detect_subtitle_band(width, height, bytes(pixels)) is None


def test_write_positions_json_is_recap_cli_compatible(tmp_path):
    out = tmp_path / "subtitle_positions.json"
    measure._write_positions(out, 1280, 720, 610, 660)

    assert measure._load_json(out) == {
        "canvas": {"width": 1280, "height": 720},
        "subtitle_y_top": 610,
        "subtitle_y_bot": 660,
    }


def test_prepare_output_dir_preserves_unrelated_files(tmp_path):
    out = tmp_path / "custom-output"
    (out / "frames").mkdir(parents=True)
    (out / "frames" / "old.pgm").write_bytes(b"old")
    (out / "preview").mkdir()
    (out / "preview" / "old.png").write_bytes(b"old")
    (out / "subtitle_positions.json").write_text("{}", encoding="utf-8")
    unrelated = out / "keep-me.txt"
    unrelated.write_text("important", encoding="utf-8")

    try:
        measure._prepare_output_dir(out)
    except RuntimeError as exc:
        assert "未标记的输出目录" in str(exc)
    else:
        raise AssertionError("an unmanaged directory must not be cleaned")

    assert unrelated.read_text(encoding="utf-8") == "important"
    assert (out / "frames" / "old.pgm").read_bytes() == b"old"
    assert (out / "preview" / "old.png").read_bytes() == b"old"
    assert (out / "subtitle_positions.json").exists()


def test_prepare_output_dir_cleans_marker_owned_artifacts_only(tmp_path):
    out = tmp_path / "owned"
    out.mkdir()
    (out / measure._OWNER_MARKER).write_text(measure._OWNER_MARKER_CONTENT, encoding="utf-8")
    (out / "frames").mkdir()
    (out / "frames" / "old.pgm").write_bytes(b"old")
    unrelated = out / "keep-me.txt"
    unrelated.write_text("important", encoding="utf-8")

    frames, preview = measure._prepare_output_dir(out)

    assert list(frames.iterdir()) == []
    assert list(preview.iterdir()) == []
    assert unrelated.read_text(encoding="utf-8") == "important"


def test_prepare_output_dir_refuses_to_claim_nonempty_unmanaged_directory(tmp_path):
    out = tmp_path / "videos"
    out.mkdir()
    sentinel = out / "movie.mp4"
    sentinel.write_bytes(b"important")

    try:
        measure._prepare_output_dir(out)
    except RuntimeError as exc:
        assert "拒绝认领非空" in str(exc)
    else:
        raise AssertionError("a non-empty unmanaged directory must not be claimed")

    assert sentinel.read_bytes() == b"important"
    assert not (out / measure._OWNER_MARKER).exists()


def test_prepare_output_dir_rejects_forged_marker(tmp_path):
    out = tmp_path / "forged"
    out.mkdir()
    marker = out / measure._OWNER_MARKER
    marker.write_text("not this tool\n", encoding="utf-8")

    try:
        measure._prepare_output_dir(out)
    except RuntimeError as exc:
        assert "所有权标记无效" in str(exc)
    else:
        raise AssertionError("an invalid marker must not authorize cleanup")


def test_main_uses_auto_rotated_frame_dimensions_for_canvas(monkeypatch, tmp_path):
    video = tmp_path / "rotated.mp4"
    video.write_bytes(b"video")
    out = tmp_path / "measure"
    width, height = 180, 320
    pixels = bytearray([120] * (width * height))
    for y in range(250, 262):
        for x in range(25, 155):
            pixels[y * width + x] = 20 if y in {250, 261} else 235

    monkeypatch.setattr(measure, "_probe_video", lambda path: (320, 180, 5.0, "1:1"))
    monkeypatch.setattr(measure, "_sample_times", lambda *args: [1.0])

    def fake_extract(video_path, timestamp, output):
        output.write_bytes(f"P5\n{width} {height}\n255\n".encode() + pixels)

    monkeypatch.setattr(measure, "_extract_gray_frame", fake_extract)
    monkeypatch.setattr(measure, "_write_preview", lambda *args: args[2].write_bytes(b"png"))

    assert measure.main([str(video), "--out-dir", str(out), "--frames", "1", "--accept-detected"]) == 0

    positions = json.loads((out / "subtitle_positions.json").read_text(encoding="utf-8"))
    assert positions["canvas"] == {"width": 180, "height": 320}
    assert 245 < positions["subtitle_y_top"] < positions["subtitle_y_bot"] < 270
    assert list((out / "frames").iterdir()) == []


def test_main_rejects_non_square_pixel_coordinate_domain(monkeypatch, tmp_path, capsys):
    video = tmp_path / "anamorphic.mp4"
    video.write_bytes(b"video")
    out = tmp_path / "measure"
    monkeypatch.setattr(measure, "_probe_video", lambda path: (320, 180, 5.0, "2:1"))

    try:
        measure.main([str(video), "--out-dir", str(out), "--accept-detected"])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("non-square pixels must be rejected before measurement")

    assert "SAR 1:1" in capsys.readouterr().err
    assert not out.exists()
