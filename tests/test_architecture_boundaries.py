import ast
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
SOURCE = ROOT / "backend" / "src"


def _module_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add("." * node.level + node.module)
    return imports


def _package_imports(path: Path) -> set[str]:
    return {imported for module in path.rglob("*.py") for imported in _module_imports(module)}


def _run_isolated_import(statement: str, forbidden: tuple[str, ...]) -> None:
    checks = "\n".join(f"assert {name!r} not in sys.modules, {name!r}" for name in forbidden)
    subprocess.run(
        [sys.executable, "-c", f"import sys\n{statement}\n{checks}"],
        cwd=ROOT,
        env=os.environ,
        check=True,
        capture_output=True,
        text=True,
    )


def test_domain_import_does_not_load_outer_layers() -> None:
    _run_isolated_import(
        "import backend.domain",
        ("backend.runtime", "backend.planning", "backend.providers", "backend.storage", "backend.tools"),
    )


def test_runtime_event_import_does_not_load_application_graph() -> None:
    _run_isolated_import(
        "import backend.runtime.core.events",
        ("backend.planning", "backend.providers", "backend.storage", "backend.tools", "requests"),
    )


def test_jobs_import_does_not_load_outer_layers_or_third_party() -> None:
    _run_isolated_import(
        "import backend.jobs",
        (
            "backend.runtime",
            "backend.tools",
            "backend.mcp",
            "backend.api",
            "backend.storage",
            "backend.providers",
            "backend.planning",
            "requests",
            "fastapi",
        ),
    )


def test_chat_completions_adapter_does_not_own_http_transport() -> None:
    imports = _package_imports(SOURCE / "providers" / "chat_completions")
    assert "requests" not in imports


def test_removed_account_cloud_and_sync_sources_are_absent() -> None:
    removed_sources = (
        ROOT / "cloud" / "pyproject.toml",
        ROOT / "cloud" / "src" / "cloud",
        SOURCE / "api" / "auth",
        SOURCE / "api" / "sync_routes.py",
        SOURCE / "cloud",
        SOURCE / "storage" / "auth",
        SOURCE / "sync",
    )
    for path in removed_sources:
        if path.suffix:
            assert not path.exists()
        else:
            assert not any(path.rglob("*.py"))


def test_backend_has_no_cloud_database_or_mail_transport_imports() -> None:
    imports = _package_imports(SOURCE)
    forbidden = {"psycopg", "smtplib", "pwdlib", "backend.cloud", "backend.sync"}
    assert not any(name.split(".", 1)[0] in forbidden for name in imports)


def test_application_services_depend_on_runner_port() -> None:
    paths = [
        SOURCE / "runtime" / "application" / "services.py",
        SOURCE / "runtime" / "conversation" / "service.py",
    ]
    for path in paths:
        imports = _module_imports(path)
        assert not any(name == "runner" or name.endswith(".runner") for name in imports), path
