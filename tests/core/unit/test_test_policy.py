from __future__ import annotations

import ast
import os
import re
import sys
from pathlib import Path

import pytest
import conftest as core_conftest
from policy import (
    INVARIANT_ID_PATTERN,
    parse_invariant_registry,
)


REPO_ROOT = Path(__file__).resolve().parents[3]
TESTS_ROOT = REPO_ROOT / "tests"
CORE_ROOT = TESTS_ROOT / "core"
TDD_ROOT = TESTS_ROOT / "tdd"
INVARIANTS_PATH = CORE_ROOT / "INVARIANTS.md"
PYTEST_FILE_PATTERN = re.compile(r"(?:test_.*|.*_test)\.py$")
FORBIDDEN_IMPORTS = (
    "assistant_agent.tools.plugins.builtin",
    "evals.agent",
    "assistant_agent.providers",
    "assistant_agent.memory.mem0",
)


def _module(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _is_pytest_file(path: Path) -> bool:
    return PYTEST_FILE_PATTERN.fullmatch(path.name) is not None


def _is_allowed_pytest_path(path: Path) -> bool:
    if CORE_ROOT in path.parents:
        return True
    if TDD_ROOT not in path.parents:
        return False
    return len(path.relative_to(TDD_ROOT).parts) >= 2


def _core_test_files() -> list[Path]:
    return [path for path in CORE_ROOT.rglob("*.py") if _is_pytest_file(path)]


def _registered_invariants() -> dict[str, set[str]]:
    return parse_invariant_registry(INVARIANTS_PATH)


def _registered_core_test_paths() -> set[str]:
    return {
        path
        for paths in _registered_invariants().values()
        for path in paths
    }


def _unregistered_core_test_files() -> list[Path]:
    registered = _registered_core_test_paths()
    return [
        path.relative_to(REPO_ROOT)
        for path in _core_test_files()
        if path.relative_to(REPO_ROOT).as_posix() not in registered
    ]


def _missing_registered_core_test_files() -> list[Path]:
    return sorted(
        Path(path)
        for path in _registered_core_test_paths()
        if not (REPO_ROOT / path).is_file()
    )


def _marker_invariant_id(decorator: ast.expr) -> str | None:
    if (
        isinstance(decorator, ast.Call)
        and isinstance(decorator.func, ast.Attribute)
        and decorator.func.attr == "core_invariant"
        and isinstance(decorator.func.value, ast.Attribute)
        and decorator.func.value.attr == "mark"
        and isinstance(decorator.func.value.value, ast.Name)
        and decorator.func.value.value.id == "pytest"
        and len(decorator.args) == 1
        and decorator.keywords == []
        and isinstance(decorator.args[0], ast.Constant)
        and isinstance(decorator.args[0].value, str)
        and INVARIANT_ID_PATTERN.fullmatch(decorator.args[0].value)
        is not None
    ):
        return decorator.args[0].value
    return None


def _decorator_invariant_ids(
    decorators: list[ast.expr],
) -> set[str]:
    marker_ids: set[str] = set()
    for decorator in decorators:
        marker_id = _marker_invariant_id(decorator)
        if marker_id is not None:
            marker_ids.add(marker_id)
    return marker_ids


def _module_core_item_invariant_ids(module: ast.Module) -> set[str]:
    marker_ids: set[str] = set()
    for node in module.body:
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test_")
        ):
            marker_ids.update(_decorator_invariant_ids(node.decorator_list))
            continue
        if not (
            isinstance(node, ast.ClassDef)
            and node.name.startswith("Test")
        ):
            continue
        class_marker_ids = _decorator_invariant_ids(node.decorator_list)
        for item in node.body:
            if (
                isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                and item.name.startswith("test_")
            ):
                marker_ids.update(class_marker_ids)
                marker_ids.update(
                    _decorator_invariant_ids(item.decorator_list)
                )
    return marker_ids


def _core_item_invariant_ids_by_path() -> dict[str, set[str]]:
    return {
        path.relative_to(REPO_ROOT).as_posix(): (
            _module_core_item_invariant_ids(_module(path))
        )
        for path in _core_test_files()
    }


def _core_item_invariant_ids() -> set[str]:
    return {
        marker_id
        for marker_ids in _core_item_invariant_ids_by_path().values()
        for marker_id in marker_ids
    }


def _uncovered_registered_invariant_ids() -> list[str]:
    markers_by_path = _core_item_invariant_ids_by_path()
    return sorted(
        invariant_id
        for invariant_id, responsible_paths in (
            _registered_invariants().items()
        )
        if not any(
            invariant_id in markers_by_path.get(path, set())
            for path in responsible_paths
        )
    )


