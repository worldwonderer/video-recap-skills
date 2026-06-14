"""Anti-drift guard: video-understanding/brief.py and video-script/narration.py are
INTENTIONAL byte-identical copies (the no-shared-code constraint forbids a shared module,
so the narration logic is duplicated into both skills). Any edit to one must be applied to
the other in lockstep — a one-sided edit reds this test. Uses absolute paths + byte read
only (no imports), so it is immune to the per-skill sys.path isolation other tests rely on.
"""
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BRIEF = ROOT / "skills" / "video-understanding" / "scripts" / "brief.py"
NARRATION = ROOT / "skills" / "video-script" / "scripts" / "narration.py"


def _md5(path):
    return hashlib.md5(path.read_bytes()).hexdigest()


def test_brief_and_narration_are_byte_identical():
    assert BRIEF.exists() and NARRATION.exists()
    assert _md5(BRIEF) == _md5(NARRATION), (
        "brief.py and narration.py have drifted. They are intentional byte-identical "
        "copies; apply the SAME diff to BOTH in one commit (see the plan's lockstep rule)."
    )


def test_asr_span_tol_matches_across_files():
    """_ASR_SPAN_TOL is duplicated in consolidate.py and brief.py/narration.py (they cannot
    import each other across skills). Guard that the literal stays in sync."""
    import re
    def tol(rel):
        m = re.search(r"_ASR_SPAN_TOL\s*=\s*([0-9.]+)", (ROOT / rel).read_text(encoding="utf-8"))
        return m.group(1) if m else None
    vals = {
        tol("skills/video-understanding/scripts/consolidate.py"),
        tol("skills/video-understanding/scripts/brief.py"),
        tol("skills/video-script/scripts/narration.py"),
    }
    assert vals == {"0.05"}, f"_ASR_SPAN_TOL drifted across files: {vals}"
