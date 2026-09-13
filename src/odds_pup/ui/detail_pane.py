"""Legs and audit trail of the selected bet. SPEC §13 detail pane."""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from odds_pup.core import LegResult, format_commission, format_odds, format_pence
from odds_pup.storage import OPEN_STATUSES
from odds_pup.ui.format import (
    ENUM_LABELS,
    RESULT_LABELS,
    STATUS_LABELS,
    TYPE_LABELS,
    audit_field_text,
    audit_kind_text,
    audit_value_text,
    local_datetime_text,
    pl_text,
)

if TYPE_CHECKING:
    from odds_pup.storage import AuditRow, BetRecord

LEG_HEADERS = ("Side", "Venue", "Odds", "Stake", "Kind", "Commission", "Liability", "Result", "Net")
AUDIT_HEADERS = ("When", "Action", "Field", "From", "To", "Reason")


def _table(headers: tuple[str, ...], name: str) -> QTableWidget:
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(list(headers))
    table.verticalHeader().setVisible(False)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    table.horizontalHeader().setStretchLastSection(True)
    table.setAccessibleName(name)
    return table


def _cell(text: str, *, right: bool = False) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    if right:
        item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    return item


class DetailPane(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.header = QLabel("Select a bet to see its legs and history.")
        self.header.setWordWrap(True)
        self.header.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.header.setAccessibleName("Bet summary")
        self.legs = _table(LEG_HEADERS, "Legs of the selected bet")
        self.audit = _table(AUDIT_HEADERS, "History of the selected bet")
        layout.addWidget(self.header)
        layout.addWidget(QLabel("Legs"))
        layout.addWidget(self.legs, 1)
        layout.addWidget(QLabel("History"))
        layout.addWidget(self.audit, 1)

    def clear(self) -> None:
        self.header.setText("Select a bet to see its legs and history.")
        self.legs.setRowCount(0)
        self.audit.setRowCount(0)

    def show_bet(self, bet: BetRecord, trail: list[AuditRow]) -> None:
        bits = [
            f"<b>{bet.event_name}</b>",
            f"{TYPE_LABELS[bet.bet_type]} · {STATUS_LABELS[bet.status]}",
            f"Placed {local_datetime_text(bet.placed_at)}",
        ]
        if bet.settled_at is not None:
            bits.append(f"Settled {local_datetime_text(bet.settled_at)}")
        bits.append(f"Expected {pl_text(bet.expected_pl_pence)}")
        if bet.status in OPEN_STATUSES:
            bits.append(f"Guaranteed now {pl_text(bet.live_guaranteed_pl)}")
        if bet.actual_pl_pence is not None:
            bits.append(f"Actual {pl_text(bet.actual_pl_pence)}")
        if bet.actual_pl_override_pence is not None:
            bits.append(f"<b>Adjusted to {pl_text(bet.actual_pl_override_pence)}</b>")
        if bet.needs_review:
            bits.append("Needs review")
        if bet.is_deleted:
            bits.append("<b>Deleted</b>")
        if bet.notes:
            bits.append(bet.notes.replace("\n", "<br>"))
        self.header.setText("<br>".join(bits))

        self.legs.setRowCount(len(bet.legs))
        for row, record in enumerate(bet.legs):
            leg = record.leg
            net = "" if leg.settled_amount is None else pl_text(leg.settled_amount)
            values = (
                ENUM_LABELS[leg.side.value],
                leg.venue,
                leg.odds_text or format_odds(leg.odds),
                format_pence(leg.stake),
                ENUM_LABELS[leg.stake_kind.value],
                format_commission(leg.commission_bp),
                format_pence(leg.liability),
                RESULT_LABELS[leg.result],
                net,
            )
            for col, value in enumerate(values):
                self.legs.setItem(row, col, _cell(value, right=col in (2, 3, 5, 6, 8)))
            result_cell = self.legs.item(row, 7)
            if leg.result is LegResult.PENDING and result_cell is not None:
                result_cell.setToolTip("Awaiting result")

        self.audit.setRowCount(len(trail))
        for row, entry in enumerate(reversed(trail)):
            cells = (
                local_datetime_text(entry.at),
                audit_kind_text(entry.kind),
                audit_field_text(entry.field),
                audit_value_text(entry.field, entry.old_value),
                audit_value_text(entry.field, entry.new_value),
                entry.reason or "",
            )
            for col, value in enumerate(cells):
                self.audit.setItem(row, col, _cell(value))
