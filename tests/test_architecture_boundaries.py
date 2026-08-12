import ast
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
SOURCE = ROOT / "backend" / "src"
CLOUD_SOURCE = ROOT / "cloud" / "src"


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
    environment = {**os.environ, "PYTHONPATH": os.pathsep.join((str(SOURCE), str(CLOUD_SOURCE)))}
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
        "import backend.domain",
        ("backend.runtime", "backend.planning", "backend.providers", "backend.storage", "backend.tools"),
    )


def test_runtime_event_import_does_not_load_application_graph() -> None:
    _run_isolated_import(
        "import backend.runtime.core.events",
        ("backend.planning", "backend.providers", "backend.storage", "backend.tools", "requests"),
    )


def test_deepseek_adapter_does_not_own_http_transport() -> None:
    imports = _package_imports(SOURCE / "backend" / "providers" / "deepseek")
    assert "requests" not in imports


def test_backend_does_not_import_tui() -> None:
    imports = _package_imports(SOURCE / "backend")
    assert not any(name == "tui" or name.startswith("tui.") for name in imports)


def test_cloud_app_does_not_load_backend_runtime() -> None:
    _run_isolated_import(
        "import cloud.api.app",
        ("backend", "backend.runtime", "backend.providers", "backend.tools", "backend.storage"),
    )


def test_cloud_source_has_no_local_runtime_or_sqlite_imports() -> None:
    imports = _package_imports(CLOUD_SOURCE / "cloud")
    assert not any(name == "sqlite3" or name == "backend" or name.startswith("backend.") for name in imports)


def test_backend_has_no_cloud_database_or_mail_transport_imports() -> None:
    imports = _package_imports(SOURCE / "backend")
    forbidden = {"psycopg", "smtplib", "pwdlib"}
    assert not any(name.split(".", 1)[0] in forbidden for name in imports)


def test_application_services_depend_on_runner_port() -> None:
    paths = [
        SOURCE / "backend" / "runtime" / "application" / "services.py",
        SOURCE / "backend" / "runtime" / "conversation" / "service.py",
    ]
    for path in paths:
        imports = _module_imports(path)
        assert not any(name == "runner" or name.endswith(".runner") for name in imports), path
