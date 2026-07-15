import sys
from argparse import Namespace
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parents[2] / "skills" / "video-recap" / "scripts")
)
"""Orchestrator fail-fast: recap.py must reject a burn-on run on an ffmpeg without the
libass `subtitles` filter BEFORE any understand/VLM/ASR/TTS spend, and must surface the
advisory narration review to the user at delivery."""
import json  # noqa: E402

import pytest  # noqa: E402

import recap_runtime as recap  # noqa: E402
import recap_timeline


def _args(burn_subtitles=None):
    return Namespace(burn_subtitles=burn_subtitles)


def test_burn_intended_default_on(monkeypatch):
    monkeypatch.delenv("BURN_SUBTITLES", raising=False)
    assert recap._burn_subtitles_intended(_args()) is True


def test_burn_intended_cli_overrides_to_off():
    assert recap._burn_subtitles_intended(_args(burn_subtitles=False)) is False


def test_burn_intended_env_off(monkeypatch):
    monkeypatch.delenv("BURN_SUBTITLES", raising=False)
    monkeypatch.setenv("BURN_SUBTITLES", "0")
    assert recap._burn_subtitles_intended(_args()) is False


def test_burn_intended_cli_beats_env(monkeypatch):
    # explicit --burn-subtitles wins even when the env says off
    monkeypatch.setenv("BURN_SUBTITLES", "0")
    assert recap._burn_subtitles_intended(_args(burn_subtitles=True)) is True


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("on", True),
        ("yes", True),
        ("TRUE", True),
        ("1", True),
        (" 1 ", True),
        ("0", False),
        ("no", False),
        ("off", False),
        ("false", False),
        ("garbage", False),
    ],
)
def test_burn_intended_env_token_forms(monkeypatch, raw, expected):
    monkeypatch.setenv("BURN_SUBTITLES", raw)
    assert recap._burn_subtitles_intended(_args()) is expected


def test_preflight_raises_when_present_but_cannot_burn(monkeypatch):
    monkeypatch.delenv("BURN_SUBTITLES", raising=False)
    monkeypatch.setattr(recap, "_ffmpeg_present_but_cannot_burn", lambda: True)
    with pytest.raises(SystemExit, match="subtitles/libass"):
        recap._preflight_burn_subtitles(_args())


def test_preflight_ok_when_can_burn(monkeypatch):
    monkeypatch.delenv("BURN_SUBTITLES", raising=False)
    monkeypatch.setattr(recap, "_ffmpeg_present_but_cannot_burn", lambda: False)
    recap._preflight_burn_subtitles(_args())  # must not raise


def test_preflight_does_not_probe_when_burn_off(monkeypatch):
    def _boom():
        raise AssertionError("must not probe ffmpeg when burn is off")

    monkeypatch.setattr(recap, "_ffmpeg_present_but_cannot_burn", _boom)
    recap._preflight_burn_subtitles(_args(burn_subtitles=False))  # must not raise


def test_cannot_burn_false_when_ffmpeg_absent(monkeypatch):
    # ffmpeg absent entirely → this guard stays out of it (fails later / reported by doctor)
    import shutil

    monkeypatch.setattr(shutil, "which", lambda _n: None)
    assert recap._ffmpeg_present_but_cannot_burn() is False


def test_cannot_burn_true_when_present_without_filter(monkeypatch):
    import shutil

    monkeypatch.setattr(shutil, "which", lambda _n: "/usr/bin/ffmpeg")
    monkeypatch.setattr(recap, "ffmpeg_has_subtitles_filter", lambda: False)
    assert recap._ffmpeg_present_but_cannot_burn() is True


def test_cannot_burn_false_when_present_with_filter(monkeypatch):
    import shutil

    monkeypatch.setattr(shutil, "which", lambda _n: "/usr/bin/ffmpeg")
    monkeypatch.setattr(recap, "ffmpeg_has_subtitles_filter", lambda: True)
    assert recap._ffmpeg_present_but_cannot_burn() is False


def test_review_pointer_prints_verdict(tmp_path, capsys):
    (tmp_path / "narration_review.md").write_text("# review", encoding="utf-8")
    (tmp_path / "narration_review.json").write_text(
        json.dumps(
            {
                "verdict": "REVISE",
                "findings": [{"severity": "error"}, {"severity": "warning"}],
            }
        ),
        encoding="utf-8",
    )
    recap_timeline._print_narration_review_pointer(tmp_path)
    out = capsys.readouterr().out
    assert "REVISE" in out
    assert "narration_review.md" in out
    assert "error 1" in out


def test_review_pointer_silent_when_absent(tmp_path, capsys):
    recap_timeline._print_narration_review_pointer(tmp_path)
    assert capsys.readouterr().out == ""


def test_review_pointer_md_present_json_absent(tmp_path, capsys):
    # md exists but no json → fall through to the plain-path branch, never crash
    (tmp_path / "narration_review.md").write_text("# review", encoding="utf-8")
    recap_timeline._print_narration_review_pointer(tmp_path)
    out = capsys.readouterr().out
    assert "narration_review.md" in out


def test_review_pointer_json_malformed(tmp_path, capsys):
    (tmp_path / "narration_review.md").write_text("# review", encoding="utf-8")
    (tmp_path / "narration_review.json").write_text("{ not json", encoding="utf-8")
    recap_timeline._print_narration_review_pointer(tmp_path)
    out = capsys.readouterr().out
    assert "narration_review.md" in out  # degrades to the plain pointer, no traceback
