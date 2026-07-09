import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'skills' / 'video-script' / 'scripts'))
import json

# narration.py is the byte-identical lockstep copy of brief.py; this proves the SAME
# consolidation edit landed here and that the no-flag golden path is unchanged.
from narration import build_agent_brief, _chunk_asr_for_writing


def test_narration_brief_noop_without_consolidation(tmp_path):
    scenes = [{"scene_id": 0, "start": 0.0, "end": 6.0, "description": "门口对峙"}]
    asr = [{"start": 1.0, "end": 5.0, "text": "第一句对白。第二句反击。"}]
    silence = [{"start": 0.0, "end": 1.0, "duration": 1.0, "has_speech": False}]
    text = build_agent_brief(scenes, asr, silence, 6.0, tmp_path).read_text(encoding="utf-8")
    assert "Understanding index (from consolidate.py)" not in text
    written = json.loads((tmp_path / "asr_writing_chunks.json").read_text(encoding="utf-8"))
    assert written == _chunk_asr_for_writing(asr, scenes)


def test_narration_index_prompt_fingerprint_matches_canonical_index_prompt():
    """Narration twin of the understanding-side guard (PR #58 review). narration.py cannot
    import consolidate (cross-skill), so this pins the fingerprint to the canonical md5 of
    consolidate.INDEX_PROMPT. If you change INDEX_PROMPT, update the embedded copy in BOTH
    brief.py and narration.py and refresh this hash (the video-understanding guard computes
    the new value dynamically)."""
    from narration import _index_prompt_fingerprint
    assert _index_prompt_fingerprint() == "7fc5856658effc4175d188347d1bc66d"
