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


def test_core_runtime_and_planning_do_not_import_provider_implementations() -> None:
    paths = [
        SOURCE / "mini_agent" / "planning" / "llm.py",
        SOURCE / "mini_agent" / "runtime" / "routing.py",
        SOURCE / "mini_agent" / "runtime" / "workflows.py",
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
