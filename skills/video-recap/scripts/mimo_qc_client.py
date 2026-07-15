"""Load this skill's local MiMo client exactly once under an unambiguous name."""

import importlib.util
from pathlib import Path

_LOCAL_LIB_PATH = Path(__file__).with_name("lib.py")
_LOCAL_LIB_SPEC = importlib.util.spec_from_file_location(
    "video_recap_mimo_qc_lib", _LOCAL_LIB_PATH
)
if (
    _LOCAL_LIB_SPEC is None or _LOCAL_LIB_SPEC.loader is None
):  # pragma: no cover - import invariant
    raise ImportError(f"cannot load local MiMo QC client: {_LOCAL_LIB_PATH}")
_LOCAL_LIB = importlib.util.module_from_spec(_LOCAL_LIB_SPEC)
_LOCAL_LIB_SPEC.loader.exec_module(_LOCAL_LIB)

DEFAULT_CONFIG = _LOCAL_LIB.CONFIG
mimo_qc_api_call = _LOCAL_LIB.mimo_qc_api_call
