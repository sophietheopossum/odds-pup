"""CLAUDE.md hard rule 5: core imports no Qt and no sqlite; storage imports no Qt."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "odds_pup"
QT = ("PySide6", "PyQt6", "shiboken6", "qtpy")


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def roots(names: set[str]) -> set[str]:
    return {name.split(".")[0] for name in names}


@pytest.mark.parametrize("path", sorted((SRC / "core").glob("*.py")), ids=lambda p: p.name)
def test_core_is_pure(path: Path):
    found = roots(imported_modules(path))
    assert not found & set(QT), f"{path.name} imports Qt: {found & set(QT)}"
    assert "sqlite3" not in found, f"{path.name} imports sqlite3"
    assert not (found - {"odds_pup"}) & {"os", "pathlib", "io", "socket", "urllib", "http"}, (
        f"{path.name} does I/O"
    )


@pytest.mark.parametrize("path", sorted((SRC / "storage").glob("*.py")), ids=lambda p: p.name)
def test_storage_has_no_qt(path: Path):
    found = roots(imported_modules(path))
    assert not found & set(QT), f"{path.name} imports Qt: {found & set(QT)}"


def test_nothing_uses_float_for_money_in_core():
    for path in (SRC / "core").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "float":
                pytest.fail(f"{path.name} calls float()")
