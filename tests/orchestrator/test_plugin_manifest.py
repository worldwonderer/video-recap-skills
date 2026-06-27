"""Plugin packaging guards (no-move variant): plugin.json has exactly the 4 Anthropic keys,
the 4 pure-tool stage skills are hidden (user-invocable: false) so video-recap is the router,
and marketplace.json is the single Claude-compatible marketplace catalog (also imported by
OpenClaw's `plugins install`); the plugin version stays single-sourced in plugin.json (bump it
on each shipped change so installed users receive the update)."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_plugin_manifest_has_exactly_four_keys():
    manifest = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert set(manifest.keys()) == {"name", "version", "description", "author"}
    assert manifest["name"] == "video-recap-skills"


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


def test_stage_skills_hidden_router_visible():
    def fm(skill):
        return (ROOT / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")
    for hidden in ("video-understanding", "video-cut", "video-voiceover", "video-assemble"):
        assert "user-invocable: false" in fm(hidden), f"{hidden} should be hidden"
    for visible in ("video-script", "video-recap"):
        assert "user-invocable: false" not in fm(visible), f"{visible} should stay invocable"


def test_config_playbook_exists():
    assert (ROOT / "skills" / "video-recap" / "references" / "config-playbook.md").exists()
