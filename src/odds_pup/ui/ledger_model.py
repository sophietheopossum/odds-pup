"""Table model over ledger rows. SPEC §13 columns; numeric sorting via a sort role."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QPersistentModelIndex, Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

from odds_pup.core import Leg, format_odds, format_pence
from odds_pup.ui.format import STATUS_LABELS, TYPE_SHORT, local_datetime_text, pl_text

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from odds_pup.storage import BetRecord

SORT_ROLE = Qt.ItemDataRole.UserRole + 1
BET_ID_ROLE = Qt.ItemDataRole.UserRole + 2

_ModelIndex = QModelIndex | QPersistentModelIndex


@dataclass(frozen=True, slots=True)
class Column:
    title: str
    text: Callable[[BetRecord], str]
    sort_key: Callable[[BetRecord], Any]
    numeric: bool = False
    pl: bool = False


def _back(bet: BetRecord) -> Leg | None:
    backs = bet.back_legs
    return backs[0].leg if backs else None


def _lay(bet: BetRecord) -> Leg | None:
    lays = bet.lay_legs
    return lays[0].leg if lays else None


def _stake_text(leg: Leg | None) -> str:
    return "" if leg is None else format_pence(leg.stake)


def _stake_key(leg: Leg | None) -> int:
    return -1 if leg is None else leg.stake


def _odds_text(leg: Leg | None) -> str:
    return "" if leg is None else format_odds(leg.odds)


def _odds_key(leg: Leg | None) -> Decimal:
    return Decimal(0) if leg is None else leg.odds


def _flags(bet: BetRecord) -> str:
    parts = []
    if bet.has_adjustments:
        parts.append("adjusted")
    if bet.needs_review:
        parts.append("review")
    if bet.is_deleted:
        parts.append("deleted")
    return ", ".join(parts)


COLUMNS: tuple[Column, ...] = (
    Column("Placed", lambda b: local_datetime_text(b.placed_at), lambda b: b.placed_at),
    Column("Event", lambda b: b.event_name, lambda b: b.event_name.casefold()),
    Column("Selection", lambda b: b.selection, lambda b: b.selection.casefold()),
    Column("Type", lambda b: TYPE_SHORT[b.bet_type], lambda b: TYPE_SHORT[b.bet_type]),
    Column("Bookmaker", lambda b: b.bookmaker or "", lambda b: (b.bookmaker or "").casefold()),
    Column("Exchange", lambda b: b.exchange or "", lambda b: (b.exchange or "").casefold()),
    Column(
        "Back stake", lambda b: _stake_text(_back(b)), lambda b: _stake_key(_back(b)), numeric=True
    ),
    Column(
        "Back odds", lambda b: _odds_text(_back(b)), lambda b: _odds_key(_back(b)), numeric=True
    ),
    Column(
        "Lay stake", lambda b: _stake_text(_lay(b)), lambda b: _stake_key(_lay(b)), numeric=True
    ),
    Column("Lay odds", lambda b: _odds_text(_lay(b)), lambda b: _odds_key(_lay(b)), numeric=True),
    Column(
        "Expected",
        lambda b: pl_text(b.expected_pl_pence),
        lambda b: b.expected_pl_pence,
        numeric=True,
        pl=True,
    ),
    Column(
        "Actual",
        lambda b: pl_text(b.effective_actual_pl),
        lambda b: b.effective_actual_pl if b.effective_actual_pl is not None else -(10**15),
        numeric=True,
        pl=True,
    ),
    Column("Status", lambda b: STATUS_LABELS[b.status], lambda b: STATUS_LABELS[b.status]),
    Column("Flags", _flags, _flags),
)
COLUMN_INDEX = {c.title: i for i, c in enumerate(COLUMNS)}


def pl_colour(amount: int | None, palette: QPalette | None = None) -> QColor | None:
    """A secondary hint only: the sign is already in the text (SPEC §13 accessibility)."""
    if amount is None or amount == 0:
        return None
    base = palette or QApplication.palette()
    dark = base.color(QPalette.ColorRole.Window).lightness() < 128
    if amount > 0:
        return QColor("#6fcf97") if dark else QColor("#1b7f3b")
    return QColor("#f28b82") if dark else QColor("#b3261e")


class LedgerModel(QAbstractTableModel):
    def __init__(self, bets: Sequence[BetRecord] = ()) -> None:
        super().__init__()
        self._bets: list[BetRecord] = list(bets)

    # -- data access -------------------------------------------------------

    def set_bets(self, bets: Sequence[BetRecord]) -> None:
        self.beginResetModel()
        self._bets = list(bets)
        self.endResetModel()

    def bet_at(self, row: int) -> BetRecord:
        return self._bets[row]

    def row_of(self, bet_id: str) -> int | None:
        for row, bet in enumerate(self._bets):
            if bet.id == bet_id:
                return row
        return None

    @property
    def bets(self) -> tuple[BetRecord, ...]:
        return tuple(self._bets)

    # -- QAbstractTableModel -------------------------------------------------

    def rowCount(self, parent: _ModelIndex = QModelIndex()) -> int:  # noqa: B008, N802
        return 0 if parent.isValid() else len(self._bets)

    def columnCount(self, parent: _ModelIndex = QModelIndex()) -> int:  # noqa: B008, N802
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(  # noqa: N802
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            return COLUMNS[section].title
        return section + 1

    def data(self, index: _ModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        bet = self._bets[index.row()]
        column = COLUMNS[index.column()]
        if role == Qt.ItemDataRole.DisplayRole:
            return column.text(bet)
        if role == SORT_ROLE:
            return column.sort_key(bet)
        if role == BET_ID_ROLE:
            return bet.id
        if role == Qt.ItemDataRole.TextAlignmentRole and column.numeric:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.ForegroundRole and column.pl:
            amount = (
                bet.expected_pl_pence if column.title == "Expected" else bet.effective_actual_pl
            )
            return pl_colour(amount)
        if role == Qt.ItemDataRole.ToolTipRole:
            return self._tooltip(bet)
        return None

    @staticmethod
    def _tooltip(bet: BetRecord) -> str:
        lines = [bet.event_name, f"Guaranteed now: {pl_text(bet.live_guaranteed_pl)}"]
        if bet.notes:
            lines.append(bet.notes)
        return "\n".join(lines)
