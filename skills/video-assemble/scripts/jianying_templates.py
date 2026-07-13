"""Load the pinned duo-video JianYing protocol templates."""

import copy
import json
from functools import lru_cache
from pathlib import Path


_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "references" / "jianying"
_TEMPLATE_FILES = {
    "project": "empty_jy_project_info.json",
    "video": "empty_jy_material_video.json",
    "audio": "empty_yj_material_audio.json",
    "text": "empty_yj_material_text.json",
    "text_style": "empty_jy_text_styles.json",
    "segment": "empty_jy_segment.json",
    "draft": "empty_jy_draft.json",
    "combination_segment": "empty_jy_combination_segment.json",
    "combination_video": "empty_jy_combination_video_material.json",
    "meta": "empty_draft_meta_info.json",
    "meta_material": "empty_jy_meta_material_value.json",
}


@lru_cache(maxsize=None)
def _read_template(name):
    try:
        filename = _TEMPLATE_FILES[name]
    except KeyError as exc:
        raise ValueError(f"unknown JianYing template: {name}") from exc
    with open(_TEMPLATE_DIR / filename, encoding="utf-8") as source:
        return json.load(source)


def template(name):
    """Return a mutable deep copy of a pinned protocol template."""
    return copy.deepcopy(_read_template(name))
