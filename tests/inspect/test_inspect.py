import importlib.util
import json
import sys
from pathlib import Path

# inspect.py shares its name with the stdlib `inspect` module, so it cannot be imported with a
# bare `import inspect` (that would resolve the stdlib). Load it by explicit file path under a
# private module name instead — this is the read-only advisory CLI under test.
_INSPECT_PATH = (
    Path(__file__).resolve().parents[2] / "skills" / "video-recap" / "scripts" / "inspect.py"
)
_spec = importlib.util.spec_from_file_location("recap_inspect", _INSPECT_PATH)
recap_inspect = importlib.util.module_from_spec(_spec)
sys.modules["recap_inspect"] = recap_inspect
_spec.loader.exec_module(recap_inspect)


# ---------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------
def test_state_full_mode(tmp_path):
    """A work_dir with only understanding + narration artifacts is full mode, and the next
    pause is None once narration.json exists."""
    (tmp_path / "scenes.json").write_text("[]", encoding="utf-8")
    (tmp_path / "narration.json").write_text("[]", encoding="utf-8")
    (tmp_path / "recap_run_manifest.json").write_text(json.dumps({
        "source_video": "/videos/movie.mp4",
        "source_video_fingerprint": "abc123",
        "settings": {"edit_mode": "full"},
    }), encoding="utf-8")

    state = recap_inspect.cmd_state(tmp_path, compact=True)
    assert state["mode"] == "full"
    assert state["source_video"]["path"] == "/videos/movie.mp4"
    assert state["source_video"]["fingerprint"] == "abc123"
    assert state["source_video"]["origin"] == "recap_run_manifest.json"
    assert state["next_pause"] is None  # narration.json present
    assert "scenes.json" in state["artifacts"]["understanding"]["present"]
    assert "narration.json" in state["artifacts"]["script"]["present"]
    assert state["stale_manifest_notes"] == []  # manifest present, full mode


def test_state_full_mode_next_pause_is_narration(tmp_path):
    """Full mode without narration.json waits on narration.json."""
    (tmp_path / "scenes.json").write_text("[]", encoding="utf-8")
    state = recap_inspect.cmd_state(tmp_path, compact=True)
    assert state["mode"] == "full"
    assert state["next_pause"]["artifact"] == "narration.json"


def test_state_cut_mode_pass1_waits_on_clip_plan(tmp_path):
    """Cut mode is detected from clip_plan_validated.json; before clip_plan.json the next pause
    is clip_plan.json (pass 1)."""
    (tmp_path / "clip_plan_validated.json").write_text(json.dumps({"clips": []}), encoding="utf-8")
    state = recap_inspect.cmd_state(tmp_path, compact=True)
    assert state["mode"] == "cut"
    assert state["next_pause"]["artifact"] == "clip_plan.json"


def test_state_cut_mode_pass2_waits_on_narration(tmp_path):
    """With clip_plan.json present but no narration.json, the next cut pause is narration.json."""
    (tmp_path / "clip_plan.json").write_text("[]", encoding="utf-8")
    (tmp_path / "clip_plan_validated.json").write_text(json.dumps({"clips": []}), encoding="utf-8")
    (tmp_path / "edited_source.mp4").write_bytes(b"\x00")
    state = recap_inspect.cmd_state(tmp_path, compact=True)
    assert state["mode"] == "cut"
    assert state["next_pause"]["artifact"] == "narration.json"


def test_state_source_from_assembly_manifest_when_no_run_manifest(tmp_path):
    """When recap_run_manifest.json is absent, the source falls back to assembly_manifest.json."""
    (tmp_path / "assembly_manifest.json").write_text(json.dumps({
        "input_video": "/videos/in.mp4",
        "source_video": "/videos/src.mp4",
        "source_video_fingerprint": "ff00",
    }), encoding="utf-8")
    state = recap_inspect.cmd_state(tmp_path, compact=True)
    assert state["source_video"]["path"] == "/videos/src.mp4"
    assert state["source_video"]["origin"] == "assembly_manifest.json"


def test_state_source_unknown_when_nothing_records_it(tmp_path):
    """No manifest anywhere → source reported unknown, no crash."""
    state = recap_inspect.cmd_state(tmp_path, compact=True)
    assert state["source_video"]["path"] is None
    assert state["source_video"]["origin"] == "unknown"


def test_state_lists_storyboards_when_present(tmp_path):
    """Storyboard paths (from the OTHER PR) are listed if their JSON sidecars exist, else omitted."""
    sb = tmp_path / "storyboard"
    sb.mkdir()
    (sb / "source_storyboard.json").write_text("{}", encoding="utf-8")
    state = recap_inspect.cmd_state(tmp_path, compact=True)
    assert "storyboard/source_storyboard.json" in state["storyboards"]
    assert "storyboard/edited_storyboard.json" not in state["storyboards"]


