import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "video-recap" / "scripts"))

import materials  # noqa: E402


def test_source_id_stable_and_duplicate_paths_get_suffix(tmp_path):
    fp1 = "a" * 64
    fp2 = "b" * 64
    a = tmp_path / "a.mp4"
    b = tmp_path / "b.mp4"
    c = tmp_path / "copy.mp4"
    for p in (a, b, c):
        p.write_bytes(b"x")

    first = materials.assign_source_ids([
        {"source_path": b, "source_video_fingerprint": fp2},
        {"source_path": a, "source_video_fingerprint": fp1},
    ])
    second = materials.assign_source_ids([
        {"source_path": a, "source_video_fingerprint": fp1},
        {"source_path": b, "source_video_fingerprint": fp2},
    ])

    assert {r["source_video_fingerprint"]: r["source_id"] for r in first} == {
        r["source_video_fingerprint"]: r["source_id"] for r in second
    }
    dup = materials.assign_source_ids([
        {"source_path": a, "source_video_fingerprint": fp1},
        {"source_path": c, "source_video_fingerprint": fp1},
    ])
    assert dup[0]["source_id"] == "src_aaaaaaaaaaaa"
    assert dup[1]["source_id"].startswith("src_aaaaaaaaaaaa_")
    assert len(dup[1]["source_id"].split("_")[-1]) == 6


def test_material_id_stable_for_same_basename_and_fingerprint(tmp_path):
    fp = "1234567890abcdef" * 4
    assert materials.material_id_for(tmp_path / "Episode 1.mp4", fp) == materials.material_id_for(tmp_path / "Episode 1.mp4", fp)
    assert materials.material_id_for(tmp_path / "Episode 1.mp4", fp).endswith("-1234567890ab")


def test_save_material_copies_allowed_files_writes_md_and_append_index(tmp_path):
    lib = tmp_path / "library"
    work = tmp_path / "work"
    work.mkdir()
    (work / "scenes.json").write_text(json.dumps([{"start": 0, "end": 1}]), encoding="utf-8")
    (work / "understanding_index.json").write_text(json.dumps({"summary": "英雄入场", "tags": ["hero"]}), encoding="utf-8")
    (work / "audio.wav").write_bytes(b"raw audio should not copy")
    (work / "secret.json").write_text("tp-secret", encoding="utf-8")

    meta = materials.save_material(lib, work, tmp_path / "ep1.mp4", "f" * 64, "settings", source_id="src_ffffffffffff")

    mdir = lib / "materials" / meta["material_id"]
    assert (mdir / "material.json").exists()
    assert (mdir / "material.md").exists()
    assert (mdir / "artifacts" / "scenes.json").exists()
    assert not (mdir / "artifacts" / "audio.wav").exists()
    assert not (mdir / "artifacts" / "secret.json").exists()
    lines = (lib / "materials_index.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["event"] == "saved"
    assert rec["summary"] == "英雄入场"
    assert "hero" in rec["tags"]

    materials.save_material(lib, work, tmp_path / "ep1.mp4", "f" * 64, "settings", source_id="src_ffffffffffff")
    assert len((lib / "materials_index.jsonl").read_text(encoding="utf-8").splitlines()) == 2


def test_restore_material_requires_matching_fingerprint_and_settings(tmp_path):
    lib = tmp_path / "library"
    work = tmp_path / "work"
    work.mkdir()
    (work / "asr_result.json").write_text(json.dumps([{"text": "hello"}]), encoding="utf-8")
    meta = materials.save_material(lib, work, tmp_path / "ep.mp4", "a" * 64, "s1")

    dest = tmp_path / "dest"
    mismatch = materials.restore_material(lib, dest, source_fingerprint="b" * 64, settings_fp="s1", material_id=meta["material_id"])
    assert mismatch["restored"] is False
    assert not dest.exists()

    mismatch = materials.restore_material(lib, dest, source_fingerprint="a" * 64, settings_fp="s2", material_id=meta["material_id"])
    assert mismatch["restored"] is False
    assert not dest.exists()

    ok = materials.restore_material(lib, dest, source_fingerprint="a" * 64, settings_fp="s1", material_id=meta["material_id"])
    assert ok["restored"] is True
    assert (dest / "asr_result.json").exists()


def test_restore_material_prunes_stale_allowed_artifacts_before_copy(tmp_path):
    lib = tmp_path / "library"
    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "scenes.json").write_text(json.dumps([{"start": 0, "end": 1}]), encoding="utf-8")
    meta = materials.save_material(lib, seed, tmp_path / "ep.mp4", "e" * 64, "settings")

    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "vlm_analysis.json").write_text(json.dumps({"summary": "stale"}), encoding="utf-8")
    (dest / "narration.json").write_text("[]", encoding="utf-8")

    restored = materials.restore_material(
        lib,
        dest,
        source_fingerprint="e" * 64,
        settings_fp="settings",
        material_id=meta["material_id"],
    )

    assert restored["restored"] is True
    assert (dest / "scenes.json").exists()
    assert not (dest / "vlm_analysis.json").exists()
    assert (dest / "narration.json").exists(), "non-material files are not pruned"
    assert "vlm_analysis.json" in restored["pruned_artifacts"]


def test_material_files_do_not_include_api_key_string(tmp_path):
    lib = tmp_path / "library"
    work = tmp_path / "work"
    work.mkdir()
    (work / "understanding_index.json").write_text(json.dumps({"summary": "safe"}), encoding="utf-8")
    materials.save_material(lib, work, tmp_path / "ep.mp4", "c" * 64, "settings")
    all_text = "\n".join(p.read_text(encoding="utf-8") for p in lib.rglob("*.json"))
    all_text += "\n" + "\n".join(p.read_text(encoding="utf-8") for p in lib.rglob("*.md"))
    all_text += "\n" + (lib / "materials_index.jsonl").read_text(encoding="utf-8")
    assert "tp-secret" not in all_text
    assert "MIMO_API_KEY" not in all_text


def test_allowed_artifacts_are_redacted_before_copy_and_restore(tmp_path):
    lib = tmp_path / "library"
    work = tmp_path / "work"
    work.mkdir()
    (work / "understanding_index.json").write_text(
        json.dumps({"summary": "token tp-secret-value", "api_key": "tp-real-looking"}),
        encoding="utf-8",
    )
    (work / "agent_narration_brief.md").write_text("MIMO_API_KEY=tp-another-secret", encoding="utf-8")
    meta = materials.save_material(lib, work, tmp_path / "ep.mp4", "d" * 64, "settings")

    persisted = "\n".join(p.read_text(encoding="utf-8") for p in (lib / "materials").rglob("*") if p.is_file())
    assert "tp-secret-value" not in persisted
    assert "tp-real-looking" not in persisted
    assert "MIMO_API_KEY" not in persisted
    assert "api_key" not in persisted.lower()

    dest = tmp_path / "dest"
    materials.restore_material(lib, dest, source_fingerprint="d" * 64, settings_fp="settings",
                               material_id=meta["material_id"])
    restored = (dest / "understanding_index.json").read_text(encoding="utf-8")
    assert "tp-secret-value" not in restored
    assert "redacted" in restored
