"""CSV export of the (filtered) ledger, one row per leg. SPEC §11.7."""

from __future__ import annotations

import csv
from typing import TYPE_CHECKING, TextIO

from odds_pup.core import format_commission, pounds, storage_form
from odds_pup.storage.schema import SCHEMA_VERSION
from odds_pup.storage.timestamps import to_db

if TYPE_CHECKING:
    from pathlib import Path

    from odds_pup.storage.models import BetFilter
    from odds_pup.storage.repository import Repository

CSV_COLUMNS = (
    "odds_pup_schema_version",
    "bet_id",
    "status",
    "bet_type",
    "event_name",
    "selection",
    "market",
    "market_outcomes",
    "placed_at",
    "event_at",
    "settled_at",
    "expected_pl",
    "actual_pl",
    "override_pl",
    "effective_pl",
    "needs_review",
    "has_adjustments",
    "notes",
    "leg_index",
    "side",
    "venue",
    "selection_index",
    "odds",
    "odds_text",
    "stake",
    "stake_kind",
    "commission",
    "result",
    "settled_amount",
)


def _money(value: int | None) -> str:
    return "" if value is None else str(pounds(value))


def _when(value: object) -> str:
    return "" if value is None else to_db(value)  # type: ignore[arg-type]


def write_csv(repo: Repository, out: TextIO, bet_filter: BetFilter | None = None) -> int:
    """Write the filtered ledger to ``out``; returns the number of leg rows written."""
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    rows = 0
    for bet in repo.list_bets(bet_filter):
        for index, record in enumerate(bet.legs):
            leg = record.leg
            writer.writerow(
                (
                    SCHEMA_VERSION,
                    bet.id,
                    bet.status.value,
                    bet.bet_type.value,
                    bet.event_name,
                    bet.selection,
                    bet.market,
                    bet.market_outcomes,
                    _when(bet.placed_at),
                    _when(bet.event_at),
                    _when(bet.settled_at),
                    _money(bet.expected_pl_pence),
                    _money(bet.actual_pl_pence),
                    _money(bet.actual_pl_override_pence),
                    _money(bet.effective_actual_pl),
                    "1" if bet.needs_review else "0",
                    "1" if bet.has_adjustments else "0",
                    bet.notes,
                    index,
                    leg.side.value,
                    leg.venue,
                    leg.selection,
                    storage_form(leg.odds),
                    leg.odds_text or "",
                    _money(leg.stake),
                    leg.stake_kind.value,
                    format_commission(leg.commission_bp),
                    leg.result.value,
                    _money(leg.settled_amount),
                )
            )
            rows += 1
    return rows


def export_csv(repo: Repository, path: Path, bet_filter: BetFilter | None = None) -> int:
    """Write the export to ``path`` (UTF-8, owner-only permissions)."""
    with path.open("w", encoding="utf-8", newline="") as out:
        count = write_csv(repo, out, bet_filter)
    path.chmod(0o600)
    return count
