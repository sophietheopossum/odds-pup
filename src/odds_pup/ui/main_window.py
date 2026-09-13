"""Ledger window: filters, table, summary strip, detail pane and all bet actions. SPEC §13."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QDate, QSortFilterProxyModel, Qt, QTimer
from PySide6.QtGui import QAction, QCloseEvent, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDockWidget,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QSplitter,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from odds_pup import __version__
from odds_pup.core import BetStatus, BetType, CoreError, TwoWayOutcome, VenueKind
from odds_pup.storage import (
    OPEN_STATUSES,
    BetFilter,
    BetRecord,
    DataPaths,
    NewBet,
    Repository,
    StorageError,
    export_csv,
    local_date_range,
    utc_now,
)
from odds_pup.ui.detail_pane import DetailPane
from odds_pup.ui.dialogs import (
    AdjustDialog,
    BetDialog,
    BetDialogMode,
    BetDialogResult,
    SettleDialog,
    SettleRequest,
)
from odds_pup.ui.format import TYPE_LABELS
from odds_pup.ui.ledger_model import SORT_ROLE, LedgerModel
from odds_pup.ui.settings import UiSettings
from odds_pup.ui.summary_strip import BookmakerTable, SummaryStrip
from odds_pup.ui.venues_dialog import VenuesDialog

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

SEARCH_DEBOUNCE_MS = 250
PARENT_CANDIDATES = 50

STATUS_CHOICES: tuple[tuple[str, frozenset[BetStatus] | None], ...] = (
    ("All statuses", None),
    ("Open or part-settled", OPEN_STATUSES),
    ("Open", frozenset({BetStatus.OPEN})),
    ("Part-settled", frozenset({BetStatus.PARTIALLY_SETTLED})),
    ("Settled", frozenset({BetStatus.SETTLED})),
    ("Void", frozenset({BetStatus.VOID})),
)


def _as_date(value: object) -> date:
    if isinstance(value, date):
        return value
    raise TypeError(f"expected a date from Qt, got {type(value).__name__}")


class MainWindow(QMainWindow):
    def __init__(
        self,
        repo: Repository,
        settings: UiSettings | None = None,
        *,
        paths: DataPaths | None = None,
        clock: Callable[[], datetime] = utc_now,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.repo = repo
        self.settings = settings or UiSettings()
        self.paths = paths
        self.clock = clock
        self.show_error: Callable[[str], None] = self._message_box_error
        self.confirm: Callable[[str, str], bool] = self._message_box_confirm
        self.setWindowTitle("odds-pup")
        self.model = LedgerModel()
        self.proxy = QSortFilterProxyModel()
        self.proxy.setSourceModel(self.model)
        self.proxy.setSortRole(SORT_ROLE)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(SEARCH_DEBOUNCE_MS)
        self._search_timer.timeout.connect(self.refresh_ledger)
        self._build()
        self._build_actions()
        self.refresh()
        geometry = self.settings.geometry()
        if not geometry.isEmpty():
            self.restoreGeometry(geometry)
        state = self.settings.window_state()
        if not state.isEmpty():
            self.restoreState(state)

    # -- construction --------------------------------------------------------

    def _build(self) -> None:
        central = QWidget()
        layout = QVBoxLayout(central)
        self.summary = SummaryStrip()
        layout.addWidget(self.summary)

        filters = QHBoxLayout()
        self.status_filter = QComboBox()
        for label, statuses in STATUS_CHOICES:
            self.status_filter.addItem(label, statuses)
        self.status_filter.setAccessibleName("Status filter")
        self.type_filter = QComboBox()
        self.type_filter.addItem("All types", None)
        for bet_type in BetType:
            self.type_filter.addItem(TYPE_LABELS[bet_type], bet_type.value)
        self.type_filter.setAccessibleName("Bet type filter")
        self.bookmaker_filter = QComboBox()
        self.bookmaker_filter.setAccessibleName("Bookmaker filter")
        self.from_enabled = QCheckBox("From")
        self.from_enabled.setAccessibleName("Filter by placed-from date")
        self.from_date = QDateEdit(QDate.currentDate().addMonths(-1))
        self.from_date.setCalendarPopup(True)
        self.from_date.setDisplayFormat("dd/MM/yyyy")
        self.from_date.setAccessibleName("Placed from")
        self.from_date.setEnabled(False)
        self.to_enabled = QCheckBox("To")
        self.to_enabled.setAccessibleName("Filter by placed-to date")
        self.to_date = QDateEdit(QDate.currentDate())
        self.to_date.setCalendarPopup(True)
        self.to_date.setDisplayFormat("dd/MM/yyyy")
        self.to_date.setAccessibleName("Placed to (inclusive)")
        self.to_date.setEnabled(False)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search event, selection, market, notes")
        self.search.setClearButtonEnabled(True)
        self.search.setAccessibleName("Search")
        self.review_only = QCheckBox("Needs review")
        self.review_only.setAccessibleName("Show only bets flagged for review")
        self.show_deleted = QCheckBox("Show deleted")
        self.show_deleted.setAccessibleName("Include deleted bets")
        for widget in (
            self.status_filter,
            self.type_filter,
            self.bookmaker_filter,
            self.from_enabled,
            self.from_date,
            self.to_enabled,
            self.to_date,
        ):
            filters.addWidget(widget)
        filters.addWidget(self.search, 1)
        filters.addWidget(self.review_only)
        filters.addWidget(self.show_deleted)
        layout.addLayout(filters)

        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(0, Qt.SortOrder.DescendingOrder)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setAccessibleName("Ledger")
        self.table.setAlternatingRowColors(True)

        self.detail = DetailPane()
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self.table)
        splitter.addWidget(self.detail)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter, 1)

        self.status_label = QLabel("")
        self.statusBar().addWidget(self.status_label, 1)
        self.setCentralWidget(central)

        self.bookmaker_table = BookmakerTable()
        dock = QDockWidget("By bookmaker", self)
        dock.setObjectName("bookmakerDock")
        dock.setWidget(self.bookmaker_table)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
        self.bookmaker_dock = dock

        # Filters only re-query the ledger; the summary depends on data, not on filters.
        for combo in (self.status_filter, self.type_filter, self.bookmaker_filter):
            combo.currentIndexChanged.connect(self.refresh_ledger)
        self.from_enabled.toggled.connect(self.from_date.setEnabled)
        self.from_enabled.toggled.connect(self.refresh_ledger)
        self.to_enabled.toggled.connect(self.to_date.setEnabled)
        self.to_enabled.toggled.connect(self.refresh_ledger)
        self.from_date.dateChanged.connect(self.refresh_ledger)
        self.to_date.dateChanged.connect(self.refresh_ledger)
        self.search.textChanged.connect(self._search_timer.start)
        self.search.returnPressed.connect(self.refresh_ledger)
        self.review_only.toggled.connect(self.refresh_ledger)
        self.show_deleted.toggled.connect(self.refresh_ledger)
        self.table.selectionModel().selectionChanged.connect(self._selection_changed)
        self.table.doubleClicked.connect(lambda _index: self.edit_selected())

    def _action(self, text: str, shortcut: str | None, slot: Callable[[], object]) -> QAction:
        action = QAction(text, self)
        if shortcut:
            action.setShortcut(QKeySequence(shortcut))
        action.triggered.connect(lambda _checked=False: slot())
        return action

    def _build_actions(self) -> None:
        self.act_new = self._action("&New bet…", "Ctrl+N", self.new_bet)
        self.act_clone = self._action("&Clone bet…", "Ctrl+D", self.clone_selected)
        self.act_edit = self._action("&Edit / correct…", "Ctrl+E", self.edit_selected)
        self.act_settle = self._action("&Settle…", "Ctrl+Return", self.settle_selected)
        self.act_adjust = self._action("&Adjust realised P/L…", "Ctrl+J", self.adjust_selected)
        self.act_reopen = self._action("&Reopen", "Ctrl+R", self.reopen_selected)
        self.act_review = self._action("Toggle needs &review", "Ctrl+M", self.toggle_review)
        self.act_delete = self._action("&Delete", "Delete", self.delete_or_restore)
        self.act_venues = self._action("Bookmakers and e&xchanges…", None, self.manage_venues)
        self.act_export = self._action("&Export CSV…", "Ctrl+Shift+E", self.export_csv)
        self.act_backup = self._action("&Back up now", None, self.backup_now)
        self.act_refresh = self._action("Re&fresh", "F5", self.refresh)
        self.act_quit = self._action("&Quit", "Ctrl+Q", self.close)
        self.act_about = self._action("&About odds-pup", None, self.about)

        file_menu = self.menuBar().addMenu("&File")
        file_menu.addActions([self.act_export, self.act_backup, self.act_refresh])
        file_menu.addSeparator()
        file_menu.addAction(self.act_quit)
        bet_menu = self.menuBar().addMenu("&Bet")
        bet_menu.addActions(
            [
                self.act_new,
                self.act_clone,
                self.act_edit,
                self.act_settle,
                self.act_adjust,
                self.act_reopen,
                self.act_review,
                self.act_delete,
            ]
        )
        settings_menu = self.menuBar().addMenu("&Settings")
        settings_menu.addAction(self.act_venues)
        view_menu = self.menuBar().addMenu("&View")
        view_menu.addAction(self.bookmaker_dock.toggleViewAction())
        help_menu = self.menuBar().addMenu("&Help")
        help_menu.addAction(self.act_about)

        toolbar = self.addToolBar("Bets")
        toolbar.setObjectName("betsToolbar")
        toolbar.setMovable(False)
        toolbar.addActions(
            [self.act_new, self.act_clone, self.act_edit, self.act_settle, self.act_adjust]
        )
        toolbar.addSeparator()
        toolbar.addActions([self.act_reopen, self.act_review, self.act_delete])
        toolbar.addSeparator()
        toolbar.addAction(self.act_export)
        self._update_action_state(None)

    # -- state ---------------------------------------------------------------

    def current_filter(self) -> BetFilter:
        placed_from = placed_to = None
        if self.from_enabled.isChecked():
            first = _as_date(self.from_date.date().toPython())
            placed_from, _ = local_date_range(first, first)
        if self.to_enabled.isChecked():
            last = _as_date(self.to_date.date().toPython())
            _, placed_to = local_date_range(last, last)
        bet_type = self.type_filter.currentData()
        return BetFilter(
            statuses=self.status_filter.currentData(),
            bet_types=None if bet_type is None else frozenset({BetType(bet_type)}),
            bookmaker=self.bookmaker_filter.currentData(),
            placed_from=placed_from,
            placed_to=placed_to,
            text=self.search.text().strip() or None,
            needs_review=True if self.review_only.isChecked() else None,
            include_deleted=self.show_deleted.isChecked(),
        )

    def refresh(self, *_: object) -> None:
        """Data changed: reload the ledger, the summary and the bookmaker list."""
        self._refresh_bookmakers()
        self.refresh_summary()
        self.refresh_ledger()

    def refresh_summary(self) -> None:
        try:
            summary = self.repo.summary(self.clock())
        except (CoreError, StorageError) as exc:
            self._report(exc)
            return
        self.summary.show_summary(summary)
        self.bookmaker_table.show_summary(summary)

    def refresh_ledger(self, *_: object) -> None:
        self._search_timer.stop()
        selected = self.selected_bet_id()
        try:
            bets = self.repo.list_bets(self.current_filter())
        except (CoreError, StorageError) as exc:
            self._report(exc)
            return
        self.model.set_bets(bets)
        self.status_label.setText(f"{len(bets)} bet(s) shown")
        if selected is not None:
            self.select_bet(selected)
        else:
            self._selection_changed()

    def _refresh_bookmakers(self) -> None:
        current = self.bookmaker_filter.currentData()
        self.bookmaker_filter.blockSignals(True)
        try:
            self.bookmaker_filter.clear()
            self.bookmaker_filter.addItem("All bookmakers", None)
            for venue in self.repo.list_venues(VenueKind.BOOKMAKER):
                self.bookmaker_filter.addItem(venue.name, venue.name)
            index = self.bookmaker_filter.findData(current)
            self.bookmaker_filter.setCurrentIndex(max(index, 0))
        except StorageError as exc:
            self._report(exc)
        finally:
            self.bookmaker_filter.blockSignals(False)

    def selected_bet(self) -> BetRecord | None:
        indexes = self.table.selectionModel().selectedRows()
        if not indexes:
            return None
        source = self.proxy.mapToSource(indexes[0])
        return self.model.bet_at(source.row())

    def selected_bet_id(self) -> str | None:
        bet = self.selected_bet()
        return None if bet is None else bet.id

    def select_bet(self, bet_id: str) -> None:
        row = self.model.row_of(bet_id)
        if row is None:
            self.table.clearSelection()
            self._selection_changed()
            return
        index = self.proxy.mapFromSource(self.model.index(row, 0))
        self.table.selectRow(index.row())
        self.table.scrollTo(index)
        self._selection_changed()

    def _selection_changed(self, *_: object) -> None:
        bet = self.selected_bet()
        if bet is None:
            self.detail.clear()
        else:
            try:
                self.detail.show_bet(bet, self.repo.audit_trail(bet.id))
            except StorageError as exc:
                self._report(exc)
        self._update_action_state(bet)

    def _update_action_state(self, bet: BetRecord | None) -> None:
        if bet is None:
            for action in (
                self.act_clone,
                self.act_edit,
                self.act_settle,
                self.act_adjust,
                self.act_reopen,
                self.act_review,
                self.act_delete,
            ):
                action.setEnabled(False)
            self.act_delete.setText("&Delete")
            return
        live = not bet.is_deleted
        two_way = [r.leg.side.value for r in bet.legs] == ["BACK", "LAY"]
        self.act_clone.setEnabled(two_way)
        self.act_edit.setEnabled(live and two_way)
        self.act_settle.setEnabled(live and bet.status in OPEN_STATUSES)
        self.act_adjust.setEnabled(live and bet.is_settled)
        self.act_reopen.setEnabled(live and bet.status is not BetStatus.OPEN)
        self.act_review.setEnabled(live)
        self.act_delete.setEnabled(True)
        self.act_delete.setText("&Restore" if bet.is_deleted else "&Delete")

    # -- actions -------------------------------------------------------------

    def _message_box_error(self, text: str) -> None:
        QMessageBox.warning(self, "odds-pup", text)

    def _message_box_confirm(self, title: str, text: str) -> bool:
        answer = QMessageBox.question(self, title, text)
        return answer == QMessageBox.StandardButton.Yes

    def _report(self, exc: Exception) -> None:
        self.show_error(str(exc))

    def _changed(self, bet_id: str | None = None) -> None:
        self.refresh()
        if bet_id is not None:
            self.select_bet(bet_id)

    def _bet_dialog(
        self, mode: BetDialogMode = BetDialogMode.NEW, prefill: BetRecord | None = None
    ) -> BetDialog | None:
        try:
            parents = self.repo.list_bets(
                BetFilter(bet_types=frozenset({BetType.QUALIFYING}), limit=PARENT_CANDIDATES)
            )
            return BetDialog(
                venues=self.repo.list_venues(),
                offers=self.repo.list_offers(),
                parent_candidates=[b for b in parents if prefill is None or b.id != prefill.id],
                settings=self.settings,
                mode=mode,
                prefill=prefill,
                clock=self.clock,
                parent=self,
            )
        except (CoreError, StorageError) as exc:
            self._report(exc)
            return None

    def create_bet(self, new_bet: NewBet) -> BetRecord | None:
        try:
            created = self.repo.create_bet(new_bet)
        except (CoreError, StorageError) as exc:
            self._report(exc)
            return None
        self._changed(created.id)
        return created

    def new_bet(self) -> None:
        dialog = self._bet_dialog()
        if dialog is not None and dialog.exec() and dialog.result_value is not None:
            self.create_bet(dialog.result_value.bet)

    def clone_selected(self) -> None:
        bet = self.selected_bet()
        if bet is None:
            return
        dialog = self._bet_dialog(BetDialogMode.NEW, bet)
        if dialog is not None and dialog.exec() and dialog.result_value is not None:
            self.create_bet(dialog.result_value.bet)

    def apply_edit(self, bet: BetRecord, result: BetDialogResult) -> BetRecord | None:
        new_bet = result.bet
        changes: dict[str, object] = {
            "bet_type": new_bet.bet_type,
            "event_name": new_bet.event_name,
            "selection": new_bet.selection,
            "market": new_bet.market,
            "offer_id": new_bet.offer_id,
            "parent_bet_id": new_bet.parent_bet_id,
            "event_at": new_bet.event_at,
            "notes": new_bet.notes,
            "legs": new_bet.legs,
        }
        if new_bet.placed_at is not None:
            changes["placed_at"] = new_bet.placed_at
        try:
            if bet.status is BetStatus.OPEN:
                updated = self.repo.edit_bet(bet.id, **changes)
            else:
                if result.settled_at is not None and bet.is_settled:
                    changes["settled_at"] = result.settled_at
                updated = self.repo.correct_bet(bet.id, reason=result.reason or "", **changes)
            if new_bet.needs_review != updated.needs_review:
                updated = self.repo.set_needs_review(bet.id, new_bet.needs_review)
        except (CoreError, StorageError) as exc:
            self._report(exc)
            self.refresh()  # a partial success (edit saved, flag failed) must still show
            return None
        self._changed(updated.id)
        return updated

    def edit_selected(self) -> None:
        bet = self.selected_bet()
        if bet is None or bet.is_deleted:
            return
        mode = BetDialogMode.EDIT if bet.status is BetStatus.OPEN else BetDialogMode.CORRECT
        dialog = self._bet_dialog(mode, bet)
        if dialog is not None and dialog.exec() and dialog.result_value is not None:
            self.apply_edit(bet, dialog.result_value)

    def apply_settle(self, bet: BetRecord, request: SettleRequest) -> BetRecord | None:
        try:
            if request.outcome is not None:
                updated = self.repo.settle_two_way(
                    bet.id, request.outcome, settled_at=request.settled_at
                )
            else:
                updated = self.repo.settle(bet.id, request.per_leg, settled_at=request.settled_at)
        except (CoreError, StorageError) as exc:
            self._report(exc)
            return None
        self._changed(updated.id)
        return updated

    def settle_selected(self, outcome: TwoWayOutcome | None = None) -> None:
        bet = self.selected_bet()
        if bet is None:
            return
        if outcome is not None:
            self.apply_settle(bet, SettleRequest(outcome, {}, self.clock()))
            return
        dialog = SettleDialog(bet, clock=self.clock, parent=self)
        if dialog.exec() and dialog.result_value is not None:
            self.apply_settle(bet, dialog.result_value)

    def adjust_selected(self) -> None:
        bet = self.selected_bet()
        if bet is None:
            return
        dialog = AdjustDialog(bet, parent=self)
        if dialog.exec() and dialog.result_value is not None:
            try:
                self.repo.adjust(bet.id, dialog.result_value.override, dialog.result_value.reason)
            except (CoreError, StorageError) as exc:
                self._report(exc)
                return
            self._changed(bet.id)

    def reopen_selected(self) -> None:
        bet = self.selected_bet()
        if bet is None:
            return
        if not self.confirm(
            "Reopen bet",
            "Return every settled leg to pending? Realised figures and any override are cleared.",
        ):
            return
        try:
            self.repo.reopen(bet.id)
        except (CoreError, StorageError) as exc:
            self._report(exc)
            return
        self._changed(bet.id)

    def toggle_review(self) -> None:
        bet = self.selected_bet()
        if bet is None:
            return
        try:
            self.repo.set_needs_review(bet.id, not bet.needs_review)
        except (CoreError, StorageError) as exc:
            self._report(exc)
            return
        self._changed(bet.id)

    def delete_or_restore(self) -> None:
        bet = self.selected_bet()
        if bet is None:
            return
        try:
            if bet.is_deleted:
                self.repo.restore(bet.id)
            else:
                if not self.confirm(
                    "Delete bet",
                    "Hide this bet and exclude it from every total? It can be restored later.",
                ):
                    return
                self.repo.soft_delete(bet.id)
        except (CoreError, StorageError) as exc:
            self._report(exc)
            return
        self._changed(bet.id)

    def manage_venues(self) -> None:
        dialog = VenuesDialog(self.repo, show_error=self.show_error, parent=self)
        dialog.exec()
        if dialog.changed:
            self.refresh()

    def export_to(self, path: Path) -> int:
        count = export_csv(self.repo, path, self.current_filter())
        self.status_label.setText(f"Exported {count} leg row(s) to {path}")
        return count

    def export_csv(self) -> None:
        stamp = self.clock().strftime("%Y%m%d-%H%M%S")
        chosen, _ = QFileDialog.getSaveFileName(
            self, "Export CSV", f"odds-pup-export-{stamp}.csv", "CSV files (*.csv)"
        )
        if not chosen:
            return
        try:
            self.export_to(Path(chosen))
        except (CoreError, StorageError, OSError) as exc:
            self._report(exc)

    def backup_now(self) -> None:
        try:
            target = self.repo.backup()
        except (StorageError, OSError) as exc:
            self._report(exc)
            return
        self.status_label.setText(f"Backup written to {target}")

    def about(self) -> None:
        where = "" if self.paths is None else f"\n\nData folder: {self.paths.root}"
        QMessageBox.about(
            self,
            "About odds-pup",
            f"odds-pup {__version__}\nA matched betting ledger. Your data stays on this machine; "
            f"the app never uses the network.{where}",
        )

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        self.settings.save_geometry(self.saveGeometry())
        self.settings.save_window_state(self.saveState())
        self.settings.sync()
        super().closeEvent(event)
