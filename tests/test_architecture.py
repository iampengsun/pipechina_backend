from __future__ import annotations

import ast
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULES_ROOT = ROOT / "app" / "modules"
BUSINESS_MODULES = {
    path.name for path in MODULES_ROOT.iterdir() if path.is_dir() and not path.name.startswith("_")
}
LAYERS = {"api", "application", "domain", "infrastructure"}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_python_314_is_the_declared_and_executed_runtime() -> None:
    assert sys.version_info[:2] == (3, 14)
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["requires-python"] == ">=3.14,<3.15"
    assert project["tool"]["ruff"]["target-version"] == "py314"
    assert project["tool"]["mypy"]["python_version"] == "3.14"


def test_each_business_module_owns_its_api_application_and_domain() -> None:
    for module_name in BUSINESS_MODULES:
        module = MODULES_ROOT / module_name
        required_layers = {"api", "application", "domain"}
        assert {path.name for path in module.iterdir() if path.is_dir()} >= required_layers
        for layer in required_layers:
            assert (module / layer / "__init__.py").is_file()
        assert (module / "api" / "router.py").is_file()
        assert (module / "application" / "service.py").is_file()
        assert (module / "domain" / "models.py").is_file()
        assert (module / "domain" / "schemas.py").is_file()


def test_business_modules_do_not_regress_to_flat_technical_folders() -> None:
    forbidden = {"router.py", "service.py", "tasks.py", "models.py", "schemas.py", "workflow.py"}
    for module_name in BUSINESS_MODULES:
        actual = {path.name for path in (MODULES_ROOT / module_name).glob("*.py")}
        assert actual.isdisjoint(forbidden)


def test_layer_dependencies_point_inward() -> None:
    forbidden_by_layer = {
        "domain": (".api", ".application", ".infrastructure"),
        "infrastructure": (".api", ".application"),
        "application": (".api",),
        "api": (".infrastructure",),
    }
    for module_name in BUSINESS_MODULES:
        for layer, forbidden_fragments in forbidden_by_layer.items():
            for path in (MODULES_ROOT / module_name / layer).glob("*.py"):
                for imported in _imports(path):
                    assert not any(fragment in imported for fragment in forbidden_fragments), (
                        f"{path.relative_to(ROOT)} imports forbidden layer {imported}"
                    )


def test_excluded_search_stack_is_not_imported() -> None:
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "app").rglob("*.py")
        if "__pycache__" not in path.parts
    ).lower()
    for forbidden in ("langchain_community.vectorstores", "chromadb", "pinecone"):
        assert forbidden not in source


def test_business_modules_depend_on_ports_not_outbound_adapters() -> None:
    for path in MODULES_ROOT.rglob("*.py"):
        for imported in _imports(path):
            assert not imported.startswith(("elasticsearch", "qdrant_client", "neo4j")), (
                f"{path.relative_to(ROOT)} bypasses a search port via {imported}"
            )
            assert not imported.startswith("app.infrastructure"), (
                f"{path.relative_to(ROOT)} bypasses an application port via {imported}"
            )


def test_ports_do_not_depend_on_business_or_infrastructure() -> None:
    for path in (ROOT / "app" / "ports").glob("*.py"):
        for imported in _imports(path):
            forbidden = ("app.modules", "app.infrastructure", "app.bootstrap")
            assert not imported.startswith(forbidden), (
                f"{path.relative_to(ROOT)} contains an outward dependency on {imported}"
            )


def test_agent_business_workflows_do_not_leak_into_shared_primitives() -> None:
    for path in (ROOT / "app/shared/agents").glob("*.py"):
        assert not any(name.startswith("app.modules") for name in _imports(path))
    assert (MODULES_ROOT / "operation_event/application/investigation.py").is_file()


def test_legacy_monolithic_ai_provider_was_removed() -> None:
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "app").rglob("*.py")
        if "__pycache__" not in path.parts
    )
    assert "class AIProvider" not in source
    assert "app.shared.providers" not in source
    assert "app.shared.storage" not in source


def test_modules_do_not_call_another_modules_api_layer() -> None:
    for module_name in BUSINESS_MODULES:
        for path in (MODULES_ROOT / module_name).rglob("*.py"):
            for imported in _imports(path):
                parts = imported.split(".")
                if parts[:2] == ["app", "modules"] and len(parts) > 3:
                    assert parts[2] == module_name or parts[3] != "api", (
                        f"{path.relative_to(ROOT)} depends on another module's HTTP adapter"
                    )
