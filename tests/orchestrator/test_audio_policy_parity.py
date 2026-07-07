import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
LIBS = {
    "assemble": ROOT / "skills/video-assemble/scripts/lib.py",
    "voiceover": ROOT / "skills/video-voiceover/scripts/lib.py",
    "script": ROOT / "skills/video-script/scripts/lib.py",
    "recap": ROOT / "skills/video-recap/scripts/lib.py",
    "understanding": ROOT / "skills/video-understanding/scripts/lib.py",
}


def _load_lib(name, path):
    spec = importlib.util.spec_from_file_location(f"audio_policy_{name}_lib", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_audio_tempo_policy_config_and_helper_stay_in_sync():
    libs = {name: _load_lib(name, path) for name, path in LIBS.items()}
    keys = [
        "narration_speed",
        "narration_cumulative_tempo_max",
        "narration_cumulative_tempo_hard_max",
        "tts_segment_tempo_max",
        "fade_ms",
    ]
    baseline = {key: libs["assemble"].CONFIG[key] for key in keys}

    for name, lib in libs.items():
        assert {key: lib.CONFIG[key] for key in keys} == baseline, name
        assert hasattr(lib, "narration_tempo_budget"), name

    for offset in (-0.05, 0.0, 0.05, 0.08):
        expected = libs["assemble"].narration_tempo_budget(offset)
        for name, lib in libs.items():
            assert lib.narration_tempo_budget(offset) == expected, name


def test_audio_mix_and_normalization_policy_stay_in_sync():
    libs = {name: _load_lib(name, path) for name, path in LIBS.items()}
    keys = [
        "ducking_mode",
        "ducking_threshold",
        "ducking_ratio",
        "ducking_attack",
        "ducking_release",
        "ducking_level_sc",
        "ducking_makeup",
        "ducking_narr_weight",
        "ducking_orig_volume",
        "zone_ducking_volume",
        "zone_fade_seconds",
        "idle_orig_volume",
        "duck_fade_seconds",
        "bgm_volume",
        "bgm_ducking_volume",
        "speech_ducking_volume",
        "final_loudnorm",
        "target_lufs",
        "target_true_peak",
        "target_lra",
        "final_limiter_peak",
        "tts_segment_normalize",
        "tts_segment_target_rms_dbfs",
        "tts_segment_peak_limit",
    ]
    baseline = {key: libs["assemble"].CONFIG[key] for key in keys}

    for name, lib in libs.items():
        assert {key: lib.CONFIG[key] for key in keys} == baseline, name


def test_visual_qc_delivery_boundary_fields_are_not_audio_policy_keys():
    """Delivery transparency fields are rollup/QC facts, not shared audio policy knobs;
    this guards against leaking visual-delivery contract fields into CONFIG parity."""
    libs = {name: _load_lib(name, path) for name, path in LIBS.items()}
    delivery_fact_keys = {
        "video_encode_passes",
        "reencode_reason",
        "audio_sample_rate",
        "final_compat_notes",
        "double_encode",
    }
    for name, lib in libs.items():
        assert not (delivery_fact_keys & set(lib.CONFIG)), name
