"""Plugin packaging guards (no-move variant): plugin.json has exactly the 4 Anthropic keys,
the 4 pure-tool stage skills are hidden (user-invocable: false) so video-recap is the router,
and marketplace.json stays deferred until product confirmation."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_plugin_manifest_has_exactly_four_keys():
    manifest = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
    assert set(manifest.keys()) == {"name", "version", "description", "author"}
    assert manifest["name"] == "video-recap-skills"


def test_marketplace_json_is_deferred():
    assert not (ROOT / ".claude-plugin" / "marketplace.json").exists()


def test_stage_skills_hidden_router_visible():
    def fm(skill):
        return (ROOT / "skills" / skill / "SKILL.md").read_text()
    for hidden in ("video-understanding", "video-cut", "video-voiceover", "video-assemble"):
        assert "user-invocable: false" in fm(hidden), f"{hidden} should be hidden"
    for visible in ("video-script", "video-recap"):
        assert "user-invocable: false" not in fm(visible), f"{visible} should stay invocable"


def test_config_playbook_exists():
    assert (ROOT / "skills" / "video-recap" / "references" / "config-playbook.md").exists()
