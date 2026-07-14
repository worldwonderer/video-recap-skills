"""Plugin packaging guards (no-move variant): plugin.json has exactly the 4 Anthropic keys,
the 4 pure-tool stage skills are hidden (user-invocable: false) so video-recap is the router,
and marketplace.json is the single Claude-compatible marketplace catalog (also imported by
OpenClaw's `plugins install`); the plugin version stays single-sourced in plugin.json (bump it
during explicit release preparation so installed users receive each shipped version)."""
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]


def test_plugin_manifest_has_exactly_four_keys():
    manifest = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert set(manifest.keys()) == {"name", "version", "description", "author"}
    assert manifest["name"] == "video-recap-skills"
    assert re.fullmatch(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", manifest["version"])
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"## [{manifest['version']}]" in changelog


def test_marketplace_json_present_and_valid():
    mp = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert mp["name"] == "video-recap"
    assert mp["owner"]["name"]
    plugins = mp["plugins"]
    assert len(plugins) == 1
    entry = plugins[0]
    # the marketplace entry name must match the plugin manifest name
    assert entry["name"] == plugin["name"] == "video-recap-skills"
    assert entry["source"] == "./"
    # source "./" must point at the dir that actually holds the plugin manifest
    assert (ROOT / ".claude-plugin" / "plugin.json").exists()
    # version stays single-sourced in plugin.json; do not pin it in the marketplace entry
    assert "version" not in entry


def _frontmatter(skill_name):
    path = ROOT / "skills" / skill_name / "SKILL.md"
    text = path.read_text(encoding="utf-8")
    match = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|\Z)", text, re.DOTALL)
    assert match, f"{path} must start with YAML frontmatter"

    values = {}
    for line in match.group(1).splitlines():
        if not line or line[0].isspace():
            continue
        key, separator, raw_value = line.partition(":")
        assert separator, (path, line)
        value = raw_value.strip()
        if value in {"true", "false"}:
            values[key] = value == "true"
        else:
            values[key] = value.strip("\"'")
    return values


def test_every_discovered_skill_has_matching_frontmatter_identity():
    skill_dirs = sorted(
        path
        for path in (ROOT / "skills").iterdir()
        if path.is_dir() and (path / "SKILL.md").is_file()
    )
    assert skill_dirs
    for skill_dir in skill_dirs:
        frontmatter = _frontmatter(skill_dir.name)
        assert frontmatter.get("name") == skill_dir.name
        if "user-invocable" in frontmatter:
            assert isinstance(frontmatter["user-invocable"], bool)


def test_skill_frontmatter_exposes_only_the_router_and_writing_skill():
    expected_invocability = {
        "video-recap": True,
        "video-script": True,
        "video-understanding": False,
        "video-cut": False,
        "video-voiceover": False,
        "video-assemble": False,
    }

    for skill_name, expected in expected_invocability.items():
        frontmatter = _frontmatter(skill_name)
        assert frontmatter["name"] == skill_name
        assert frontmatter.get("user-invocable", True) is expected


def test_public_readmes_are_skill_first_and_document_verified_hosts():
    readmes = {
        "zh": (ROOT / "README.md").read_text(encoding="utf-8"),
        "en": (ROOT / "README.en.md").read_text(encoding="utf-8"),
    }

    for language, text in readmes.items():
        assert "codex plugin marketplace add worldwonderer/video-recap-skills" in text, language
        assert "codex plugin add video-recap-skills@video-recap" in text, language
        assert "https://opencode.ai/docs/skills/" in text, language
        assert "opencode debug skill" in text, language
        assert "python3 skills/video-recap/scripts/recap.py" not in text, language
        assert "python3 tools/measure_subtitle.py" not in text, language

    assert "用 /path/to/ep1.mp4 和 /path/to/ep2.mp4" in readmes["zh"]
    assert "Use /path/to/ep1.mp4 and /path/to/ep2.mp4" in readmes["en"]
