"""New/Edit/Correct bet, Settle and Adjust dialogs. SPEC §8.3, §8.4, §13."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QCompleter,
    QDateTimeEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from odds_pup.core import (
    LADDERS,
    BetType,
    CoreError,
    Leg,
    LegResult,
    Pence,
    Side,
    StakeKind,
    TwoWayOutcome,
    TwoWayPosition,
    ValidationError,
    VenueKind,
    advisories_for,
    format_commission,
    format_odds,
    format_pence,
    parse_commission,
    parse_money,
    parse_odds,
    settle_two_way,
    two_way_position,
)
from odds_pup.storage import BetRecord, NewBet, NewLeg, Offer, StorageError, Venue, utc_now
from odds_pup.ui.format import (
    RESULT_LABELS,
    TYPE_LABELS,
    from_qdatetime,
    local_date_text,
    pl_text,
    to_qdatetime,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from datetime import datetime
    from decimal import Decimal

    from odds_pup.ui.settings import UiSettings

TWO_WAY = (BetType.QUALIFYING, BetType.FREE_BET_SNR, BetType.FREE_BET_SR)
OUTCOME_LABELS: dict[TwoWayOutcome, str] = {
    TwoWayOutcome.BACK_WON: "Back won",
    TwoWayOutcome.LAY_WON: "Lay won (selection lost)",
    TwoWayOutcome.VOID: "Void (both legs)",
}


class BetDialogMode(StrEnum):
    NEW = "NEW"
    EDIT = "EDIT"
    CORRECT = "CORRECT"


def _line(name: str, placeholder: str = "") -> QLineEdit:
    edit = QLineEdit()
    edit.setAccessibleName(name)
    edit.setPlaceholderText(placeholder)
    edit.setClearButtonEnabled(True)
    return edit


def _venue_combo(name: str, venues: Sequence[Venue]) -> QComboBox:
    combo = QComboBox()
    combo.setEditable(True)
    combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
    combo.addItems([v.name for v in venues])
    combo.setCurrentText("")
    completer = QCompleter([v.name for v in venues])
    completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
    completer.setFilterMode(Qt.MatchFlag.MatchContains)
    combo.setCompleter(completer)
    combo.setAccessibleName(name)
    return combo


def _datetime_edit(name: str, moment: datetime) -> QDateTimeEdit:
    edit = QDateTimeEdit(to_qdatetime(moment))
    edit.setCalendarPopup(True)
    edit.setDisplayFormat("dd/MM/yyyy HH:mm")
    edit.setAccessibleName(name)
    return edit


def _odds_text_if_special(text: str, odds: Decimal) -> str | None:
    """Keep what the user typed when it is a fraction or keyword (SPEC §3.3 odds_text)."""
    cleaned = text.strip()
    return None if cleaned == format_odds(odds) or cleaned == str(odds) else cleaned


# ------------------------------------------------------------------ bet dialog


@dataclass(frozen=True, slots=True)
class BetDialogResult:
    bet: NewBet
    reason: str | None = None
    settled_at: datetime | None = None
    """Corrected settlement time (CORRECT mode on a settled bet only)."""


class BetDialog(QDialog):
    """New, Edit or Correct a one-back-one-lay bet with the live calculator (SPEC §13)."""

    def __init__(
        self,
        *,
        venues: Sequence[Venue],
        offers: Sequence[Offer] = (),
        parent_candidates: Sequence[BetRecord] = (),
        settings: UiSettings | None = None,
        mode: BetDialogMode = BetDialogMode.NEW,
        prefill: BetRecord | None = None,
        clock: Callable[[], datetime] = utc_now,
        save: Callable[[BetDialogResult], None] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.mode = mode
        self._save = save
        self._prefilled = prefill is not None
        self._venues = {v.name.casefold(): v for v in venues}
        self._offers = list(offers)
        self._parents = list(parent_candidates)
        self._settings = settings
        self._clock = clock
        self._position: TwoWayPosition | None = None
        self._has_settled_at = False
        self._lay_auto = True
        self.result_value: BetDialogResult | None = None
        self.setWindowTitle(
            {"NEW": "New bet", "EDIT": "Edit bet", "CORRECT": "Correct bet"}[mode.value]
        )
        self._build(venues)
        if prefill is not None:
            self._apply_prefill(prefill)
        elif settings is not None:
            self._apply_settings(settings)
        self._recalculate()

    # -- construction --------------------------------------------------------

    def _build(self, venues: Sequence[Venue]) -> None:
        outer = QVBoxLayout(self)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

        self.type_combo = QComboBox()
        for bet_type in TWO_WAY:
            self.type_combo.addItem(TYPE_LABELS[bet_type], bet_type)
        self.type_combo.setAccessibleName("Bet type")
        form.addRow("&Type", self.type_combo)

        self.event_name = _line("Event", "Arsenal v Chelsea")
        self.selection = _line("Selection", "Arsenal")
        self.market = _line("Market", "Match Odds")
        form.addRow("&Event", self.event_name)
        form.addRow("Se&lection", self.selection)
        form.addRow("&Market", self.market)

        bookmakers = [v for v in venues if v.kind is VenueKind.BOOKMAKER]
        exchanges = [v for v in venues if v.kind is VenueKind.EXCHANGE]
        self.bookmaker = _venue_combo("Bookmaker", bookmakers)
        self.back_stake = _line("Back stake", "10.00")
        self.back_odds = _line("Back odds", "2.00 or 11/8")
        form.addRow("&Bookmaker", self.bookmaker)
        form.addRow("Back &stake £", self.back_stake)
        form.addRow("Back &odds", self.back_odds)

        self.exchange = _venue_combo("Exchange", exchanges)
        self.commission = _line("Commission %", "e.g. 2 (0 if none)")
        self.lay_odds = _line("Lay odds", "2.10")
        lay_row = QHBoxLayout()
        self.lay_stake = _line("Lay stake", "")
        self.use_suggested = QPushButton("Use suggested")
        self.use_suggested.setAutoDefault(False)
        self.use_suggested.setToolTip(
            "Replace the lay stake with the one that equalises both outcomes"
        )
        lay_row.addWidget(self.lay_stake, 1)
        lay_row.addWidget(self.use_suggested)
        form.addRow("E&xchange", self.exchange)
        form.addRow("&Commission %", self.commission)
        form.addRow("Lay o&dds", self.lay_odds)
        lay_label = QLabel("Lay sta&ke £")
        lay_label.setBuddy(self.lay_stake)
        form.addRow(lay_label, lay_row)

        self.offer = QComboBox()
        self.offer.addItem("(none)", None)
        for offer in self._offers:
            self.offer.addItem(offer.title, offer.id)
        self.offer.setAccessibleName("Offer")
        self.parent_bet = QComboBox()
        self.parent_bet.addItem("(none)", None)
        for candidate in self._parents:
            label = f"{local_date_text(candidate.placed_at)}  {candidate.event_name}"
            if candidate.selection:
                label += f" — {candidate.selection}"
            self.parent_bet.addItem(label, candidate.id)
        self.parent_bet.setAccessibleName("Qualifying bet this free bet came from")
        form.addRow("O&ffer", self.offer)
        form.addRow("&Parent bet", self.parent_bet)

        now = self._clock()
        self.placed_at = _datetime_edit("Placed at", now)
        event_row = QHBoxLayout()
        self.has_event_at = QCheckBox("Event starts")
        self.has_event_at.setAccessibleName("Record the event start time")
        self.event_at = _datetime_edit("Event start", now)
        self.event_at.setEnabled(False)
        event_row.addWidget(self.has_event_at)
        event_row.addWidget(self.event_at, 1)
        form.addRow("Pl&aced", self.placed_at)
        form.addRow("", event_row)

        self.notes = QPlainTextEdit()
        self.notes.setAccessibleName("Notes")
        self.notes.setPlaceholderText("Notes (never passwords)")
        self.notes.setTabChangesFocus(True)
        line = self.notes.fontMetrics().lineSpacing()
        margins = self.notes.contentsMargins()
        self.notes.setFixedHeight(
            3 * line
            + int(2 * self.notes.document().documentMargin())
            + margins.top()
            + margins.bottom()
            + 2 * self.notes.frameWidth()
        )
        form.addRow("&Notes", self.notes)
        self.needs_review = QCheckBox("Needs review")
        self.needs_review.setAccessibleName("Flag this bet for review")
        form.addRow("", self.needs_review)

        self.reason = _line("Reason for the correction", "what was wrong and why")
        self.corrected_settled_at = _datetime_edit("Settled at", now)
        if self.mode is BetDialogMode.CORRECT:
            form.addRow("&Reason", self.reason)
            form.addRow("Se&ttled at", self.corrected_settled_at)
            form.setRowVisible(self.corrected_settled_at, False)
        self._form = form

        outer.addLayout(form)

        # backfill
        self.backfill = QGroupBox("Already settled (backfill)")
        self.backfill.setCheckable(True)
        self.backfill.setChecked(False)
        self.backfill.setAccessibleName("This bet has already been settled")
        backfill_form = QFormLayout(self.backfill)
        self.backfill_outcome = QComboBox()
        for outcome, label in OUTCOME_LABELS.items():
            self.backfill_outcome.addItem(label, outcome)
        self.backfill_outcome.setAccessibleName("Outcome")
        self.settled_at = _datetime_edit("Settled at", now)
        backfill_form.addRow("Outcome", self.backfill_outcome)
        backfill_form.addRow("Settled at", self.settled_at)
        outer.addWidget(self.backfill)
        self.backfill.setVisible(self.mode is BetDialogMode.NEW)

        # live panel
        panel = QGroupBox("Position")
        grid = QGridLayout(panel)
        self.liability_label = QLabel("—")
        self.back_wins_label = QLabel("—")
        self.lay_wins_label = QLabel("—")
        self.guaranteed_label = QLabel("—")
        self.rating_label = QLabel("—")
        figures: tuple[tuple[str, QLabel], ...] = (
            ("Liability", self.liability_label),
            ("If back wins", self.back_wins_label),
            ("If lay wins", self.lay_wins_label),
            ("Guaranteed", self.guaranteed_label),
            ("Rating", self.rating_label),
        )
        self._captions: dict[int, str] = {}
        for row, (caption, figure) in enumerate(figures):
            grid.addWidget(QLabel(caption), row, 0)
            grid.addWidget(figure, row, 1)
            self._captions[id(figure)] = caption
            figure.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.warnings_label = QLabel("")
        self.warnings_label.setWordWrap(True)
        grid.addWidget(self.warnings_label, 5, 0, 1, 2)
        outer.addWidget(panel)

        self.error_label = QLabel("")
        self.error_label.setWordWrap(True)
        outer.addWidget(self.error_label)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        self.save_button = self.buttons.button(QDialogButtonBox.StandardButton.Save)
        outer.addWidget(self.buttons)
        # Register Save as the default only once it belongs to the dialog; otherwise Qt picks
        # the first auto-default button (Use suggested) and Enter overwrites the lay stake.
        self.save_button.setDefault(True)
        cancel = self.buttons.button(QDialogButtonBox.StandardButton.Cancel)
        cancel.setAutoDefault(False)

        # wiring
        for widget in (
            self.back_stake,
            self.back_odds,
            self.commission,
            self.lay_odds,
            self.event_name,
            self.reason,
        ):
            widget.textChanged.connect(self._recalculate)
        self.lay_stake.textEdited.connect(self._lay_edited)
        self.type_combo.currentIndexChanged.connect(self._recalculate)
        self.bookmaker.currentTextChanged.connect(self._recalculate)
        self.exchange.currentTextChanged.connect(self._exchange_changed)
        self.use_suggested.clicked.connect(self._use_suggested)
        self.has_event_at.toggled.connect(self.event_at.setEnabled)
        self.backfill.toggled.connect(self._recalculate)
        self.buttons.accepted.connect(self._accept)
        self.buttons.rejected.connect(self.reject)

    def _apply_settings(self, settings: UiSettings) -> None:
        """Prefill the last venues used, but only while they still exist (they may be renamed).

        The commission comes from the exchange's current default (set in Settings), never from
        the last bet: a remembered rate would silently outlive a changed default.
        """
        bookmaker = self._venues.get(settings.last_bookmaker.strip().casefold())
        if bookmaker is not None:
            self.bookmaker.setCurrentText(bookmaker.name)
        exchange = self._venues.get(settings.last_exchange.strip().casefold())
        if exchange is not None and exchange.kind is VenueKind.EXCHANGE:
            self.exchange.setCurrentText(exchange.name)
            self.commission.setText(format_commission(exchange.default_commission_bp).rstrip("%"))

    def _apply_prefill(self, bet: BetRecord) -> None:
        backs, lays = bet.back_legs, bet.lay_legs
        if len(bet.legs) != 2 or not backs or not lays:
            raise ValidationError("this dialog edits one-back-one-lay bets only")
        back, lay = backs[0].leg, lays[0].leg
        index = self.type_combo.findData(bet.bet_type)
        if index < 0:
            raise ValidationError(f"{bet.bet_type} cannot be edited in this dialog")
        self.type_combo.setCurrentIndex(index)
        self.event_name.setText(bet.event_name)
        self.selection.setText(bet.selection)
        self.market.setText(bet.market)
        self.bookmaker.setCurrentText(back.venue)
        self.back_stake.setText(str(format_pence(back.stake)).lstrip("£"))
        self.back_odds.setText(back.odds_text or format_odds(back.odds))
        self.exchange.setCurrentText(lay.venue)
        self.commission.setText(format_commission(lay.commission_bp).rstrip("%"))
        self.lay_odds.setText(lay.odds_text or format_odds(lay.odds))
        self.lay_stake.setText(format_pence(lay.stake).lstrip("£"))
        self._lay_auto = False
        if bet.offer_id is not None and (index := self.offer.findData(bet.offer_id)) >= 0:
            self.offer.setCurrentIndex(index)
        if bet.parent_bet_id is not None:
            index = self.parent_bet.findData(bet.parent_bet_id)
            if index < 0:  # not among the recent qualifiers: keep the link rather than drop it
                self.parent_bet.addItem("(current parent)", bet.parent_bet_id)
                index = self.parent_bet.count() - 1
            self.parent_bet.setCurrentIndex(index)
        if self.mode is BetDialogMode.NEW:  # clone: fresh timestamp
            self.placed_at.setDateTime(to_qdatetime(self._clock()))
        else:
            self.placed_at.setDateTime(to_qdatetime(bet.placed_at))
        if bet.event_at is not None:
            self.has_event_at.setChecked(True)
            self.event_at.setDateTime(to_qdatetime(bet.event_at))
        self.notes.setPlainText(bet.notes)
        self.needs_review.setChecked(bet.needs_review)
        if self.mode is BetDialogMode.CORRECT and bet.settled_at is not None:
            self.corrected_settled_at.setDateTime(to_qdatetime(bet.settled_at))
            self._form.setRowVisible(self.corrected_settled_at, True)
            self._has_settled_at = True

    # -- behaviour -----------------------------------------------------------

    def _exchange_changed(self, name: str) -> None:
        venue = self._venues.get(name.strip().casefold())
        if venue is not None and venue.kind is VenueKind.EXCHANGE:
            self.commission.setText(format_commission(venue.default_commission_bp).rstrip("%"))
        self._recalculate()

    def _lay_edited(self, _text: str) -> None:
        self._lay_auto = False
        self._recalculate()

    def _use_suggested(self) -> None:
        self._lay_auto = True
        self._recalculate()

    def bet_type(self) -> BetType:
        return BetType(self.type_combo.currentData())

    def _read_position(self) -> TwoWayPosition:
        bet_type = self.bet_type()
        back_stake = parse_money(self.back_stake.text() or "0")
        back_odds = parse_odds(self.back_odds.text())
        lay_odds = parse_odds(self.lay_odds.text())
        if not self.commission.text().strip():
            raise ValidationError("Enter the exchange commission (0 if there is none).")
        commission_bp = parse_commission(self.commission.text())
        bookmaker = self.bookmaker.currentText().strip() or "bookmaker"
        exchange = self.exchange.currentText().strip() or "exchange"
        lay_stake: Pence | None = None
        if not self._lay_auto:
            if not self.lay_stake.text().strip():
                raise ValidationError("Enter the lay stake, or press Use suggested.")
            lay_stake = parse_money(self.lay_stake.text())
        return two_way_position(
            bet_type,
            back_stake,
            back_odds,
            lay_odds,
            commission_bp,
            lay_stake=lay_stake,
            bookmaker=bookmaker,
            exchange=exchange,
            back_odds_text=_odds_text_if_special(self.back_odds.text(), back_odds),
            lay_odds_text=_odds_text_if_special(self.lay_odds.text(), lay_odds),
        )

    def _recalculate(self, *_: object) -> None:
        try:
            position = self._read_position()
        except CoreError as exc:
            self._position = None
            self._show_error(str(exc))
            return
        self._position = position
        if self._lay_auto:
            self.lay_stake.blockSignals(True)
            self.lay_stake.setText(format_pence(position.lay.stake).lstrip("£"))
            self.lay_stake.blockSignals(False)
        self.use_suggested.setText(f"Use suggested ({format_pence(position.suggested_lay_stake)})")
        self.use_suggested.setEnabled(not position.lay_stake_is_suggested)
        self._figure(self.liability_label, format_pence(position.liability))
        self._figure(self.back_wins_label, pl_text(position.pl_if_back_wins))
        self._figure(self.lay_wins_label, pl_text(position.pl_if_lay_wins))
        self._figure(self.guaranteed_label, pl_text(position.guaranteed))
        self._figure(self.rating_label, f"{position.rating}%")
        venue = self._venues.get(self.exchange.currentText().strip().casefold())
        ladder = None if venue is None or venue.ladder is None else LADDERS.get(venue.ladder)
        minimum = None if venue is None else venue.min_stake_pence
        found = advisories_for(position, ladder=ladder, min_lay_stake=minimum)
        self.warnings_label.setText("\n".join(f"Warning: {a.message}" for a in found))
        problems = []
        if not self.event_name.text().strip():
            problems.append("Enter the event name.")
        if not self.bookmaker.currentText().strip():
            problems.append("Enter the bookmaker.")
        if not self.exchange.currentText().strip():
            problems.append("Enter the exchange.")
        elif venue is not None and venue.kind is not VenueKind.EXCHANGE:
            problems.append(f"{venue.name} is a bookmaker; lay at an exchange.")
        if self.mode is BetDialogMode.CORRECT and not self.reason.text().strip():
            problems.append("A correction needs a reason.")
        if problems:
            self._show_error(" ".join(problems))
        else:
            self.error_label.setText("")
            self.save_button.setEnabled(True)

    def _figure(self, label: QLabel, text: str) -> None:
        label.setText(text)
        label.setAccessibleName(f"{self._captions[id(label)]}: {text}")

    def _show_error(self, message: str) -> None:
        self.error_label.setText(message)
        self.save_button.setEnabled(False)
        self.use_suggested.setEnabled(not self._lay_auto)
        for label in (
            self.liability_label,
            self.back_wins_label,
            self.lay_wins_label,
            self.guaranteed_label,
            self.rating_label,
        ):
            self._figure(label, "—")
        self.warnings_label.setText("")

    def position(self) -> TwoWayPosition | None:
        return self._position

    def build_result(self) -> BetDialogResult:
        """The NewBet described by the form (raises CoreError if the form is not valid)."""
        position = self._read_position()
        back, lay = position.back, position.lay
        settled_at: datetime | None = None
        if self.mode is BetDialogMode.NEW and self.backfill.isChecked():
            outcome = TwoWayOutcome(self.backfill_outcome.currentData())
            back, lay = settle_two_way(back, lay, outcome)
            settled_at = from_qdatetime(self.settled_at.dateTime())

        bet = NewBet(
            bet_type=self.bet_type(),
            event_name=self.event_name.text().strip(),
            legs=[NewLeg.from_leg(back, keep_result=True), NewLeg.from_leg(lay, keep_result=True)],
            selection=self.selection.text().strip(),
            market=self.market.text().strip(),
            market_outcomes=2,
            offer_id=self.offer.currentData(),
            parent_bet_id=self.parent_bet.currentData(),
            placed_at=from_qdatetime(self.placed_at.dateTime()),
            event_at=from_qdatetime(self.event_at.dateTime())
            if self.has_event_at.isChecked()
            else None,
            settled_at=settled_at,
            notes=self.notes.toPlainText().strip(),
            needs_review=self.needs_review.isChecked(),
        )
        if self.mode is not BetDialogMode.CORRECT:
            return BetDialogResult(bet=bet)
        corrected = (
            from_qdatetime(self.corrected_settled_at.dateTime()) if self._has_settled_at else None
        )
        return BetDialogResult(bet=bet, reason=self.reason.text().strip(), settled_at=corrected)

    def _accept(self) -> None:
        """Validate, save through the callback, and close only if the save succeeded."""
        try:
            result = self.build_result()
            if self._save is not None:
                self._save(result)
        except (CoreError, StorageError) as exc:
            self.error_label.setText(str(exc))
            return
        self.result_value = result
        if self._settings is not None and self.mode is BetDialogMode.NEW and not self._prefilled:
            self._settings.last_bookmaker = self.bookmaker.currentText().strip()
            self._settings.last_exchange = self.exchange.currentText().strip()
        self.accept()


# --------------------------------------------------------------- settle dialog


@dataclass(frozen=True, slots=True)
class SettleRequest:
    outcome: TwoWayOutcome | None
    per_leg: dict[int, tuple[LegResult, Pence | None]]
    settled_at: datetime


class _LegRow:
    def __init__(self, index: int, leg: Leg) -> None:
        self.index = index
        self.leg = leg
        self.result = QComboBox()
        for result, label in RESULT_LABELS.items():
            self.result.addItem(label, result)
        self.result.setAccessibleName(f"Result of leg {index + 1}")
        back = leg.side is Side.BACK
        self.amount = _line(f"Amount for leg {index + 1}", "return £" if back else "net P/L £")
        self.amount.setToolTip(
            "The return the bookmaker credited, including your stake (a £10 bet paid out early "
            "at 2.00 returns £20)."
            if back
            else "The cash-out profit or loss the exchange shows for this leg."
        )
        self.amount.setMinimumWidth(110)
        self.amount.setEnabled(False)
        self.result.currentIndexChanged.connect(self._toggle_amount)

    def _toggle_amount(self, _index: int) -> None:
        # PySide6 hands StrEnum item data back as plain str; normalise before comparing.
        self.amount.setEnabled(LegResult(self.result.currentData()) is LegResult.CASHED_OUT)

    def value(self) -> tuple[LegResult, Pence | None] | None:
        result = LegResult(self.result.currentData())
        if result is LegResult.PENDING:
            return None
        if result is LegResult.CASHED_OUT:
            amount = parse_money(self.amount.text())
            if self.leg.side is Side.BACK:
                amount = self.leg.cashed_out_from_return(amount).settled_amount or 0
            return result, amount
        return result, None


class SettleDialog(QDialog):
    """Back won / Lay won / Void buttons plus a per-leg editor (SPEC §8.3)."""

    def __init__(
        self,
        bet: BetRecord,
        *,
        clock: Callable[[], datetime] = utc_now,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Settle: {bet.event_name}")
        self.bet = bet
        self.result_value: SettleRequest | None = None
        outer = QVBoxLayout(self)
        two_way = [r.leg.side for r in bet.legs] == [Side.BACK, Side.LAY] and all(
            not r.leg.is_settled for r in bet.legs
        )
        self.outcome_buttons: dict[TwoWayOutcome, QPushButton] = {}
        if two_way:
            row = QHBoxLayout()
            for outcome, label in OUTCOME_LABELS.items():
                button = QPushButton(label)
                # Never the default: Enter in the per-leg editor must not settle the whole bet.
                button.setAutoDefault(False)
                button.clicked.connect(lambda _=False, o=outcome: self._choose(o))
                row.addWidget(button)
                self.outcome_buttons[outcome] = button
            outer.addLayout(row)
        per_leg = QGroupBox("Per leg")
        grid = QGridLayout(per_leg)
        self.rows: list[_LegRow] = []
        for index, record in enumerate(bet.legs):
            leg = record.leg
            caption = (
                f"{leg.side.value.title()} {format_pence(leg.stake)} @ "
                f"{leg.odds_text or format_odds(leg.odds)} at {leg.venue}"
            )
            grid.addWidget(QLabel(caption), index, 0)
            if leg.is_settled:
                grid.addWidget(QLabel(RESULT_LABELS[leg.result]), index, 1)
                continue
            row_widgets = _LegRow(index, leg)
            grid.addWidget(row_widgets.result, index, 1)
            grid.addWidget(row_widgets.amount, index, 2)
            self.rows.append(row_widgets)
        outer.addWidget(per_leg)
        form = QFormLayout()
        self.settled_at = _datetime_edit("Settled at", clock())
        form.addRow("Settled at", self.settled_at)
        outer.addLayout(form)
        self.error_label = QLabel("")
        self.error_label.setWordWrap(True)
        outer.addWidget(self.error_label)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Apply | QDialogButtonBox.StandardButton.Cancel
        )
        self.apply_button = buttons.button(QDialogButtonBox.StandardButton.Apply)
        self.apply_button.setText("Apply per-leg results")
        self.apply_button.clicked.connect(self._apply)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)
        self.apply_button.setDefault(True)  # after it belongs to the dialog, see BetDialog
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setAutoDefault(False)

    def _choose(self, outcome: TwoWayOutcome) -> None:
        self.result_value = SettleRequest(
            outcome=outcome, per_leg={}, settled_at=from_qdatetime(self.settled_at.dateTime())
        )
        self.accept()

    def per_leg_results(self) -> dict[int, tuple[LegResult, Pence | None]]:
        results: dict[int, tuple[LegResult, Pence | None]] = {}
        for row in self.rows:
            value = row.value()
            if value is not None:
                results[row.index] = value
        return results

    def _apply(self) -> None:
        try:
            results = self.per_leg_results()
        except CoreError as exc:
            self.error_label.setText(str(exc))
            return
        if not results:
            self.error_label.setText("Choose a result for at least one leg.")
            return
        self.result_value = SettleRequest(
            outcome=None, per_leg=results, settled_at=from_qdatetime(self.settled_at.dateTime())
        )
        self.accept()


# --------------------------------------------------------------- adjust dialog


@dataclass(frozen=True, slots=True)
class AdjustRequest:
    override: Pence | None
    reason: str


class AdjustDialog(QDialog):
    """Audited manual override of the realised P/L (SPEC §8.4)."""

    def __init__(self, bet: BetRecord, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Adjust: {bet.event_name}")
        self.result_value: AdjustRequest | None = None
        outer = QVBoxLayout(self)
        outer.addWidget(QLabel(f"Derived realised P/L: {pl_text(bet.actual_pl_pence)}"))
        current = bet.actual_pl_override_pence
        outer.addWidget(
            QLabel(
                "No override in place."
                if current is None
                else f"Current override: {pl_text(current)}"
            )
        )
        self.set_radio = QRadioButton("Set realised P/L to £")
        self.set_radio.setAccessibleName("Set an override")
        self.clear_radio = QRadioButton("Clear the override")
        self.clear_radio.setAccessibleName("Clear the override")
        self.clear_radio.setEnabled(current is not None)
        self.set_radio.setChecked(True)
        self.amount = _line("Override amount", "e.g. 0 or -1.50")
        if current is not None:
            self.amount.setText(format_pence(current).replace("£", ""))
        row = QHBoxLayout()
        row.addWidget(self.set_radio)
        row.addWidget(self.amount, 1)
        outer.addLayout(row)
        outer.addWidget(self.clear_radio)
        form = QFormLayout()
        self.reason = _line("Reason", "required, e.g. 2-up early payout")
        form.addRow("&Reason", self.reason)
        outer.addLayout(form)
        self.error_label = QLabel("")
        outer.addWidget(self.error_label)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.accepted.connect(self._accept)
        self.buttons.rejected.connect(self.reject)
        outer.addWidget(self.buttons)
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setDefault(True)
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setAutoDefault(False)
        self.set_radio.toggled.connect(self.amount.setEnabled)

    def build_request(self) -> AdjustRequest:
        reason = self.reason.text().strip()
        if not reason:
            raise ValidationError("an adjustment needs a reason")
        if self.clear_radio.isChecked():
            return AdjustRequest(None, reason)
        return AdjustRequest(parse_money(self.amount.text()), reason)

    def _accept(self) -> None:
        try:
            self.result_value = self.build_request()
        except CoreError as exc:
            self.error_label.setText(str(exc))
            return
        self.accept()


def stake_kind_for(bet_type: BetType) -> StakeKind:
    return {
        BetType.QUALIFYING: StakeKind.CASH,
        BetType.FREE_BET_SNR: StakeKind.FREE_SNR,
        BetType.FREE_BET_SR: StakeKind.FREE_SR,
    }[bet_type]
