import ast
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
SOURCE = ROOT / "src"


def _module_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    return imports


def _project_modules() -> dict[str, Path]:
    modules: dict[str, Path] = {}
    package_root = SOURCE / "mini_agent"
    for path in package_root.rglob("*.py"):
        parts = list(path.relative_to(package_root).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        name = "mini_agent" + (f".{'.'.join(parts)}" if parts else "")
        modules[name] = path
    return modules


def _resolved_project_imports(module: str, path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    package = module if path.name == "__init__.py" else module.rpartition(".")[0]
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names if alias.name.startswith("mini_agent"))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parent = package.split(".")
                target = ".".join(parent[: len(parent) - node.level + 1] + ([node.module] if node.module else []))
            else:
                target = node.module or ""
            if target.startswith("mini_agent"):
                imports.add(target)
    return imports


def _run_isolated_import(statement: str, forbidden: tuple[str, ...]) -> None:
    checks = "\n".join(f"assert {name!r} not in sys.modules, {name!r}" for name in forbidden)
    environment = {**os.environ, "PYTHONPATH": str(SOURCE)}
    subprocess.run(
        [sys.executable, "-c", f"import sys\n{statement}\n{checks}"],
        cwd=ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )


def test_domain_import_does_not_load_outer_layers() -> None:
    _run_isolated_import(
        "import mini_agent.domain",
        (
            "mini_agent.runtime",
            "mini_agent.planning",
            "mini_agent.providers",
            "mini_agent.storage",
            "mini_agent.tools",
            "requests",
        ),
    )


def test_runtime_event_import_does_not_load_application_graph() -> None:
    _run_isolated_import(
        "import mini_agent.runtime.events",
        ("mini_agent.planning", "mini_agent.providers", "mini_agent.storage", "mini_agent.tools", "requests"),
    )


def test_project_module_graph_has_no_cycles() -> None:
    modules = _project_modules()
    graph = {
        module: {target for target in _resolved_project_imports(module, path) if target in modules}
        for module, path in modules.items()
    }
    visited: set[str] = set()
    active: list[str] = []

    def visit(module: str) -> None:
        if module in active:
            cycle = active[active.index(module) :] + [module]
            raise AssertionError("Module dependency cycle: " + " -> ".join(cycle))
        if module in visited:
            return
        active.append(module)
        for dependency in graph[module]:
            visit(dependency)
        active.pop()
        visited.add(module)

    for module in modules:
        visit(module)


def test_core_runtime_and_planning_do_not_import_provider_implementations() -> None:
    workflow_paths = sorted((SOURCE / "mini_agent" / "runtime" / "workflows").glob("*.py"))
    paths = [
        SOURCE / "mini_agent" / "planning" / "llm.py",
        SOURCE / "mini_agent" / "runtime" / "routing.py",
        *workflow_paths,
    ]

    for path in paths:
        assert not any(name == "mini_agent.providers" for name in _module_imports(path)), path


def test_deepseek_adapter_does_not_own_http_transport() -> None:
    imports = _module_imports(SOURCE / "mini_agent" / "providers" / "deepseek.py")

    assert "requests" not in imports


def test_application_services_depend_on_runner_port() -> None:
    paths = [
        SOURCE / "mini_agent" / "runtime" / "application.py",
        SOURCE / "mini_agent" / "runtime" / "conversations.py",
    ]

    for path in paths:
        imports = _module_imports(path)
        assert not any(name == "runner" or name.endswith(".runner") for name in imports), path


def test_runtime_execution_core_does_not_import_planning_package() -> None:
    workflow_paths = sorted((SOURCE / "mini_agent" / "runtime" / "workflows").glob("*.py"))
    paths = [SOURCE / "mini_agent" / "runtime" / "routing.py", *workflow_paths]

    for path in paths:
        imports = _module_imports(path)
        assert not any(name == "mini_agent.planning" or name.startswith("mini_agent.planning.") for name in imports), (
            path
        )


def test_runtime_factory_is_only_a_compatibility_entrypoint() -> None:
    imports = _module_imports(SOURCE / "mini_agent" / "runtime" / "factory.py")

    assert "mini_agent.bootstrap" in imports
    assert not any(
        name == package or name.startswith(f"{package}.")
        for package in ("mini_agent.planning", "mini_agent.providers", "mini_agent.storage", "mini_agent.tools")
        for name in imports
    )


def test_storage_artifacts_do_not_import_runtime_layer() -> None:
    imports = _module_imports(SOURCE / "mini_agent" / "storage" / "artifacts.py")

    assert not any(name == "mini_agent.runtime" or name.startswith("mini_agent.runtime.") for name in imports)


def test_runtime_artifacts_module_is_a_compatibility_export() -> None:
    imports = _module_imports(SOURCE / "mini_agent" / "runtime" / "artifacts.py")

    assert imports == {"mini_agent.storage.artifacts"}


def test_tool_registry_does_not_call_catalog_private_builders() -> None:
    source = (SOURCE / "mini_agent" / "tools" / "registry.py").read_text(encoding="utf-8")

    assert "_build_tools" not in source


def test_tool_catalog_does_not_depend_on_registry_implementation() -> None:
    source = (SOURCE / "mini_agent" / "tools" / "catalog.py").read_text(encoding="utf-8")

    assert "registry" not in _module_imports(SOURCE / "mini_agent" / "tools" / "catalog.py")
    assert "ToolRegistry" not in source
