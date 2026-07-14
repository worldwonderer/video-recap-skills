import ast
import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _test_functions(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_")
    ]


def _body_fingerprint(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    body = node.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
        body = body[1:]
    return ast.dump(ast.Module(body=body, type_ignores=[]), include_attributes=False)


def test_test_functions_do_not_repeat_an_exact_body():
    seen = {}
    duplicates = []
    for path in sorted((ROOT / "tests").rglob("test_*.py")):
        for node in _test_functions(path):
            fingerprint = _body_fingerprint(node)
            location = f"{path.relative_to(ROOT)}::{node.name}"
            if fingerprint in seen:
                duplicates.append((seen[fingerprint], location))
            else:
                seen[fingerprint] = location

    assert not duplicates, f"Duplicate test bodies should be parametrized or have one canonical owner: {duplicates}"


def test_canonical_runner_includes_every_test_group():
    runner_path = ROOT / "scripts" / "test.py"
    spec = importlib.util.spec_from_file_location("canonical_test_runner", runner_path)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)

    discovered = {
        path.name
        for path in (ROOT / "tests").iterdir()
        if path.is_dir() and any(path.glob("test_*.py"))
    }

    assert len(runner.GROUPS) == len(set(runner.GROUPS))
    assert set(runner.GROUPS) == discovered
