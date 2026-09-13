from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication, QMessageBox

from odds_pup.storage import DataPaths, InstanceLock, list_backups
from odds_pup.ui import app as app_module


@pytest.fixture
def quiet_message_boxes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    shown: list[str] = []

    def record(*args: object, **_kwargs: object) -> int:
        shown.append(str(args[2]))
        return 0

    monkeypatch.setattr(QMessageBox, "critical", record)
    return shown


def test_run_opens_ledger_and_releases_lock(
    qapp, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, quiet_message_boxes: list[str]
):
    monkeypatch.setattr(QApplication, "exec", lambda self: 0)
    data_dir = tmp_path / "ledger"
    assert app_module.run(["--data-dir", str(data_dir)]) == 0
    paths = DataPaths(data_dir)
    assert paths.database.exists()
    assert len(list_backups(paths.backups)) == 1  # startup backup
    assert quiet_message_boxes == []
    lock = InstanceLock(paths.lock)  # released on exit
    lock.acquire()
    lock.release()


def test_run_refuses_a_second_instance(
    qapp, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, quiet_message_boxes: list[str]
):
    monkeypatch.setattr(QApplication, "exec", lambda self: 0)
    data_dir = tmp_path / "ledger"
    data_dir.mkdir()
    with InstanceLock(DataPaths(data_dir).lock):
        assert app_module.run(["--data-dir", str(data_dir)]) == 1
    assert quiet_message_boxes
    assert "already open" in quiet_message_boxes[0]


def test_run_refuses_newer_database(
    qapp, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, quiet_message_boxes: list[str]
):
    import sqlite3

    monkeypatch.setattr(QApplication, "exec", lambda self: 0)
    data_dir = tmp_path / "ledger"
    data_dir.mkdir()
    conn = sqlite3.connect(DataPaths(data_dir).database)
    conn.execute("PRAGMA user_version = 42")
    conn.close()
    assert app_module.run(["--data-dir", str(data_dir)]) == 1
    assert "newer odds-pup" in quiet_message_boxes[0]


def test_main_version_does_not_need_qt(capsys):
    from odds_pup.__main__ import main

    assert main(["--version"]) == 0
    assert capsys.readouterr().out.startswith("odds-pup ")
