import ast
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
SKILLS_ROOT = ROOT / "skills"
SKILL_NAMES = tuple(
    path.name
    for path in sorted(SKILLS_ROOT.iterdir())
    if path.is_dir() and (path / "SKILL.md").is_file()
)
STAGE_SKILL_NAMES = tuple(name for name in SKILL_NAMES if name != "video-recap")


def _skill_path(skill_name: str) -> Path:
    return ROOT / "skills" / skill_name / "SKILL.md"


def _markdown_headings_outside_fences(text: str, prefix: str) -> list[str]:
    headings = []
    in_fence = False
    for line in text.splitlines():
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence and line.startswith(prefix):
            headings.append(line)
    return headings


def _markdown_section(text: str, heading: str) -> str:
    lines = text.splitlines()
    start = lines.index(heading) + 1
    end = next(
        (index for index in range(start, len(lines)) if lines[index].startswith("## ")),
        len(lines),
    )
    return "\n".join(lines[start:end])


def _json_fences(path: Path) -> list[dict | list]:
    text = path.read_text(encoding="utf-8")
    return [json.loads(raw) for raw in re.findall(r"```json\s*\n(.*?)\n```", text, re.DOTALL)]


def test_all_skill_headings_are_sequential_numbered_chinese():
    for skill_name in SKILL_NAMES:
        headings = _markdown_headings_outside_fences(
            _skill_path(skill_name).read_text(encoding="utf-8"),
            "## ",
        )

        assert headings, skill_name
        numbers = []
        for heading in headings:
            match = re.fullmatch(r"## (\d+)\. (.+)", heading)
            assert match, (skill_name, heading)
            numbers.append(int(match.group(1)))
            assert re.search(r"[\u3400-\u9fff]", match.group(2)), (skill_name, heading)
        assert numbers == list(range(1, len(numbers) + 1)), (skill_name, numbers)


def test_creative_roles_and_artifact_examples_form_a_structured_contract():
    recap_text = _skill_path("video-recap").read_text(encoding="utf-8")
    recap_roles = re.findall(
        r"(?m)^\d+\. \*\*([^*]+)\*\*[：:]",
        _markdown_section(recap_text, "## 2. 创作职责"),
    )
    assert recap_roles == ["导演判断", "故事编辑", "画面剪辑", "声音/旁白", "观众复核"]

    script_text = _skill_path("video-script").read_text(encoding="utf-8")
    script_roles = re.findall(
        r"(?m)^\d+\. ([^\n]+)$",
        _markdown_section(script_text, "## 1. 定位"),
    )
    assert script_roles == ["导演", "故事编辑", "画面剪辑师", "声音/旁白编辑", "第一次观看的观众"]

    for skill_name in ("video-recap", "video-script"):
        playbook = ROOT / "skills" / skill_name / "references" / "creative-editing-playbook.md"
        examples = _json_fences(playbook)
        story_plan = next(item for item in examples if isinstance(item, dict) and "director_intent" in item)
        av_board = next(item for item in examples if isinstance(item, dict) and "items" in item)

        assert story_plan["schema_version"] == 1
        assert len(story_plan["hypotheses"]) >= 2
        assert story_plan["chosen_hypothesis"] in {item["id"] for item in story_plan["hypotheses"]}
        assert {
            "viewer_promise",
            "pov",
            "dramatic_question",
            "emotional_start",
            "emotional_end",
            "ending_aftertaste",
            "withhold_reveal",
        } <= set(story_plan["director_intent"])
        assert {"beat_id", "function", "change", "must_keep_moment", "evidence"} <= set(story_plan["beats"][0])

        board_item = av_board["items"][0]
        assert av_board["schema_version"] == 1
        assert {"beat_id", "preferred_moment", "entry_reason", "exit_reason", "audio_owner", "narration_job"} <= set(board_item)
        assert set(board_item["audio_owner"].split("|")) == {
            "original_dialogue",
            "action_sound",
            "ambience",
            "music",
            "silence",
            "narration",
        }
        assert set(board_item["narration_job"].split("|")) == {
            "none",
            "context",
            "causal_link",
            "foreshadow",
            "interpretation",
            "transition",
        }


