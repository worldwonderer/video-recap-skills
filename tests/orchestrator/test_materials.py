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
    (work / "asr_clean.json").write_text(json.dumps({"segments": [{"text": "clean"}]}), encoding="utf-8")
    (work / "understanding_index.json").write_text(json.dumps({"summary": "英雄入场", "tags": ["hero"]}), encoding="utf-8")
    (work / "audio.wav").write_bytes(b"raw audio should not copy")
    (work / "secret.json").write_text("tp-secret", encoding="utf-8")

    meta = materials.save_material(lib, work, tmp_path / "ep1.mp4", "f" * 64, "settings", source_id="src_ffffffffffff")

    mdir = lib / "materials" / meta["material_id"]
    assert (mdir / "material.json").exists()
    assert (mdir / "material.md").exists()
    assert (mdir / "artifacts" / "scenes.json").exists()
    assert (mdir / "artifacts" / "asr_clean.json").exists()
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
    (work / "asr_clean.json").write_text(json.dumps({"segments": [{"text": "hello。"}]}), encoding="utf-8")
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
    assert (dest / "asr_clean.json").exists()


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


def test_allowed_artifacts_redact_secret_values_but_keep_legitimate_words(tmp_path):
    """Redaction must remove credential VALUES while leaving ordinary analysis words
    (secret/token/api_key as plain text, benign field names) intact — the library is a
    faithful, reusable copy of the analysis, not a word-censored one."""
    lib = tmp_path / "library"
    work = tmp_path / "work"
    work.mkdir()
    (work / "understanding_index.json").write_text(
        json.dumps({
            "summary": "主角发现了一个秘密 secret，一枚 token 在黑市流通",   # legit words -> must survive
            "api_key": "tp-abcdef12345678",                                  # credential key -> value dropped
            "token_economy": "影片解释 token 的发行机制",                     # benign name containing 'token' -> kept
            "notes": "调试时漏了 key: sk-ABCDEFGHIJKLMNOP1234 和 tp-zzzzzzzz9999",  # value shapes -> redacted
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    (work / "agent_narration_brief.md").write_text(
        "MIMO_API_KEY=tp-another-secret-value\n剧情梗概：一个关于 secret 和 token 的故事", encoding="utf-8")
    meta = materials.save_material(lib, work, tmp_path / "ep.mp4", "d" * 64, "settings")

    persisted = "\n".join(p.read_text(encoding="utf-8") for p in (lib / "materials").rglob("*") if p.is_file())
    # secret VALUES are gone
    for secret in ("tp-abcdef12345678", "sk-ABCDEFGHIJKLMNOP1234", "tp-zzzzzzzz9999", "tp-another-secret-value"):
        assert secret not in persisted, secret
    # legitimate words and benign field names PRESERVED (the over-redaction fix)
    assert "主角发现了一个秘密" in persisted
    assert "secret" in persisted and "token" in persisted
    assert "token_economy" in persisted

    idx = json.loads((lib / "materials" / meta["material_id"] / "artifacts" / "understanding_index.json")
                     .read_text(encoding="utf-8"))
    assert idx["api_key"] == "[redacted]"                 # value dropped, key name kept, not coalesced
    assert "secret" in idx["summary"] and "token" in idx["summary"]
    assert idx["token_economy"] == "影片解释 token 的发行机制"

    dest = tmp_path / "dest"
    materials.restore_material(lib, dest, source_fingerprint="d" * 64, settings_fp="settings",
                               material_id=meta["material_id"])
    restored = (dest / "understanding_index.json").read_text(encoding="utf-8")
    assert "tp-abcdef12345678" not in restored
    assert "secret" in restored and "token" in restored


def test_redact_json_keeps_distinct_secret_named_keys_without_coalescing(tmp_path):
    """A dict with multiple credential-named keys must keep every key (each value dropped),
    never collapse them into one 'redacted_key', and must not touch benign look-alike names."""
    out = materials._redact_json({
        "api_key": "tp-realvalue123456",
        "access_token": "sk-ABCDEFGHIJKLMNOP1234",
        "tokenized_scenes": ["镜头1", "镜头2"],   # benign name with 'token' substring -> untouched
        "secrets_revealed": "结局揭晓的秘密",       # benign name with 'secret' substring -> untouched
        "title": "ok",
    })
    assert out["api_key"] == "[redacted]"
    assert out["access_token"] == "[redacted]"
    assert "redacted_key" not in out                 # no coalescing
    assert out["tokenized_scenes"] == ["镜头1", "镜头2"]
    assert out["secrets_revealed"] == "结局揭晓的秘密"
    assert out["title"] == "ok"


def test_redact_text_leaves_plain_words_but_strips_token_shapes():
    assert materials._redact_text("the secret garden hides a golden token") == \
        "the secret garden hides a golden token"
    assert "tp-" not in materials._redact_text("key is tp-abcdef12345678 ok")
    assert "ghp_" not in materials._redact_text("token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345")


def test_restore_overwrite_false_does_not_prune_then_lose_staged_file(tmp_path):
    """FF-B: prune_stale_allowed + overwrite=False must NOT prune a staged file and then skip
    restoring it. The staged file is preserved; only true (non-staged) stale orphans are pruned."""
    lib = tmp_path / "library"
    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "scenes.json").write_text(json.dumps([{"start": 0, "end": 1}]), encoding="utf-8")
    meta = materials.save_material(lib, seed, tmp_path / "ep.mp4", "g" * 64, "settings")

    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "scenes.json").write_text(json.dumps([{"keep": "existing"}]), encoding="utf-8")  # staged name, present
    (dest / "vlm_analysis.json").write_text(json.dumps({"stale": 1}), encoding="utf-8")        # non-staged orphan

    res = materials.restore_material(lib, dest, source_fingerprint="g" * 64, settings_fp="settings",
                                     material_id=meta["material_id"], overwrite=False)

    assert (dest / "scenes.json").exists(), "staged file must survive (not pruned-then-skipped)"
    assert json.loads((dest / "scenes.json").read_text(encoding="utf-8")) == [{"keep": "existing"}]
    assert not (dest / "vlm_analysis.json").exists(), "true stale orphan is pruned"
    assert "vlm_analysis.json" in res["pruned_artifacts"]
    assert "scenes.json" not in res["pruned_artifacts"]


def test_save_material_reconciles_orphan_artifacts_on_resave(tmp_path):
    """FF-C: re-saving with fewer artifacts removes the orphan from artifacts/ so the on-disk
    set matches material.json (no stale blob lingering for greps)."""
    lib = tmp_path / "library"
    work = tmp_path / "work"
    work.mkdir()
    (work / "scenes.json").write_text("[]", encoding="utf-8")
    (work / "asr_result.json").write_text(json.dumps([{"text": "hi"}]), encoding="utf-8")
    meta = materials.save_material(lib, work, tmp_path / "ep.mp4", "h" * 64, "s")
    adir = lib / "materials" / meta["material_id"] / "artifacts"
    assert (adir / "asr_result.json").exists()

    (work / "asr_result.json").unlink()  # a smaller / partial re-analysis
    meta2 = materials.save_material(lib, work, tmp_path / "ep.mp4", "h" * 64, "s")

    assert not (adir / "asr_result.json").exists(), "orphan artifact removed on re-save"
    assert (adir / "scenes.json").exists()
    assert {a["name"] for a in meta2["artifacts"]} == {"scenes.json"}
