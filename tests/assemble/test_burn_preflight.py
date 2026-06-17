import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'skills' / 'video-assemble' / 'scripts'))
"""Fail-fast preflight: when subtitle burn-in is on but ffmpeg lacks the libass
`subtitles` filter, assemble must raise BEFORE the render — not die at the final -vf."""
import shutil  # noqa: E402
import subprocess  # noqa: E402

import pytest  # noqa: E402

import assemble  # noqa: E402


def test_preflight_raises_when_present_but_filter_missing(monkeypatch):
    monkeypatch.setitem(assemble.CONFIG, "burn_subtitles", True)
    monkeypatch.setattr(shutil, "which", lambda _n: "/usr/bin/ffmpeg")
    monkeypatch.setattr(assemble, "_ffmpeg_filters", lambda: {"scale", "atempo"})
    with pytest.raises(SystemExit, match="subtitles/libass"):
        assemble._preflight_burn_subtitles()


def test_preflight_ok_when_filter_present(monkeypatch):
    monkeypatch.setitem(assemble.CONFIG, "burn_subtitles", True)
    monkeypatch.setattr(shutil, "which", lambda _n: "/usr/bin/ffmpeg")
    monkeypatch.setattr(assemble, "_ffmpeg_filters", lambda: {"subtitles", "scale"})
    assemble._preflight_burn_subtitles()  # must not raise


def test_preflight_noop_when_ffmpeg_absent(monkeypatch):
    # ffmpeg absent entirely → guard stays out of it (render fails later regardless), so the
    # mocked, ffmpeg-less test/CI environment is never blocked.
    monkeypatch.setitem(assemble.CONFIG, "burn_subtitles", True)
    monkeypatch.setattr(shutil, "which", lambda _n: None)

    def _boom():
        raise AssertionError("must not probe filters when ffmpeg is absent")

    monkeypatch.setattr(assemble, "_ffmpeg_filters", _boom)
    assemble._preflight_burn_subtitles()  # must not raise, must not probe


def test_preflight_skipped_when_burn_off(monkeypatch):
    monkeypatch.setitem(assemble.CONFIG, "burn_subtitles", False)

    def _boom():
        raise AssertionError("must not probe ffmpeg when burn is off")

    monkeypatch.setattr(assemble, "_ffmpeg_filters", _boom)
    assemble._preflight_burn_subtitles()  # must not raise, must not probe


def test_ffmpeg_filters_parse_matches_doctor(monkeypatch):
    """The parser must mirror doctor.py: take column-2 names from filter rows whose first
    token starts with a flag char. A mis-parse returning an empty set would block every
    capable ffmpeg, so this pins the parse contract."""
    sample = (
        "Filters:\n"
        "  T.. atempo            A->A       Adjust audio tempo.\n"
        " ..C subtitles          V->V       Render text subtitles.\n"
        "garbage line with no flag token\n"
    )

    class _Result:
        returncode = 0
        stdout = sample

    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Result())
    filters = assemble._ffmpeg_filters()
    assert "subtitles" in filters and "atempo" in filters


def test_ffmpeg_filters_empty_when_ffmpeg_absent(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    assert assemble._ffmpeg_filters() == set()


def test_ffmpeg_filters_empty_on_nonzero_returncode(monkeypatch):
    class _Result:
        returncode = 1
        stdout = "should be ignored"

    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Result())
    assert assemble._ffmpeg_filters() == set()
