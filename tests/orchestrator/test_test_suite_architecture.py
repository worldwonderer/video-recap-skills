import ast
import importlib.util
from pathlib import Path
from collections import defaultdict


ROOT = Path(__file__).resolve().parents[2]
MAX_SCRIPT_MODULE_LINES = 800

PUBLIC_ENTRYPOINTS = (
    "skills/video-assemble/scripts/assemble.py",
    "skills/video-cut/scripts/cut.py",
    "skills/video-recap/scripts/mimo_qc.py",
    "skills/video-recap/scripts/recap.py",
    "skills/video-script/scripts/narration.py",
    "skills/video-script/scripts/review.py",
    "skills/video-understanding/scripts/brief.py",
    "skills/video-understanding/scripts/understand.py",
)

REQUIRED_PUBLIC_EXPORTS = {
    "skills/video-assemble/scripts/assemble.py": {
        "assemble_video",
        "assembly_settings_fingerprint",
        "final_loudnorm_filter",
        "main",
    },
    "skills/video-recap/scripts/mimo_qc.py": {
        "build_report",
        "mimo_qc_api_call",
        "sample_video_frames",
        "write_report",
    },
}


def _test_functions(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    ]


def _body_fingerprint(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    body = node.body
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
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

    assert not duplicates, (
        f"Duplicate test bodies should be parametrized or have one canonical owner: {duplicates}"
    )


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


def _script_modules():
    return sorted((ROOT / "skills").glob("*/scripts/*.py"))


def _line_count(path: Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines())


def test_all_skill_script_modules_stay_within_the_strict_line_budget():
    oversized = {
        str(path.relative_to(ROOT)): _line_count(path)
        for path in _script_modules()
        if _line_count(path) > MAX_SCRIPT_MODULE_LINES
    }

    assert not oversized, (
        f"Every skill Python module must stay at or below {MAX_SCRIPT_MODULE_LINES} lines; "
        f"split responsibilities instead of extending an oversized file: {oversized}"
    )


def test_skill_scripts_do_not_import_other_skill_scripts():
    module_owners = defaultdict(set)
    skill_script_dirs = {}
    for skill_dir in sorted((ROOT / "skills").iterdir()):
        scripts_dir = skill_dir / "scripts"
        if not scripts_dir.is_dir():
            continue
        local_modules = {path.stem for path in scripts_dir.glob("*.py")}
        skill_script_dirs[skill_dir.name] = (scripts_dir, local_modules)
        for module in local_modules:
            module_owners[module].add(skill_dir.name)

    violations = []
    for skill_name, (scripts_dir, local_modules) in skill_script_dirs.items():
        other_script_paths = {
            f"skills/{other_skill}/scripts"
            for other_skill in skill_script_dirs
            if other_skill != skill_name
        }
        for path in sorted(scripts_dir.glob("*.py")):
            source = path.read_text(encoding="utf-8")
            for other_path in sorted(other_script_paths):
                if other_path in source:
                    violations.append(
                        f"{path.relative_to(ROOT)} references cross-skill path {other_path}"
                    )

            tree = ast.parse(source, filename=str(path))
            for node in ast.walk(tree):
                imported = []
                if isinstance(node, ast.Import):
                    imported = [alias.name.split(".", 1)[0] for alias in node.names]
                elif (
                    isinstance(node, ast.ImportFrom) and node.level == 0 and node.module
                ):
                    imported = [node.module.split(".", 1)[0]]
                for module in imported:
                    if module in module_owners and module not in local_modules:
                        owners = sorted(module_owners[module])
                        violations.append(
                            f"{path.relative_to(ROOT)}:{node.lineno} imports {module!r} "
                            f"owned by other skill(s) {owners}"
                        )

    assert not violations, (
        "Every skill must remain self-contained; scripts may only import their own "
        f"skill's modules: {violations}"
    )


def test_skill_local_import_graphs_are_acyclic():
    cycles = []
    for scripts_dir in sorted((ROOT / "skills").glob("*/scripts")):
        modules = {path.stem: path for path in scripts_dir.glob("*.py")}
        graph = {name: set() for name in modules}
        for name, path in modules.items():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                imported = []
                if isinstance(node, ast.Import):
                    imported = [alias.name.split(".", 1)[0] for alias in node.names]
                elif (
                    isinstance(node, ast.ImportFrom) and node.level == 0 and node.module
                ):
                    imported = [node.module.split(".", 1)[0]]
                graph[name].update(module for module in imported if module in modules)

        visited = set()
        active = []

        def visit(module):
            if module in active:
                cycle = active[active.index(module) :] + [module]
                cycles.append(f"{scripts_dir.parent.name}: {' -> '.join(cycle)}")
                return
            if module in visited:
                return
            active.append(module)
            for dependency in sorted(graph[module]):
                visit(dependency)
            active.pop()
            visited.add(module)

        for module in sorted(graph):
            visit(module)

    assert not cycles, f"Skill-local script imports must remain acyclic: {cycles}"


def test_public_entrypoints_have_no_private_compatibility_surface():
    violations = []
    for relative in PUBLIC_ENTRYPOINTS:
        path = ROOT / relative
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        private_imports = []
        declared_exports = None
        for node in tree.body:
            if isinstance(node, ast.ImportFrom):
                private_imports.extend(
                    alias.asname or alias.name
                    for alias in node.names
                    if (alias.asname or alias.name).startswith("_")
                )
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "__all__"
                for target in node.targets
            ):
                declared_exports = ast.literal_eval(node.value)

        if private_imports:
            violations.append(
                f"{relative} imports private compatibility symbols: {private_imports}"
            )
        if declared_exports is None:
            violations.append(f"{relative} must declare its public __all__")
        elif any(str(name).startswith("_") for name in declared_exports):
            violations.append(
                f"{relative} exports private compatibility symbols: {declared_exports}"
            )
        else:
            missing = REQUIRED_PUBLIC_EXPORTS.get(relative, set()) - set(
                declared_exports
            )
            if missing:
                violations.append(
                    f"{relative} dropped established public exports: {sorted(missing)}"
                )

    assert not violations, f"Entrypoints must expose public APIs only: {violations}"