def test_research_guides_match_their_own_stage_timing():
    recap_guide = (SKILLS_ROOT / "video-recap" / "references" / "research-guide.md").read_text(encoding="utf-8")
    script_guide = (SKILLS_ROOT / "video-script" / "references" / "research-guide.md").read_text(encoding="utf-8")

    assert "开始视频理解**之前**" in recap_guide
    assert "继续视频理解" in recap_guide
    assert "继续写 `narration.json`" not in recap_guide
    assert "直接写解说词" not in recap_guide
    assert "不会自动重跑或改写已有 VLM / ASR 产物" in script_guide
    assert "`context-only`" in script_guide
    assert "开始视频理解**之前**" not in script_guide
    assert "回到当前创作阶段" in script_guide
    assert "`recap_story_plan.json`" in script_guide
    assert "`visual_audio_board.json`" in script_guide
    assert "直接写解说词" not in script_guide


def test_deslop_qc_schema_keeps_template_transitions_advisory():
    marker = "模板化“不是……而是……”转折"
    for schema_path in (
        SKILLS_ROOT / "video-recap" / "references" / "data-schema.md",
        SKILLS_ROOT / "video-understanding" / "references" / "data-schema.md",
    ):
        lines = schema_path.read_text(encoding="utf-8").splitlines()
        blocker_line = next(line for line in lines if line.startswith("- `blockers`："))
        advisory_line = next(line for line in lines if line.startswith("- `advisories`："))

        assert marker not in blocker_line, schema_path
        assert marker in advisory_line, schema_path


def test_skill_json_examples_are_parseable_and_cut_reason_keeps_editorial_fields():
    for skill_name in SKILL_NAMES:
        _json_fences(_skill_path(skill_name))

    cut_example = _json_fences(_skill_path("video-cut"))[0]
    reason_parts = [part.strip() for part in cut_example["reason"].split("|")]

    assert len(reason_parts) == 7
    assert reason_parts[0].startswith("b")
    assert "→" in reason_parts[2]
    assert reason_parts[3].startswith("POV=")
    assert reason_parts[-2].startswith("入点=")
    assert reason_parts[-1].startswith("出点=")

    def nested_items(value):
        if isinstance(value, dict):
            yield value
            for child in value.values():
                yield from nested_items(child)
        elif isinstance(value, list):
            for child in value:
                yield from nested_items(child)

    for schema_path in (
        SKILLS_ROOT / "video-recap" / "references" / "data-schema.md",
        SKILLS_ROOT / "video-understanding" / "references" / "data-schema.md",
    ):
        displayed_clips = [
            item
            for example in _json_fences(schema_path)
            for item in nested_items(example)
            if "reason" in item
            and (
                {"start", "end"} <= set(item)
                or {"source_start", "source_end"} <= set(item)
            )
        ]
        assert displayed_clips, schema_path
        for clip in displayed_clips:
            parts = [part.strip() for part in clip["reason"].split("|")]
            assert len(parts) == 7, (schema_path, clip)
            assert parts[0].startswith("b") and "→" in parts[2]
            assert parts[3].startswith("POV=")
            assert parts[-2].startswith("入点=") and parts[-1].startswith("出点=")


def test_markdown_references_are_local_and_resolve_inside_each_skill():
    for skill_name in SKILL_NAMES:
        skill_dir = ROOT / "skills" / skill_name
        for markdown_path in skill_dir.rglob("*.md"):
            text = markdown_path.read_text(encoding="utf-8")
            references = re.findall(r"`((?:\.\.?/|references/)[^`\n]+\.md)`", text)
            for reference in references:
                resolved = (markdown_path.parent / reference).resolve()
                try:
                    resolved.relative_to(skill_dir.resolve())
                except ValueError:
                    pytest.fail(f"{markdown_path}: reference escapes its skill: {reference}")
                assert resolved.is_file(), (markdown_path, reference)

            # A bare script name such as `review.py` still denotes an implementation
            # reference. It must resolve in this skill's own scripts directory rather
            # than silently relying on a sibling with a matching filename.
            for script_name in re.findall(r"`([A-Za-z0-9_.-]+\.py)`", text):
                assert (skill_dir / "scripts" / script_name).is_file(), (markdown_path, script_name)