def _misowned_core_item_markers() -> list[tuple[Path, str]]:
    registered = _registered_invariants()
    offenders: list[tuple[Path, str]] = []
    for path, marker_ids in _core_item_invariant_ids_by_path().items():
        for marker_id in marker_ids:
            if (
                marker_id in registered
                and path not in registered[marker_id]
            ):
                offenders.append((Path(path), marker_id))
    return sorted(offenders)


def _is_stable_assertion_token(value: str) -> bool:
    return (
        value.endswith("-sentinel")
        or INVARIANT_ID_PATTERN.fullmatch(value) is not None
        or re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:-]*", value) is not None
    )


def _human_copy_assertion_offenders() -> list[tuple[Path, str]]:
    offenders: list[tuple[Path, str]] = []
    for path in _core_test_files():
        for assertion in (node for node in ast.walk(_module(path)) if isinstance(node, ast.Assert)):
            for constant in (
                node
                for node in ast.walk(assertion)
                if isinstance(node, ast.Constant) and isinstance(node.value, str)
            ):
                value = constant.value
                if (
                    (any("\u4e00" <= character <= "\u9fff" for character in value) or any(character.isspace() for character in value))
                    and not _is_stable_assertion_token(value)
                ):
                    offenders.append((path.relative_to(REPO_ROOT), value))
    return offenders


def _imports_forbidden_module(module_name: str) -> bool:
    return any(
        module_name == forbidden or module_name.startswith(f"{forbidden}.")
        for forbidden in FORBIDDEN_IMPORTS
    )


