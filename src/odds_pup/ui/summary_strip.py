"""The summary strip: realised, this month, open guaranteed, open liability. SPEC §13."""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from odds_pup.core import format_pence
from odds_pup.ui.format import pl_text
from odds_pup.ui.ledger_model import pl_colour

if TYPE_CHECKING:
    from odds_pup.storage import Summary


class StatTile(QFrame):
    """A caption and a value. Sizes derive from the application font so system scaling applies."""

    def __init__(self, caption: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        base = QApplication.font()
        self.caption_text = caption
        self.caption = QLabel(caption)
        caption_font = QFont(base)
        caption_font.setPointSizeF(base.pointSizeF() * 0.85)
        self.caption.setFont(caption_font)
        self.value = QLabel("—")
        value_font = QFont(base)
        value_font.setPointSizeF(base.pointSizeF() * 1.5)
        value_font.setBold(True)
        self.value.setFont(value_font)
        self.value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.caption)
        layout.addWidget(self.value)
        self._set("—", None)

    def _set(self, text: str, colour: QColor | None) -> None:
        self.value.setText(text)
        palette = self.value.palette()
        palette.setColor(
            QPalette.ColorRole.WindowText,
            colour if colour is not None else self.palette().color(QPalette.ColorRole.WindowText),
        )
        self.value.setPalette(palette)
        self.value.setAccessibleName(f"{self.caption_text}: {text}")

    def show_pl(self, amount: int) -> None:
        self._set(pl_text(amount), pl_colour(amount))

    def show_text(self, text: str) -> None:
        self._set(text, None)


class SummaryStrip(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.all_time = StatTile("Realised, all time")
        self.this_month = StatTile("Realised, this month")
        self.open_guaranteed = StatTile("Open positions, guaranteed")
        self.open_liability = StatTile("Open liability")
        self.counts = StatTile("Bets")
        for tile in (
            self.all_time,
            self.this_month,
            self.open_guaranteed,
            self.open_liability,
            self.counts,
        ):
            layout.addWidget(tile)

    def show_summary(self, summary: Summary) -> None:
        self.all_time.show_pl(summary.realised_all_time)
        self.this_month.show_pl(summary.realised_this_month)
        self.open_guaranteed.show_pl(summary.open_guaranteed)
        total_liability = sum(summary.open_liability_by_exchange.values())
        self.open_liability.show_text(format_pence(total_liability))
        per_exchange = "\n".join(
            f"{name}: {format_pence(amount)}"
            for name, amount in summary.open_liability_by_exchange.items()
        )
        self.open_liability.setToolTip(per_exchange or "No open lays")
        self.counts.show_text(f"{summary.open_count} open · {summary.settled_count} settled")


class BookmakerTable(QTableWidget):
    """Realised P/L per bookmaker (first back leg's venue)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(0, 2, parent)
        self.setHorizontalHeaderLabels(["Bookmaker", "Realised"])
        self.verticalHeader().setVisible(False)
        self.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.horizontalHeader().setStretchLastSection(True)
        self.setAccessibleName("Realised profit per bookmaker")
        self.setTabKeyNavigation(False)

    def show_summary(self, summary: Summary) -> None:
        rows = sorted(summary.realised_by_bookmaker.items(), key=lambda kv: kv[1])
        self.setRowCount(len(rows))
        for row, (name, amount) in enumerate(rows):
            self.setItem(row, 0, QTableWidgetItem(name))
            item = QTableWidgetItem(pl_text(amount))
            item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            colour = pl_colour(amount)
            if colour is not None:
                item.setForeground(colour)
            self.setItem(row, 1, item)
        self.resizeColumnToContents(0)
