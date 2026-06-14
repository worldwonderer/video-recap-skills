"""Root pytest guard for this self-contained skill bundle.

Each skill intentionally has its own top-level modules (notably `lib.py`). The
canonical test command runs one isolated pytest process per skill group, so a
plain root-level `pytest` run would otherwise collect incompatible groups in one
interpreter and report misleading import collisions.
"""

from pathlib import Path

import pytest

_CANONICAL_MESSAGE = (
    "This repository uses isolated per-skill pytest processes. "
    "Run `python3 scripts/test.py` for the full suite, or target one group such "
    "as `python3 -m pytest tests/assemble -q`."
)


def _is_help_or_version(args):
    return any(arg in {"--help", "-h", "--version", "-V"} for arg in args)


def _test_group_for_path(root, path):
    try:
        rel = path.relative_to(root / "tests")
    except ValueError:
        return None
    if not rel.parts:
        return ""
    return rel.parts[0]


def pytest_cmdline_main(config):
    if _is_help_or_version(config.invocation_params.args):
        return None
    args = list(getattr(config.option, "file_or_dir", None) or [])
    if not args:
        raise pytest.UsageError(_CANONICAL_MESSAGE)
    root = Path(config.rootpath).resolve()
    groups = set()
    for raw in args:
        path = (root / raw).resolve() if not Path(raw).is_absolute() else Path(raw).resolve()
        if path == root or path == root / "tests":
            raise pytest.UsageError(_CANONICAL_MESSAGE)
        group = _test_group_for_path(root, path)
        if group:
            groups.add(group)
    if len(groups) > 1:
        raise pytest.UsageError(_CANONICAL_MESSAGE)
    return None