def _forbidden_import_offenders() -> list[tuple[Path, str]]:
    offenders: list[tuple[Path, str]] = []
    for path in _core_test_files():
        for node in ast.walk(_module(path)):
            if isinstance(node, ast.Import):
                imported_modules = (alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                if _imports_forbidden_module(node.module):
                    imported_modules = (
                        (
                            node.module
                            if node.module not in FORBIDDEN_IMPORTS
                            or alias.name == "*"
                            else f"{node.module}.{alias.name}"
                        )
                        for alias in node.names
                    )
                else:
                    imported_modules = (
                        f"{node.module}.{alias.name}"
                        for alias in node.names
                    )
            else:
                continue
            for module_name in imported_modules:
                if _imports_forbidden_module(module_name):
                    offenders.append((path.relative_to(REPO_ROOT), module_name))
    return offenders


@pytest.mark.core_invariant("POLICY-001")
def test_marker_scanner_rejects_non_item_and_non_pytest_decorators(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = ast.parse(
        """
@fake.core_invariant("BOOT-001")
def test_fake_decorator():
    pass

@core_invariant("RUN-001")
def test_alias_decorator():
    pass

def helper():
    @pytest.mark.core_invariant("LOOP-001")
    def test_nested():
        pass

if False:
    @pytest.mark.core_invariant("TOOL-001")
    def test_conditional():
        pass

class Helper:
    @pytest.mark.core_invariant("GATE-001")
    def test_non_test_class_method(self):
        pass

@pytest.mark.core_invariant("POLICY-001")
def test_top_level():
    pass

class TestProbe:
    @pytest.mark.core_invariant("EXT-001")
    def test_direct_method(self):
        pass
"""
    )
    monkeypatch.setattr(
        sys.modules[__name__],
        "_core_test_files",
        lambda: [REPO_ROOT / "tests/core/unit/test_test_policy.py"],
    )
    monkeypatch.setattr(sys.modules[__name__], "_module", lambda path: module)

    assert _core_item_invariant_ids() == {"POLICY-001", "EXT-001"}


@pytest.mark.core_invariant("POLICY-001")
def test_marker_coverage_requires_the_registered_responsible_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = ast.parse(
        """
@pytest.mark.core_invariant("TOOL-001")
def test_wrong_responsible_file():
    pass
"""
    )
    monkeypatch.setattr(
        sys.modules[__name__],
        "_registered_invariants",
        lambda: {
            "TOOL-001": {
                "tests/core/contract/test_tool_contract.py",
            }
        },
    )
    monkeypatch.setattr(
        sys.modules[__name__],
        "_core_test_files",
        lambda: [
            REPO_ROOT / "tests/core/contract/test_extension_contract.py",
        ],
    )
    monkeypatch.setattr(sys.modules[__name__], "_module", lambda path: module)

    assert _uncovered_registered_invariant_ids() == ["TOOL-001"]


@pytest.mark.core_invariant("POLICY-001")
def test_registry_requires_contract_and_responsible_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    registry = tmp_path / "INVARIANTS.md"
    registry.write_text(
        "| HACK-001 |\n"
        "| EMPTY-001 | | `tests/core/unit/test_test_policy.py` |\n"
        "| PATH-001 | contract-sentinel | missing-path-sentinel |\n"
        "| BOOT-001 | contract-sentinel | "
        "`tests/core/integration/test_runtime_lifecycle.py` |\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(core_conftest, "INVARIANTS_PATH", registry)

    assert core_conftest.registered_invariant_ids() == {"BOOT-001"}


@pytest.mark.core_invariant("POLICY-001")
def test_wrong_file_marker_is_an_ownership_offender_even_when_covered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    correct_path = (
        REPO_ROOT / "tests/core/contract/test_tool_contract.py"
    )
    wrong_path = (
        REPO_ROOT / "tests/core/contract/test_extension_contract.py"
    )
    module = ast.parse(
        """
@pytest.mark.core_invariant("TOOL-001")
def test_probe():
    pass
"""
    )
    monkeypatch.setattr(
        sys.modules[__name__],
        "_registered_invariants",
        lambda: {
            "TOOL-001": {
                "tests/core/contract/test_tool_contract.py",
            }
        },
    )
    monkeypatch.setattr(
        sys.modules[__name__],
        "_core_test_files",
        lambda: [correct_path, wrong_path],
    )
    monkeypatch.setattr(
        sys.modules[__name__],
        "_module",
        lambda path: module,
    )

    assert _uncovered_registered_invariant_ids() == []
    assert _misowned_core_item_markers() == [
        (
            Path("tests/core/contract/test_extension_contract.py"),
            "TOOL-001",
        )
    ]


@pytest.mark.core_invariant("POLICY-001")
def test_provider_subtree_imports_are_forbidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = REPO_ROOT / "tests/core/unit/test_test_policy.py"
    module = ast.parse(
        """
import assistant_agent.providers.qwen_image_generation
from assistant_agent.providers import qwen_realtime_vision
from assistant_agent.providers.ark_image_generation import ArkImageProvider
"""
    )
    monkeypatch.setattr(
        sys.modules[__name__],
        "_core_test_files",
        lambda: [path],
    )
    monkeypatch.setattr(
        sys.modules[__name__],
        "_module",
        lambda candidate: module,
    )

    assert _forbidden_import_offenders() == [
        (
            Path("tests/core/unit/test_test_policy.py"),
            "assistant_agent.providers.qwen_image_generation",
        ),
        (
            Path("tests/core/unit/test_test_policy.py"),
            "assistant_agent.providers.qwen_realtime_vision",
        ),
        (
            Path("tests/core/unit/test_test_policy.py"),
            "assistant_agent.providers.ark_image_generation",
        ),
    ]


@pytest.mark.core_invariant("POLICY-001")
def test_python_tests_exist_only_under_core_or_tdd_features() -> None:
    offenders = [
        path
        for path in TESTS_ROOT.rglob("*.py")
        if _is_pytest_file(path) and not _is_allowed_pytest_path(path)
    ]
    assert offenders == []


@pytest.mark.core_invariant("POLICY-001")
def test_tdd_tests_require_a_feature_subdirectory() -> None:
    assert _is_allowed_pytest_path(TDD_ROOT / "feature" / "test_probe.py")
    assert _is_allowed_pytest_path(TDD_ROOT / "feature" / "probe_test.py")
    assert not _is_allowed_pytest_path(TDD_ROOT / "test_probe.py")
    assert not _is_allowed_pytest_path(TESTS_ROOT / "feature" / "test_probe.py")


@pytest.mark.core_invariant("POLICY-001")
def test_default_collection_excludes_tdd(pytestconfig: pytest.Config) -> None:
    assert list(pytestconfig.getini("testpaths")) == ["tests/core"]


@pytest.mark.core_invariant("POLICY-001")
def test_core_test_files_are_registered() -> None:
    assert _unregistered_core_test_files() == []


@pytest.mark.core_invariant("POLICY-001")
def test_registered_core_test_files_exist() -> None:
    assert _missing_registered_core_test_files() == []


@pytest.mark.core_invariant("POLICY-001")
def test_registered_invariants_have_core_item_markers() -> None:
    assert _uncovered_registered_invariant_ids() == []


@pytest.mark.core_invariant("POLICY-001")
def test_core_item_markers_match_registered_responsible_files() -> None:
    assert _misowned_core_item_markers() == []


@pytest.mark.core_invariant("POLICY-001")
def test_core_assertions_do_not_bind_human_copy() -> None:
    assert _human_copy_assertion_offenders() == []


@pytest.mark.core_invariant("POLICY-001")
def test_core_tests_do_not_import_feature_implementations() -> None:
    assert _forbidden_import_offenders() == []


@pytest.mark.core_invariant("POLICY-001")
def test_core_tests_force_mock_provider_mode() -> None:
    assert os.environ["MULTIMODAL_AGENT_PROVIDER_MODE"] == "mock"