def test_state_forward_compat_prefers_manifest_file(tmp_path):
    """A future write-side manifest.json/task_state.json is surfaced as the state source."""
    (tmp_path / "manifest.json").write_text("{}", encoding="utf-8")
    state = recap_inspect.cmd_state(tmp_path, compact=True)
    assert "manifest.json" in state["forward_state_files"]


def test_state_missing_artifact_no_traceback(tmp_path):
    """An empty work_dir produces a clear human report with a stale-manifest warning, never a
    traceback. (Renders to markdown without raising.)"""
    state = recap_inspect.cmd_state(tmp_path, compact=True)
    md = recap_inspect._render_state_md(state, compact=True)
    assert "缺少 recap_run_manifest.json" in md
    assert "unknown" in md
    assert "模式" in md


def test_state_missing_work_dir_message(tmp_path):
    """A nonexistent work_dir returns a clear error, not a traceback."""
    missing = tmp_path / "does_not_exist"
    state = recap_inspect.cmd_state(missing, compact=True)
    assert "error" in state
    md = recap_inspect._render_state_md(state, compact=True)
    assert "不存在" in md


def test_state_cut_narration_without_phase_ledger_flagged_stale(tmp_path):
    """Cut mode with a narration.json but no recap_phase.json is flagged as a possible stale
    narration (the desync recap.py guards)."""
    (tmp_path / "clip_plan.json").write_text("[]", encoding="utf-8")
    (tmp_path / "clip_plan_validated.json").write_text(json.dumps({"clips": []}), encoding="utf-8")
    (tmp_path / "narration.json").write_text("[]", encoding="utf-8")
    (tmp_path / "recap_run_manifest.json").write_text(json.dumps(
        {"source_video": "/v.mp4", "source_video_fingerprint": "x"}), encoding="utf-8")
    state = recap_inspect.cmd_state(tmp_path, compact=True)
    notes = " ".join(state["stale_manifest_notes"])
    assert "recap_phase.json" in notes


# ---------------------------------------------------------------------------
# clip-map
# ---------------------------------------------------------------------------
# A known hand-authored validated plan. The forward affine map is
# output = clip.output_start + (src - clip.source_start), so:
#   clip 0: source 10-20  <->  output 0-10   (offset -10)
#   clip 1: source 50-56  <->  output 10-16  (offset -40)
_VALIDATED_PLAN = {
    "clips": [
        {"clip_id": 0, "source_start": 10.0, "source_end": 20.0,
         "output_start": 0.0, "output_end": 10.0, "duration": 10.0, "reason": "hook"},
        {"clip_id": 1, "source_start": 50.0, "source_end": 56.0,
         "output_start": 10.0, "output_end": 16.0, "duration": 6.0, "reason": "climax"},
    ],
    "total_duration": 16.0,
}


def _write_plan(tmp_path, plan=None):
    (tmp_path / "clip_plan_validated.json").write_text(
        json.dumps(plan if plan is not None else _VALIDATED_PLAN), encoding="utf-8")


def test_clip_map_absent_validated_plan_message(tmp_path):
    """No clip_plan_validated.json → a clear "not a cut run / not yet validated" message."""
    result = recap_inspect.cmd_clip_map(
        tmp_path, output_start=0.0, output_end=5.0,
        source_start=None, source_end=None, compact=True)
    assert "error" in result
    assert "clip_plan_validated.json" in result["error"]


def test_clip_map_output_to_source_exact_numbers(tmp_path):
    """A within-clip OUTPUT query maps to the exact SOURCE window (offset -10 for clip 0)."""
    _write_plan(tmp_path)
    result = recap_inspect.cmd_clip_map(
        tmp_path, output_start=2.0, output_end=7.0,
        source_start=None, source_end=None, compact=True)
    q = result["queries"][0]
    assert q["direction"] == "output→source"
    assert q["clips_touched"] == [0]
    assert q["cross_clip_boundary"] is False
    seg = q["segments"][0]
    assert seg["output"] == [2.0, 7.0]
    assert seg["source"] == [12.0, 17.0]  # 10 + (2-0)=12 ; 10 + (7-0)=17
    assert q["cut_out_source_gaps"] == []


def test_clip_map_source_to_output_exact_numbers(tmp_path):
    """A within-clip SOURCE query maps to the exact OUTPUT window (clip 1, offset -40)."""
    _write_plan(tmp_path)
    result = recap_inspect.cmd_clip_map(
        tmp_path, output_start=None, output_end=None,
        source_start=51.0, source_end=54.0, compact=True)
    q = result["queries"][0]
    assert q["direction"] == "source→output"
    assert q["clips_touched"] == [1]
    seg = q["segments"][0]
    assert seg["source"] == [51.0, 54.0]
    assert seg["output"] == [11.0, 14.0]  # 10 + (51-50)=11 ; 10 + (54-50)=14


