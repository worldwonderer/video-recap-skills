import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
CLIENT_LIBS = [
    ("understanding", ROOT / "skills" / "video-understanding" / "scripts" / "lib.py"),
    ("voiceover", ROOT / "skills" / "video-voiceover" / "scripts" / "lib.py"),
    ("script", ROOT / "skills" / "video-script" / "scripts" / "lib.py"),
]


def _load_lib(name, path):
    spec = importlib.util.spec_from_file_location(f"mimo_client_safety_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(("name", "path"), CLIENT_LIBS)
def test_api_error_sanitizer_redacts_media_data_urls_and_keys(name, path):
    lib = _load_lib(name, path)
    test_key = "tp-" + ("testkey" * 6)
    raw = (
        "server echoed data:audio/wav;base64,QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo= "
        f"for key {test_key}"
    )

    safe = lib._sanitize_api_error(raw)

    assert "QUJDREV" not in safe
    assert test_key not in safe
    assert "<redacted-data-url>" in safe
    assert "<redacted-key>" in safe
