"""CSV export of the (filtered) ledger, one row per leg. SPEC §11.7."""

from __future__ import annotations

import csv
import os
from typing import TYPE_CHECKING, TextIO

from odds_pup.core import format_commission, pounds, storage_form
from odds_pup.storage.paths import FILE_MODE
from odds_pup.storage.schema import SCHEMA_VERSION
from odds_pup.storage.timestamps import to_db

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime
    from pathlib import Path

    from odds_pup.storage.models import BetFilter, BetRecord
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
    "deleted",
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


def _when(value: datetime | None) -> str:
    return "" if value is None else to_db(value)


def write_rows(bets: Sequence[BetRecord], out: TextIO) -> int:
    """Write already-loaded bets to ``out``; returns the number of leg rows written."""
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    rows = 0
    for bet in bets:
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
                    "1" if bet.is_deleted else "0",
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


def write_csv(repo: Repository, out: TextIO, bet_filter: BetFilter | None = None) -> int:
    """Query first, then write, so a failed query never leaves a half-written stream."""
    return write_rows(repo.list_bets(bet_filter), out)


def export_csv(repo: Repository, path: Path, bet_filter: BetFilter | None = None) -> int:
    """Write the export to ``path`` atomically (temp file, owner-only, then rename)."""
    bets = repo.list_bets(bet_filter)
    temp = path.with_name(f".{path.name}.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC, FILE_MODE)
    try:
        os.fchmod(fd, FILE_MODE)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as out:
            count = write_rows(bets, out)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    return count