def test_clip_map_point_query_inside_clip_resolves(tmp_path):
    """A zero-width query (start == end) is a POINT lookup: a point inside a clip resolves to
    that clip instead of silently falling through to 'not in any clip'."""
    _write_plan(tmp_path)
    result = recap_inspect.cmd_clip_map(
        tmp_path, output_start=5.0, output_end=5.0,
        source_start=None, source_end=None, compact=True)
    q = result["queries"][0]
    assert q["clips_touched"] == [0]
    seg = q["segments"][0]
    assert seg["output"] == [5.0, 5.0]
    assert seg["source"] == [15.0, 15.0]  # 10 + (5-0) = 15


def test_clip_map_output_query_straddling_two_clips_flagged(tmp_path):
    """An OUTPUT window crossing the clip0/clip1 join is flagged cross-clip and yields one
    mapped segment per clip with exact source numbers."""
    _write_plan(tmp_path)
    result = recap_inspect.cmd_clip_map(
        tmp_path, output_start=5.0, output_end=12.0,
        source_start=None, source_end=None, compact=True)
    q = result["queries"][0]
    assert q["cross_clip_boundary"] is True
    assert q["clips_touched"] == [0, 1]
    by_clip = {s["clip_id"]: s for s in q["segments"]}
    assert by_clip[0]["source"] == [15.0, 20.0]   # output 5-10 -> source 15-20
    assert by_clip[1]["source"] == [50.0, 52.0]   # output 10-12 -> source 50-52


def test_clip_map_source_range_outside_all_clips_flagged_cut_out(tmp_path):
    """A SOURCE window spanning footage between clips reports the cut-out gap (source range in
    no clip) and still maps the covered ends."""
    _write_plan(tmp_path)
    result = recap_inspect.cmd_clip_map(
        tmp_path, output_start=None, output_end=None,
        source_start=15.0, source_end=55.0, compact=True)
    q = result["queries"][0]
    assert q["cross_clip_boundary"] is True
    assert q["cut_out_source_gaps"] == [[20.0, 50.0]]  # footage cut away between the two clips


def test_clip_map_source_query_entirely_in_cut_out_gap(tmp_path):
    """A SOURCE window wholly inside cut-away footage maps to no clip and is fully a cut-out gap."""
    _write_plan(tmp_path)
    result = recap_inspect.cmd_clip_map(
        tmp_path, output_start=None, output_end=None,
        source_start=30.0, source_end=40.0, compact=True)
    q = result["queries"][0]
    assert q["segments"] == []
    assert q["clips_touched"] == []
    assert q["cut_out_source_gaps"] == [[30.0, 40.0]]


def test_clip_map_both_windows_in_one_call(tmp_path):
    """Querying both --output-* and --source-* returns two query blocks."""
    _write_plan(tmp_path)
    result = recap_inspect.cmd_clip_map(
        tmp_path, output_start=0.0, output_end=10.0,
        source_start=50.0, source_end=56.0, compact=True)
    dirs = {q["direction"] for q in result["queries"]}
    assert dirs == {"output→source", "source→output"}


def test_clip_map_no_window_specified_message(tmp_path):
    """Calling clip-map with no window bounds returns a clear usage message, not a crash."""
    _write_plan(tmp_path)
    result = recap_inspect.cmd_clip_map(
        tmp_path, output_start=None, output_end=None,
        source_start=None, source_end=None, compact=True)
    assert "error" in result


def test_clip_map_malformed_json_no_traceback(tmp_path):
    """A corrupt clip_plan_validated.json returns a clear message, never a traceback."""
    (tmp_path / "clip_plan_validated.json").write_text("{not json", encoding="utf-8")
    result = recap_inspect.cmd_clip_map(
        tmp_path, output_start=0.0, output_end=5.0,
        source_start=None, source_end=None, compact=True)
    assert "error" in result
    md = recap_inspect._render_clip_map_md(result, compact=True)
    assert "JSON" in md or "json" in md


def test_clip_map_bare_list_plan_with_derived_output(tmp_path):
    """A bare-list plan without output_start/end derives the output cursor the same way cut.py
    does (durations accumulate), matching assemble._output_clip_spans."""
    _write_plan(tmp_path, plan=[
        {"source_start": 10.0, "source_end": 20.0},  # output 0-10
        {"source_start": 50.0, "source_end": 56.0},  # output 10-16
    ])
    result = recap_inspect.cmd_clip_map(
        tmp_path, output_start=None, output_end=None,
        source_start=51.0, source_end=54.0, compact=True)
    seg = result["queries"][0]["segments"][0]
    assert seg["output"] == [11.0, 14.0]