def test_stage_sources_never_point_to_a_sibling_skill_path():
    paths = [
        markdown_path
        for skill_name in SKILL_NAMES
        for markdown_path in (ROOT / "skills" / skill_name).rglob("*.md")
    ]
    paths.extend(
        source_path
        for skill_name in STAGE_SKILL_NAMES
        for source_path in (ROOT / "skills" / skill_name / "scripts").glob("*.py")
    )

    for path in paths:
        current_name = path.relative_to(ROOT / "skills").parts[0]
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"(?:\.\./)+video-[a-z-]+/", text), path
        assert not re.search(r"\bskills/video-[a-z-]+/", text), path
        for target_name in re.findall(r"\b(video-[a-z-]+)/(?:references|scripts)/", text):
            assert target_name == current_name, (path, target_name)
        if path.suffix == ".md":
            for target_name in re.findall(r"`(video-[a-z-]+)/[^`]+\.(?:py|md)`", text):
                assert target_name == current_name, (path, target_name)


def test_stage_markdown_never_names_a_sibling_skill():
    """Stage instructions describe artifact contracts, never another skill implementation."""
    known_names = set(SKILL_NAMES)
    for skill_name in STAGE_SKILL_NAMES:
        skill_dir = SKILLS_ROOT / skill_name
        sibling_names = known_names - {skill_name}
        for markdown_path in skill_dir.rglob("*.md"):
            text = markdown_path.read_text(encoding="utf-8")
            mentioned = sorted(name for name in sibling_names if re.search(rf"\b{re.escape(name)}\b", text))
            assert not mentioned, (markdown_path, mentioned)


def test_all_literal_prompt_anchors_resolve_in_the_owning_skill():
    """Dynamically discover load_prompt("...") calls instead of pinning today's anchors."""
    for skill_name in SKILL_NAMES:
        skill_dir = SKILLS_ROOT / skill_name
        anchors = set()
        for source_path in (skill_dir / "scripts").glob("*.py"):
            tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not node.args:
                    continue
                function_name = node.func.id if isinstance(node.func, ast.Name) else None
                if function_name != "load_prompt":
                    continue
                first_arg = node.args[0]
                if isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str):
                    anchors.add(first_arg.value)

        if not anchors:
            continue
        prompt_path = skill_dir / "references" / "prompt-templates.md"
        assert prompt_path.is_file(), (skill_name, anchors)
        declared = set(re.findall(r"(?m)^### ([A-Za-z0-9_-]+)\s*$", prompt_path.read_text(encoding="utf-8")))
        assert anchors <= declared, (skill_name, sorted(anchors - declared))


@pytest.mark.parametrize("skill_name", SKILL_NAMES)
def test_each_skill_imports_all_python_modules_from_an_isolated_copy(skill_name, tmp_path):
    isolated_skill = tmp_path / skill_name
    shutil.copytree(ROOT / "skills" / skill_name, isolated_skill)
    scripts_dir = isolated_skill / "scripts"
    code = """
import importlib
import json
from pathlib import Path
import sys

scripts_dir = Path.cwd().resolve()
sys.path.insert(0, str(scripts_dir))
names = sorted(path.stem for path in scripts_dir.glob("*.py") if path.stem != "__init__")
for name in names:
    module = importlib.import_module(name)
    module_path = Path(module.__file__).resolve()
    assert scripts_dir in module_path.parents, (name, module_path, scripts_dir)
print(json.dumps(names))
"""

    result = subprocess.run(
        [sys.executable, "-I", "-c", code],
        cwd=scripts_dir,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == sorted(path.stem for path in scripts_dir.glob("*.py"))
