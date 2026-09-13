"""Application entry: open the ledger (which takes the single-instance lock), show the window.

SPEC §11.3-§11.6.
"""

from __future__ import annotations

import argparse
import sys

from PySide6.QtWidgets import QApplication, QMessageBox

from odds_pup import __version__
from odds_pup.core import CoreError
from odds_pup.storage import DataPaths, Repository, StorageError, resolve_data_dir
from odds_pup.ui.main_window import MainWindow
from odds_pup.ui.settings import APPLICATION, ORGANISATION, UiSettings


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="odds-pup", description="Matched betting ledger.")
    parser.add_argument("--version", action="version", version=f"odds-pup {__version__}")
    parser.add_argument(
        "--data-dir",
        help="Directory holding the ledger database and backups "
        "(default: platform user data dir, or $ODDS_PUP_DATA_DIR).",
    )
    return parser.parse_args(argv)


def run(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    paths = DataPaths(resolve_data_dir(args.data_dir))
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setOrganizationName(ORGANISATION)
    app.setApplicationName(APPLICATION)
    app.setApplicationVersion(__version__)
    try:
        repo = Repository.open(paths)
    except (StorageError, CoreError, OSError) as exc:
        QMessageBox.critical(None, "odds-pup", f"Cannot open the ledger in {paths.root}:\n\n{exc}")
        return 1
    window = MainWindow(repo, UiSettings(), paths=paths)
    window.show()
    try:
        return int(app.exec())
    finally:
        repo.close()
