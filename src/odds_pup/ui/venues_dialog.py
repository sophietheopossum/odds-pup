"""Manage bookmakers and exchanges: rename typos, set real commission, delete unused. SPEC §3.4."""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from odds_pup.core import (
    LADDERS,
    CoreError,
    VenueKind,
    format_commission,
    format_pence,
    parse_commission,
    parse_money,
)
from odds_pup.storage import StorageError, Venue

if TYPE_CHECKING:
    from collections.abc import Callable

    from odds_pup.storage import Repository

HEADERS = ("Name", "Kind", "Default commission", "Minimum lay stake", "Price ladder", "Used by")


class VenueEditor(QDialog):
    """Add or edit one venue."""

    def __init__(self, venue: Venue | None, *, in_use: bool, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit venue" if venue else "Add venue")
        form = QFormLayout(self)
        self.name = QLineEdit(venue.name if venue else "")
        self.name.setAccessibleName("Venue name")
        self.kind = QComboBox()
        self.kind.addItem("Bookmaker", VenueKind.BOOKMAKER)
        self.kind.addItem("Exchange", VenueKind.EXCHANGE)
        self.kind.setAccessibleName("Venue kind")
        if venue is not None:
            self.kind.setCurrentIndex(self.kind.findData(venue.kind))
        self.kind.setEnabled(not in_use)
        if in_use:
            self.kind.setToolTip("Bets already use this venue, so its kind is fixed.")
        self.commission = QLineEdit(
            format_commission(venue.default_commission_bp).rstrip("%") if venue else "0"
        )
        self.commission.setAccessibleName("Default commission percent")
        self.min_stake = QLineEdit(
            ""
            if venue is None or venue.min_stake_pence is None
            else format_pence(venue.min_stake_pence).lstrip("£")
        )
        self.min_stake.setPlaceholderText("none")
        self.min_stake.setAccessibleName("Minimum lay stake in pounds")
        self.ladder = QComboBox()
        self.ladder.addItem("(none)", None)
        for name in sorted(LADDERS):
            self.ladder.addItem(name.title(), name)
        self.ladder.setAccessibleName("Price ladder")
        if venue is not None and venue.ladder is not None:
            self.ladder.setCurrentIndex(self.ladder.findData(venue.ladder))
        form.addRow("&Name", self.name)
        form.addRow("&Kind", self.kind)
        form.addRow("&Commission %", self.commission)
        form.addRow("&Minimum lay stake £", self.min_stake)
        form.addRow("Price &ladder", self.ladder)
        self.error = QLabel("")
        self.error.setWordWrap(True)
        form.addRow(self.error)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self.values: dict[str, object] | None = None

    def read(self) -> dict[str, object]:
        name = self.name.text().strip()
        if not name:
            raise CoreError("enter a name")
        min_text = self.min_stake.text().strip()
        return {
            "name": name,
            "kind": VenueKind(self.kind.currentData()),
            "default_commission_bp": parse_commission(self.commission.text() or "0"),
            "min_stake_pence": None if not min_text else parse_money(min_text),
            "ladder": self.ladder.currentData(),
        }

    def _accept(self) -> None:
        try:
            self.values = self.read()
        except CoreError as exc:
            self.error.setText(str(exc))
            return
        self.accept()


class VenuesDialog(QDialog):
    def __init__(
        self,
        repo: Repository,
        *,
        show_error: Callable[[str], None] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.repo = repo
        self.status = QLabel("")
        self.show_error = show_error or self.status.setText
        self.changed = False
        self.setWindowTitle("Bookmakers and exchanges")
        self.resize(760, 520)
        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "Set each exchange's commission to what your account actually pays; the New Bet "
                "form pre-fills it. Renaming fixes a typo on every bet that uses the venue."
            )
        )
        self.table = QTableWidget(0, len(HEADERS))
        self.table.setHorizontalHeaderLabels(list(HEADERS))
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setAccessibleName("Venues")
        self.table.setTabKeyNavigation(False)
        layout.addWidget(self.table, 1)
        row = QHBoxLayout()
        self.add_button = QPushButton("&Add…")
        self.edit_button = QPushButton("&Edit…")
        self.delete_button = QPushButton("&Delete")
        for button in (self.add_button, self.edit_button, self.delete_button):
            row.addWidget(button)
        row.addStretch(1)
        for button in (self.add_button, self.edit_button, self.delete_button):
            button.setAutoDefault(False)
        close = QPushButton("Close")
        row.addWidget(close)
        layout.addLayout(row)
        layout.addWidget(self.status)
        self.add_button.clicked.connect(self.add_venue)
        self.edit_button.clicked.connect(self.edit_selected)
        self.delete_button.clicked.connect(self.delete_selected)
        close.clicked.connect(self.accept)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self.table.activated.connect(lambda _index: self.edit_selected())  # Enter or double-click
        self._venues: list[Venue] = []
        self._usage: dict[str, int] = {}
        close.setDefault(True)
        self.reload()

    def reload(self, select_id: str | None = None) -> None:
        self._venues = self.repo.list_venues()
        self._usage = {v.id: self.repo.venue_usage(v.id) for v in self._venues}
        self.table.setRowCount(len(self._venues))
        for index, venue in enumerate(self._venues):
            values = (
                venue.name,
                venue.kind.value.title(),
                format_commission(venue.default_commission_bp),
                "" if venue.min_stake_pence is None else format_pence(venue.min_stake_pence),
                (venue.ladder or "").title(),
                str(self._usage[venue.id]),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, venue.id)
                self.table.setItem(index, column, item)
            if venue.id == select_id:
                self.table.selectRow(index)
        self._update_buttons()

    def selected(self) -> Venue | None:
        rows = self.table.selectionModel().selectedRows()
        return self._venues[rows[0].row()] if rows else None

    def _update_buttons(self) -> None:
        venue = self.selected()
        self.edit_button.setEnabled(venue is not None)
        self.delete_button.setEnabled(venue is not None and self._usage.get(venue.id, 1) == 0)

    def _run_editor(self, venue: Venue | None) -> dict[str, object] | None:
        editor = VenueEditor(venue, in_use=bool(venue and self._usage.get(venue.id)), parent=self)
        return editor.values if editor.exec() else None

    def add_venue(self) -> None:
        values = self._run_editor(None)
        if values is not None:
            self.apply_add(values)

    def apply_add(self, values: dict[str, object]) -> Venue | None:
        try:
            venue = self.repo.ensure_venue(
                str(values["name"]),
                VenueKind(str(values["kind"])),
                default_commission_bp=int(values["default_commission_bp"]),  # type: ignore[call-overload]
                min_stake_pence=values["min_stake_pence"],  # type: ignore[arg-type]
                ladder=values["ladder"],  # type: ignore[arg-type]
            )
        except (CoreError, StorageError) as exc:
            self.show_error(str(exc))
            return None
        self.changed = True
        self.reload(venue.id)
        return venue

    def edit_selected(self) -> None:
        venue = self.selected()
        if venue is None:
            return
        values = self._run_editor(venue)
        if values is not None:
            self.apply_edit(venue, values)

    def apply_edit(self, venue: Venue, values: dict[str, object]) -> Venue | None:
        try:
            updated = self.repo.update_venue(venue.id, **values)  # type: ignore[arg-type]
        except (CoreError, StorageError) as exc:
            self.show_error(str(exc))
            return None
        self.changed = True
        self.reload(updated.id)
        return updated

    def delete_selected(self) -> None:
        venue = self.selected()
        if venue is None:
            return
        try:
            self.repo.delete_venue(venue.id)
        except (CoreError, StorageError) as exc:
            self.show_error(str(exc))
            return
        self.changed = True
        self.reload()
