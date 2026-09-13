"""The ledger: one connection, one transaction per user action. SPEC §8-§11."""

from __future__ import annotations

import enum
import sqlite3
import uuid
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from odds_pup.core import (
    AuditKind,
    BetStatus,
    BetType,
    Leg,
    LegResult,
    Pence,
    Side,
    StakeKind,
    TwoWayOutcome,
    ValidationError,
    VenueKind,
    guaranteed_pl,
    realised_pl,
    status_of,
    storage_form,
    validate_position,
)
from odds_pup.core import settle_two_way as core_settle_two_way
from odds_pup.storage.backup import create_backup
from odds_pup.storage.database import connect, migrate, transaction
from odds_pup.storage.errors import IllegalActionError, NotFoundError, StorageError
from odds_pup.storage.models import (
    AuditRow,
    BetFilter,
    BetRecord,
    LegRecord,
    NewBet,
    NewLeg,
    Offer,
    Summary,
    Venue,
)
from odds_pup.storage.paths import DataPaths, prepare
from odds_pup.storage.schema import SEED_VENUES
from odds_pup.storage.timestamps import from_db, local_month_bounds, to_db, utc_now

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence
    from pathlib import Path

    from odds_pup.core.ladder import Ladder


class _Unset(enum.Enum):
    token = 0


UNSET = _Unset.token
"""Sentinel for "leave this field alone" in edit and correct calls."""

OPEN_STATUSES = frozenset({BetStatus.OPEN, BetStatus.PARTIALLY_SETTLED})
SETTLED_STATUSES = frozenset({BetStatus.SETTLED, BetStatus.VOID})
_CHUNK = 500
_LEG_FIELDS = (
    "side",
    "venue",
    "odds",
    "stake",
    "selection",
    "stake_kind",
    "commission_bp",
    "odds_text",
)


def _new_id() -> str:
    return str(uuid.uuid4())


def _text(value: object) -> str | None:
    """Audit-trail serialisation: the stored text form of a value (SPEC §10)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, datetime):
        return to_db(value)
    if isinstance(value, Decimal):
        return storage_form(value)
    if isinstance(value, enum.Enum):
        return str(value.value)
    return str(value)


def _optional_dt(text: str | None) -> datetime | None:
    return None if text is None else from_db(text)


class Repository:
    """All reads and writes against one ledger database."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        clock: Callable[[], datetime] = utc_now,
        paths: DataPaths | None = None,
    ) -> None:
        self._conn = connection
        self._clock = clock
        self.paths = paths

    @classmethod
    def open(
        cls,
        paths: DataPaths,
        *,
        clock: Callable[[], datetime] = utc_now,
        backups: bool = True,
    ) -> Repository:
        """Prepare the directory, connect, back up, migrate, seed (SPEC §11.3-§11.5)."""
        prepare(paths)
        connection = connect(paths.database)
        repo = cls(connection, clock=clock, paths=paths)

        def backup() -> None:
            create_backup(connection, paths.backups, now=clock())

        migrate(connection, before_migration=backup if backups else None)
        repo.seed_venues()
        if backups:
            backup()
        return repo

    def close(self) -> None:
        self._conn.close()

    def backup(self) -> Path:
        if self.paths is None:
            raise StorageError("this repository has no data directory to back up into")
        return create_backup(self._conn, self.paths.backups, now=self._clock())

    # ------------------------------------------------------------------ venues

    def _venue(self, row: sqlite3.Row) -> Venue:
        return Venue(
            id=row["id"],
            name=row["name"],
            kind=VenueKind(row["kind"]),
            default_commission_bp=row["default_commission_bp"],
            min_stake_pence=row["min_stake_pence"],
            ladder=row["ladder"],
            created_at=from_db(row["created_at"]),
        )

    def list_venues(self, kind: VenueKind | None = None) -> list[Venue]:
        sql = "SELECT * FROM venues"
        params: tuple[object, ...] = ()
        if kind is not None:
            sql += " WHERE kind = ?"
            params = (kind.value,)
        sql += " ORDER BY name COLLATE NOCASE"
        return [self._venue(r) for r in self._conn.execute(sql, params)]

    def get_venue(self, venue_id: str) -> Venue:
        row = self._conn.execute("SELECT * FROM venues WHERE id = ?", (venue_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"no venue {venue_id}")
        return self._venue(row)

    def find_venue(self, name: str) -> Venue | None:
        """Case-insensitive lookup by name (the column collates NOCASE)."""
        row = self._conn.execute("SELECT * FROM venues WHERE name = ?", (name.strip(),)).fetchone()
        return None if row is None else self._venue(row)

    def _insert_venue(
        self,
        name: str,
        kind: VenueKind,
        default_commission_bp: int,
        min_stake_pence: Pence | None,
        ladder: str | None,
    ) -> Venue:
        cleaned = name.strip()
        if not cleaned:
            raise ValidationError("venue name must not be empty")
        venue_id = _new_id()
        self._conn.execute(
            "INSERT INTO venues (id, name, kind, default_commission_bp, min_stake_pence, ladder,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                venue_id,
                cleaned,
                kind.value,
                default_commission_bp,
                min_stake_pence,
                ladder,
                to_db(self._clock()),
            ),
        )
        return self.get_venue(venue_id)

    def ensure_venue(
        self,
        name: str,
        kind: VenueKind,
        *,
        default_commission_bp: int = 0,
        min_stake_pence: Pence | None = None,
        ladder: str | None = None,
    ) -> Venue:
        """The venue called ``name``, created with these defaults if it does not exist yet."""
        existing = self.find_venue(name)
        if existing is not None:
            if existing.kind is not kind:
                raise IllegalActionError(
                    f"{existing.name} is a {existing.kind.value.lower()}, "
                    f"not a {kind.value.lower()}"
                )
            return existing
        with transaction(self._conn):
            return self._insert_venue(name, kind, default_commission_bp, min_stake_pence, ladder)

    def update_venue(
        self,
        venue_id: str,
        *,
        default_commission_bp: int | _Unset = UNSET,
        min_stake_pence: Pence | _Unset | None = UNSET,
        ladder: str | _Unset | None = UNSET,
    ) -> Venue:
        current = self.get_venue(venue_id)
        with transaction(self._conn):
            self._conn.execute(
                "UPDATE venues SET default_commission_bp = ?, min_stake_pence = ?, ladder = ?"
                " WHERE id = ?",
                (
                    current.default_commission_bp
                    if isinstance(default_commission_bp, _Unset)
                    else default_commission_bp,
                    current.min_stake_pence
                    if isinstance(min_stake_pence, _Unset)
                    else min_stake_pence,
                    current.ladder if isinstance(ladder, _Unset) else ladder,
                    venue_id,
                ),
            )
        return self.get_venue(venue_id)

    def seed_venues(self) -> int:
        """Insert the UK seed list into an empty venues table. Returns how many were added."""
        count = self._conn.execute("SELECT COUNT(*) FROM venues").fetchone()[0]
        if count:
            return 0
        with transaction(self._conn):
            for name, kind, bp, min_stake, ladder in SEED_VENUES:
                self._insert_venue(name, kind, bp, min_stake, ladder)
        return len(SEED_VENUES)

    def ladder_for(self, venue: Venue, ladders: Mapping[str, Ladder]) -> Ladder | None:
        return None if venue.ladder is None else ladders.get(venue.ladder)

    # ------------------------------------------------------------------ offers

    def _offer(self, row: sqlite3.Row) -> Offer:
        return Offer(
            id=row["id"],
            venue_id=row["venue_id"],
            title=row["title"],
            notes=row["notes"],
            created_at=from_db(row["created_at"]),
        )

    def create_offer(self, title: str, *, venue_id: str | None = None, notes: str = "") -> Offer:
        if not title.strip():
            raise ValidationError("offer title must not be empty")
        offer_id = _new_id()
        with transaction(self._conn):
            self._conn.execute(
                "INSERT INTO offers (id, venue_id, title, notes, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (offer_id, venue_id, title.strip(), notes, to_db(self._clock())),
            )
        return self.get_offer(offer_id)

    def get_offer(self, offer_id: str) -> Offer:
        row = self._conn.execute("SELECT * FROM offers WHERE id = ?", (offer_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"no offer {offer_id}")
        return self._offer(row)

    def list_offers(self) -> list[Offer]:
        rows = self._conn.execute("SELECT * FROM offers ORDER BY created_at DESC, title")
        return [self._offer(r) for r in rows]

    # -------------------------------------------------------------------- bets

    def _resolve_leg(self, new: NewLeg) -> tuple[Venue, Leg]:
        """Turn form input into a venue row and a validated domain leg."""
        wanted_kind = VenueKind.EXCHANGE if new.side is Side.LAY else None
        venue = self.find_venue(new.venue)
        if venue is None:
            kind = VenueKind.EXCHANGE if new.side is Side.LAY else VenueKind.BOOKMAKER
            venue = self._insert_venue(new.venue, kind, 0, None, None)
        elif wanted_kind is not None and venue.kind is not wanted_kind:
            raise IllegalActionError(
                f"cannot lay at {venue.name}: it is a bookmaker, not an exchange"
            )
        if new.commission_bp is not None:
            commission_bp = new.commission_bp
        else:
            commission_bp = venue.default_commission_bp if venue.kind is VenueKind.EXCHANGE else 0
        leg = Leg(
            side=new.side,
            venue=venue.name,
            odds=new.odds,
            stake=new.stake,
            selection=new.selection,
            stake_kind=new.stake_kind,
            commission_bp=commission_bp,
            result=new.result,
            settled_amount=new.settled_amount,
            odds_text=new.odds_text,
        )
        return venue, leg

    def _insert_legs(self, bet_id: str, resolved: Sequence[tuple[Venue, Leg]], now: str) -> None:
        for index, (venue, leg) in enumerate(resolved):
            self._conn.execute(
                "INSERT INTO bet_legs (id, bet_id, leg_index, side, venue_id, selection_index,"
                " odds, odds_text, stake_pence, stake_kind, commission_bp, result,"
                " settled_amount_pence, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _new_id(),
                    bet_id,
                    index,
                    leg.side.value,
                    venue.id,
                    leg.selection,
                    storage_form(leg.odds),
                    leg.odds_text,
                    leg.stake,
                    leg.stake_kind.value,
                    leg.commission_bp,
                    leg.result.value,
                    leg.settled_amount,
                    now,
                ),
            )

    def _audit(
        self,
        bet_id: str,
        action_id: str,
        kind: AuditKind,
        *,
        field: str | None = None,
        old: object = None,
        new: object = None,
        reason: str | None = None,
        at: str | None = None,
    ) -> None:
        self._conn.execute(
            "INSERT INTO adjustments (id, bet_id, action_id, at, kind, field, old_value,"
            " new_value, reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _new_id(),
                bet_id,
                action_id,
                at or to_db(self._clock()),
                kind.value,
                field,
                _text(old),
                _text(new),
                reason,
            ),
        )

    def create_bet(self, new: NewBet) -> BetRecord:
        """Insert a bet with its legs; legs carrying results make it a backfilled bet (§8.5)."""
        if not new.event_name.strip():
            raise ValidationError("event name must not be empty")
        if not new.legs:
            raise ValidationError("a bet needs at least one leg")
        now_dt = self._clock()
        now = to_db(now_dt)
        with transaction(self._conn):
            resolved = [self._resolve_leg(leg) for leg in new.legs]
            core_legs = tuple(leg for _, leg in resolved)
            validate_position(core_legs, new.market_outcomes)
            status = status_of(core_legs)
            expected = guaranteed_pl(
                tuple(leg.reopened() for leg in core_legs), new.market_outcomes
            )
            actual = realised_pl(core_legs)
            placed_at = new.placed_at or now_dt
            settled_at: datetime | None = None
            if status in SETTLED_STATUSES:
                settled_at = new.settled_at or now_dt
            bet_id = _new_id()
            try:
                self._conn.execute(
                    "INSERT INTO bets (id, bet_type, status, event_name, selection, market,"
                    " market_outcomes, offer_id, parent_bet_id, currency, placed_at, event_at,"
                    " settled_at, expected_pl_pence, actual_pl_pence, actual_pl_override_pence,"
                    " needs_review, notes, created_at, updated_at, deleted_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'GBP', ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?,"
                    " NULL)",
                    (
                        bet_id,
                        new.bet_type.value,
                        status.value,
                        new.event_name.strip(),
                        new.selection,
                        new.market,
                        new.market_outcomes,
                        new.offer_id,
                        new.parent_bet_id,
                        to_db(placed_at),
                        None if new.event_at is None else to_db(new.event_at),
                        None if settled_at is None else to_db(settled_at),
                        expected,
                        actual,
                        1 if new.needs_review else 0,
                        new.notes,
                        now,
                        now,
                    ),
                )
                self._insert_legs(bet_id, resolved, now)
            except sqlite3.IntegrityError as exc:
                raise StorageError(f"could not save bet: {exc}") from exc
            backfilled = any(leg.is_settled for leg in core_legs)
            self._audit(
                bet_id,
                _new_id(),
                AuditKind.CREATED,
                new="backfill" if backfilled else None,
                at=now,
            )
        return self.get_bet(bet_id)

    def _bet_rows(self, sql: str, params: Sequence[object]) -> list[BetRecord]:
        rows = self._conn.execute(sql, tuple(params)).fetchall()
        if not rows:
            return []
        ids = [r["id"] for r in rows]
        legs_by_bet: defaultdict[str, list[LegRecord]] = defaultdict(list)
        flagged: set[str] = set()
        for start in range(0, len(ids), _CHUNK):
            chunk = ids[start : start + _CHUNK]
            marks = ",".join("?" * len(chunk))
            for row in self._conn.execute(
                "SELECT l.*, v.name AS venue_name FROM bet_legs l JOIN venues v ON v.id ="
                f" l.venue_id WHERE l.bet_id IN ({marks}) ORDER BY l.bet_id, l.leg_index",
                chunk,
            ):
                legs_by_bet[row["bet_id"]].append(
                    LegRecord(
                        id=row["id"],
                        venue_id=row["venue_id"],
                        leg=Leg(
                            side=Side(row["side"]),
                            venue=row["venue_name"],
                            odds=Decimal(row["odds"]),
                            stake=row["stake_pence"],
                            selection=row["selection_index"],
                            stake_kind=StakeKind(row["stake_kind"]),
                            commission_bp=row["commission_bp"],
                            result=LegResult(row["result"]),
                            settled_amount=row["settled_amount_pence"],
                            odds_text=row["odds_text"],
                        ),
                    )
                )
            for row in self._conn.execute(
                "SELECT DISTINCT bet_id FROM adjustments WHERE kind IN ('ADJUSTMENT',"
                f" 'CORRECTION') AND bet_id IN ({marks})",
                chunk,
            ):
                flagged.add(row["bet_id"])
        return [
            BetRecord(
                id=r["id"],
                bet_type=BetType(r["bet_type"]),
                status=BetStatus(r["status"]),
                event_name=r["event_name"],
                selection=r["selection"],
                market=r["market"],
                market_outcomes=r["market_outcomes"],
                offer_id=r["offer_id"],
                parent_bet_id=r["parent_bet_id"],
                currency=r["currency"],
                placed_at=from_db(r["placed_at"]),
                event_at=_optional_dt(r["event_at"]),
                settled_at=_optional_dt(r["settled_at"]),
                expected_pl_pence=r["expected_pl_pence"],
                actual_pl_pence=r["actual_pl_pence"],
                actual_pl_override_pence=r["actual_pl_override_pence"],
                needs_review=bool(r["needs_review"]),
                notes=r["notes"],
                created_at=from_db(r["created_at"]),
                updated_at=from_db(r["updated_at"]),
                deleted_at=_optional_dt(r["deleted_at"]),
                legs=tuple(legs_by_bet[r["id"]]),
                has_adjustments=r["id"] in flagged,
            )
            for r in rows
        ]

    def get_bet(self, bet_id: str) -> BetRecord:
        """A bet by id, deleted or not (the detail pane can show and restore deleted bets)."""
        found = self._bet_rows("SELECT * FROM bets WHERE id = ?", (bet_id,))
        if not found:
            raise NotFoundError(f"no bet {bet_id}")
        return found[0]

    def list_bets(self, bet_filter: BetFilter | None = None) -> list[BetRecord]:
        """Ledger rows, newest placed first (SPEC §13 filters)."""
        f = bet_filter or BetFilter()
        clauses: list[str] = []
        params: list[object] = []
        if not f.include_deleted:
            clauses.append("b.deleted_at IS NULL")
        if f.statuses is not None:
            if not f.statuses:
                return []
            clauses.append(f"b.status IN ({','.join('?' * len(f.statuses))})")
            params.extend(sorted(s.value for s in f.statuses))
        if f.bet_types is not None:
            if not f.bet_types:
                return []
            clauses.append(f"b.bet_type IN ({','.join('?' * len(f.bet_types))})")
            params.extend(sorted(t.value for t in f.bet_types))
        if f.placed_from is not None:
            clauses.append("b.placed_at >= ?")
            params.append(to_db(f.placed_from))
        if f.placed_to is not None:
            clauses.append("b.placed_at < ?")
            params.append(to_db(f.placed_to))
        if f.offer_id is not None:
            clauses.append("b.offer_id = ?")
            params.append(f.offer_id)
        if f.needs_review is not None:
            clauses.append("b.needs_review = ?")
            params.append(1 if f.needs_review else 0)
        if f.text:
            pattern = "%" + _escape_like(f.text.strip()) + "%"
            clauses.append(
                "(b.event_name LIKE ? ESCAPE '\\' OR b.selection LIKE ? ESCAPE '\\'"
                " OR b.market LIKE ? ESCAPE '\\' OR b.notes LIKE ? ESCAPE '\\')"
            )
            params.extend([pattern] * 4)
        for side, name in ((Side.BACK, f.bookmaker), (Side.LAY, f.exchange)):
            if name:
                clauses.append(
                    "EXISTS (SELECT 1 FROM bet_legs l JOIN venues v ON v.id = l.venue_id"
                    " WHERE l.bet_id = b.id AND l.side = ? AND v.name = ?)"
                )
                params.extend([side.value, name.strip()])
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        return self._bet_rows(
            f"SELECT b.* FROM bets b{where} ORDER BY b.placed_at DESC, b.created_at DESC, b.id",
            params,
        )

    # ------------------------------------------------------------ edit/correct

    def edit_bet(self, bet_id: str, **changes: Any) -> BetRecord:
        """Change an OPEN bet's parameters; the placement expectation is recomputed (§9.2)."""
        bet = self.get_bet(bet_id)
        if bet.status is not BetStatus.OPEN:
            raise IllegalActionError(
                "only an OPEN bet can be edited; use correct_bet with a reason"
            )
        return self._apply_changes(bet, AuditKind.EDITED, None, changes)

    def correct_bet(self, bet_id: str, *, reason: str, **changes: Any) -> BetRecord:
        """Fix a data-entry error on a bet that has left OPEN; expectation stays frozen (§9.2)."""
        bet = self.get_bet(bet_id)
        if bet.status is BetStatus.OPEN:
            raise IllegalActionError("an OPEN bet is edited, not corrected")
        if not reason or not reason.strip():
            raise ValidationError("a correction needs a reason")
        return self._apply_changes(bet, AuditKind.CORRECTION, reason.strip(), changes)

    _HEADER_FIELDS = frozenset(
        {
            "event_name",
            "selection",
            "market",
            "market_outcomes",
            "offer_id",
            "parent_bet_id",
            "placed_at",
            "event_at",
            "notes",
        }
    )

    def _header_changes(
        self, bet: BetRecord, changes: Mapping[str, Any]
    ) -> list[tuple[str, object, object]]:
        unknown = set(changes) - self._HEADER_FIELDS - {"legs"}
        if unknown:
            raise ValidationError(f"unknown bet fields: {sorted(unknown)}")
        header: list[tuple[str, object, object]] = []
        for name in sorted(self._HEADER_FIELDS):
            if name not in changes or isinstance(changes[name], _Unset):
                continue
            new_value = changes[name]
            if name == "event_name" and (not isinstance(new_value, str) or not new_value.strip()):
                raise ValidationError("event name must not be empty")
            if name == "market_outcomes" and (
                isinstance(new_value, bool) or not isinstance(new_value, int)
            ):
                raise ValidationError("market_outcomes must be an int")
            if name == "placed_at" and new_value is None:
                raise ValidationError("placed_at is required")
            if (
                name in ("placed_at", "event_at")
                and new_value is not None
                and (not isinstance(new_value, datetime) or new_value.tzinfo is None)
            ):
                raise ValidationError(f"{name} must be a timezone-aware datetime")
            old_value = getattr(bet, name)
            if new_value != old_value:
                header.append((name, old_value, new_value))
        return header

    def _replace_legs(
        self,
        bet: BetRecord,
        new_legs: Sequence[NewLeg],
        *,
        kind: AuditKind,
        reason: str | None,
        action_id: str,
        now: str,
        market_outcomes: int,
    ) -> tuple[Leg, ...]:
        """Swap a bet's legs for ``new_legs`` inside the caller's transaction, auditing diffs."""
        if kind is AuditKind.EDITED and any(
            leg.result is not LegResult.PENDING for leg in new_legs
        ):
            raise IllegalActionError("settle a bet with settle(), not by editing legs")
        if kind is AuditKind.CORRECTION and len(new_legs) != len(bet.legs):
            raise IllegalActionError(
                "a correction cannot add or remove legs; reopen and recreate the bet"
            )
        resolved: list[tuple[Venue, Leg]] = []
        for index, new_leg in enumerate(new_legs):
            venue, leg = self._resolve_leg(new_leg)
            if kind is AuditKind.CORRECTION:  # results are not correctable (§9.2)
                stored = bet.legs[index].leg
                leg = leg.settled(stored.result, stored.settled_amount)
            resolved.append((venue, leg))
        core_legs = tuple(leg for _, leg in resolved)
        validate_position(core_legs, market_outcomes)
        for index, leg in enumerate(core_legs):
            old_leg = bet.legs[index].leg if index < len(bet.legs) else None
            for field_name in _LEG_FIELDS:
                old_v = None if old_leg is None else getattr(old_leg, field_name)
                new_v = getattr(leg, field_name)
                if old_v != new_v:
                    self._audit(
                        bet.id,
                        action_id,
                        kind,
                        field=f"legs[{index}].{field_name}",
                        old=old_v,
                        new=new_v,
                        reason=reason,
                        at=now,
                    )
        for index in range(len(core_legs), len(bet.legs)):
            self._audit(
                bet.id,
                action_id,
                kind,
                field=f"legs[{index}]",
                old="present",
                new="removed",
                reason=reason,
                at=now,
            )
        self._conn.execute("DELETE FROM bet_legs WHERE bet_id = ?", (bet.id,))
        self._insert_legs(bet.id, resolved, now)
        return core_legs

    def _apply_changes(
        self,
        bet: BetRecord,
        kind: AuditKind,
        reason: str | None,
        changes: Mapping[str, Any],
    ) -> BetRecord:
        if bet.is_deleted:
            raise IllegalActionError("restore the bet before changing it")
        header = self._header_changes(bet, changes)
        raw_legs = changes.get("legs")
        new_legs: Sequence[NewLeg] | None = (
            None if raw_legs is None or isinstance(raw_legs, _Unset) else list(raw_legs)
        )
        if not header and new_legs is None:
            return bet
        now = to_db(self._clock())
        action_id = _new_id()
        market_outcomes = bet.market_outcomes
        for name, _, value in header:
            if name == "market_outcomes":
                market_outcomes = int(value)  # type: ignore[call-overload]
        with transaction(self._conn):
            if new_legs is not None:
                core_legs = self._replace_legs(
                    bet,
                    new_legs,
                    kind=kind,
                    reason=reason,
                    action_id=action_id,
                    now=now,
                    market_outcomes=market_outcomes,
                )
            else:
                core_legs = bet.core_legs
                validate_position(core_legs, market_outcomes)
            for name, old_value, new_value in header:
                self._audit(
                    bet.id,
                    action_id,
                    kind,
                    field=name,
                    old=old_value,
                    new=new_value,
                    reason=reason,
                    at=now,
                )
            expected = bet.expected_pl_pence
            if kind is AuditKind.EDITED:
                expected = guaranteed_pl(
                    tuple(leg.reopened() for leg in core_legs), market_outcomes
                )
                if expected != bet.expected_pl_pence:
                    self._audit(
                        bet.id,
                        action_id,
                        kind,
                        field="expected_pl_pence",
                        old=bet.expected_pl_pence,
                        new=expected,
                        at=now,
                    )
            values: dict[str, object] = {name: new_value for name, _, new_value in header}
            for key in ("placed_at", "event_at"):
                if isinstance(values.get(key), datetime):
                    values[key] = to_db(values[key])  # type: ignore[arg-type]
            values.update(
                status=status_of(core_legs).value,
                expected_pl_pence=expected,
                actual_pl_pence=realised_pl(core_legs),
                updated_at=now,
            )
            assignments = ", ".join(f"{k} = ?" for k in values)
            try:
                self._conn.execute(
                    f"UPDATE bets SET {assignments} WHERE id = ?", (*values.values(), bet.id)
                )
            except sqlite3.IntegrityError as exc:
                raise StorageError(f"could not save bet: {exc}") from exc
        return self.get_bet(bet.id)

    # ----------------------------------------------------------------- settle

    def settle(
        self,
        bet_id: str,
        results: Mapping[int, LegResult | tuple[LegResult, Pence | None]],
        *,
        settled_at: datetime | None = None,
    ) -> BetRecord:
        """Record results for pending legs by index; derive status and realised P/L (§8.3)."""
        bet = self.get_bet(bet_id)
        if bet.is_deleted:
            raise IllegalActionError("restore the bet before settling it")
        if not results:
            raise ValidationError("nothing to settle")
        legs = list(bet.core_legs)
        now_dt = self._clock()
        now = to_db(now_dt)
        action_id = _new_id()
        with transaction(self._conn):
            for index, spec in results.items():
                if not 0 <= index < len(legs):
                    raise NotFoundError(f"bet has no leg {index}")
                if legs[index].is_settled:
                    raise IllegalActionError(f"leg {index} already has a result; reopen it first")
                result, amount = spec if isinstance(spec, tuple) else (spec, None)
                if result is LegResult.PENDING:
                    raise ValidationError("PENDING is not a settlement result")
                legs[index] = legs[index].settled(result, amount)
                self._conn.execute(
                    "UPDATE bet_legs SET result = ?, settled_amount_pence = ? WHERE bet_id = ?"
                    " AND leg_index = ?",
                    (result.value, amount, bet.id, index),
                )
                self._audit(
                    bet.id,
                    action_id,
                    AuditKind.SETTLED,
                    field=f"legs[{index}].result",
                    old=LegResult.PENDING,
                    new=result,
                    at=now,
                )
                if amount is not None:
                    self._audit(
                        bet.id,
                        action_id,
                        AuditKind.SETTLED,
                        field=f"legs[{index}].settled_amount",
                        old=None,
                        new=amount,
                        at=now,
                    )
            status = status_of(legs)
            actual = realised_pl(legs)
            settled_text: str | None = None
            if status in SETTLED_STATUSES:
                settled_text = to_db(settled_at or now_dt)
            if status is not bet.status:
                self._audit(
                    bet.id,
                    action_id,
                    AuditKind.SETTLED,
                    field="status",
                    old=bet.status,
                    new=status,
                    at=now,
                )
            self._conn.execute(
                "UPDATE bets SET status = ?, actual_pl_pence = ?, settled_at = ?, updated_at = ?"
                " WHERE id = ?",
                (status.value, actual, settled_text, now, bet.id),
            )
        return self.get_bet(bet.id)

    def settle_two_way(
        self, bet_id: str, outcome: TwoWayOutcome, *, settled_at: datetime | None = None
    ) -> BetRecord:
        """The Settle dialog's three buttons for a one-back-one-lay bet (§8.3)."""
        bet = self.get_bet(bet_id)
        if len(bet.legs) != 2 or [r.leg.side for r in bet.legs] != [Side.BACK, Side.LAY]:
            raise IllegalActionError("settle_two_way needs a bet with exactly one back and one lay")
        if any(r.leg.is_settled for r in bet.legs):
            raise IllegalActionError("a leg already has a result; reopen it first")
        back, lay = core_settle_two_way(bet.legs[0].leg, bet.legs[1].leg, outcome)
        return self.settle(bet_id, {0: back.result, 1: lay.result}, settled_at=settled_at)

    def reopen(self, bet_id: str, leg_indexes: Iterable[int] | None = None) -> BetRecord:
        """Return settled legs to PENDING; clears realised figures and any override (§9.2)."""
        bet = self.get_bet(bet_id)
        if bet.status is BetStatus.OPEN:
            raise IllegalActionError("the bet is already open")
        if bet.is_deleted:
            raise IllegalActionError("restore the bet before reopening it")
        legs = list(bet.core_legs)
        wanted = (
            [i for i, leg in enumerate(legs) if leg.is_settled]
            if leg_indexes is None
            else list(leg_indexes)
        )
        if not wanted:
            raise ValidationError("no legs to reopen")
        now = to_db(self._clock())
        action_id = _new_id()
        with transaction(self._conn):
            for index in wanted:
                if not 0 <= index < len(legs):
                    raise NotFoundError(f"bet has no leg {index}")
                if not legs[index].is_settled:
                    raise IllegalActionError(f"leg {index} is not settled")
                old = legs[index]
                legs[index] = old.reopened()
                self._conn.execute(
                    "UPDATE bet_legs SET result = 'PENDING', settled_amount_pence = NULL"
                    " WHERE bet_id = ? AND leg_index = ?",
                    (bet.id, index),
                )
                self._audit(
                    bet.id,
                    action_id,
                    AuditKind.REOPENED,
                    field=f"legs[{index}].result",
                    old=old.result,
                    new=LegResult.PENDING,
                    at=now,
                )
                if old.settled_amount is not None:
                    self._audit(
                        bet.id,
                        action_id,
                        AuditKind.REOPENED,
                        field=f"legs[{index}].settled_amount",
                        old=old.settled_amount,
                        new=None,
                        at=now,
                    )
            status = status_of(legs)
            actual = realised_pl(legs)
            settled_text = None
            if status in SETTLED_STATUSES and bet.settled_at is not None:
                settled_text = to_db(bet.settled_at)
            if bet.actual_pl_override_pence is not None:
                self._audit(
                    bet.id,
                    action_id,
                    AuditKind.REOPENED,
                    field="actual_pl_override_pence",
                    old=bet.actual_pl_override_pence,
                    new=None,
                    at=now,
                )
            if status is not bet.status:
                self._audit(
                    bet.id,
                    action_id,
                    AuditKind.REOPENED,
                    field="status",
                    old=bet.status,
                    new=status,
                    at=now,
                )
            self._conn.execute(
                "UPDATE bets SET status = ?, actual_pl_pence = ?, actual_pl_override_pence = NULL,"
                " settled_at = ?, updated_at = ? WHERE id = ?",
                (status.value, actual, settled_text, now, bet.id),
            )
        return self.get_bet(bet.id)

    def adjust(self, bet_id: str, override_pence: Pence | None, reason: str) -> BetRecord:
        """Audited manual override of the realised P/L on a settled bet (§8.4)."""
        bet = self.get_bet(bet_id)
        if bet.status not in SETTLED_STATUSES:
            raise IllegalActionError("only a SETTLED or VOID bet can be adjusted")
        if bet.is_deleted:
            raise IllegalActionError("restore the bet before adjusting it")
        if not reason or not reason.strip():
            raise ValidationError("an adjustment needs a reason")
        if override_pence is not None and (
            isinstance(override_pence, bool) or not isinstance(override_pence, int)
        ):
            raise ValidationError("override must be an int number of pence or None")
        if override_pence == bet.actual_pl_override_pence:
            return bet
        now = to_db(self._clock())
        with transaction(self._conn):
            self._audit(
                bet.id,
                _new_id(),
                AuditKind.ADJUSTMENT,
                field="actual_pl_override_pence",
                old=bet.actual_pl_override_pence,
                new=override_pence,
                reason=reason.strip(),
                at=now,
            )
            self._conn.execute(
                "UPDATE bets SET actual_pl_override_pence = ?, updated_at = ? WHERE id = ?",
                (override_pence, now, bet.id),
            )
        return self.get_bet(bet.id)

    def set_needs_review(self, bet_id: str, flag: bool, reason: str | None = None) -> BetRecord:
        bet = self.get_bet(bet_id)
        if bet.needs_review == flag:
            return bet
        now = to_db(self._clock())
        with transaction(self._conn):
            self._audit(
                bet.id,
                _new_id(),
                AuditKind.EDITED,
                field="needs_review",
                old=bet.needs_review,
                new=flag,
                reason=reason,
                at=now,
            )
            self._conn.execute(
                "UPDATE bets SET needs_review = ?, updated_at = ? WHERE id = ?",
                (1 if flag else 0, now, bet.id),
            )
        return self.get_bet(bet.id)

    def soft_delete(self, bet_id: str) -> BetRecord:
        bet = self.get_bet(bet_id)
        if bet.is_deleted:
            return bet
        now = to_db(self._clock())
        with transaction(self._conn):
            self._audit(bet.id, _new_id(), AuditKind.DELETED, field="deleted_at", new=now, at=now)
            self._conn.execute(
                "UPDATE bets SET deleted_at = ?, updated_at = ? WHERE id = ?", (now, now, bet.id)
            )
        return self.get_bet(bet.id)

    def restore(self, bet_id: str) -> BetRecord:
        bet = self.get_bet(bet_id)
        if not bet.is_deleted:
            return bet
        now = to_db(self._clock())
        with transaction(self._conn):
            self._audit(
                bet.id,
                _new_id(),
                AuditKind.RESTORED,
                field="deleted_at",
                old=bet.deleted_at,
                new=None,
                at=now,
            )
            self._conn.execute(
                "UPDATE bets SET deleted_at = NULL, updated_at = ? WHERE id = ?", (now, bet.id)
            )
        return self.get_bet(bet.id)

    def audit_trail(self, bet_id: str) -> list[AuditRow]:
        self.get_bet(bet_id)
        rows = self._conn.execute(
            "SELECT * FROM adjustments WHERE bet_id = ? ORDER BY at, rowid", (bet_id,)
        )
        return [
            AuditRow(
                id=r["id"],
                bet_id=r["bet_id"],
                action_id=r["action_id"],
                at=from_db(r["at"]),
                kind=AuditKind(r["kind"]),
                field=r["field"],
                old_value=r["old_value"],
                new_value=r["new_value"],
                reason=r["reason"],
            )
            for r in rows
        ]

    # ---------------------------------------------------------------- summary

    def summary(self, now: datetime | None = None) -> Summary:
        """The summary strip figures (SPEC §5.4). Buckets by Europe/London month (§12)."""
        moment = now or self._clock()
        month_start, month_end = local_month_bounds(moment)
        realised_sql = (
            "SELECT COALESCE(SUM(COALESCE(actual_pl_override_pence, actual_pl_pence)), 0),"
            " COUNT(*) FROM bets WHERE status IN ('SETTLED', 'VOID') AND deleted_at IS NULL"
        )
        all_time, settled_count = self._conn.execute(realised_sql).fetchone()
        this_month = self._conn.execute(
            realised_sql + " AND settled_at >= ? AND settled_at < ?",
            (to_db(month_start), to_db(month_end)),
        ).fetchone()[0]
        by_bookmaker: dict[str, Pence] = {}
        for name, total in self._conn.execute(
            "SELECT v.name, SUM(COALESCE(b.actual_pl_override_pence, b.actual_pl_pence))"
            " FROM bets b JOIN bet_legs l ON l.bet_id = b.id AND l.leg_index ="
            "   (SELECT MIN(leg_index) FROM bet_legs WHERE bet_id = b.id AND side = 'BACK')"
            " JOIN venues v ON v.id = l.venue_id"
            " WHERE b.status IN ('SETTLED', 'VOID') AND b.deleted_at IS NULL"
            " GROUP BY v.name ORDER BY v.name COLLATE NOCASE"
        ):
            by_bookmaker[name] = int(total)
        open_bets = self.list_bets(BetFilter(statuses=OPEN_STATUSES))
        liability: defaultdict[str, int] = defaultdict(int)
        open_guaranteed = 0
        for bet in open_bets:
            open_guaranteed += bet.live_guaranteed_pl
            for record in bet.lay_legs:
                if not record.leg.is_settled and record.leg.liability > 0:
                    liability[record.leg.venue] += record.leg.liability
        return Summary(
            realised_all_time=int(all_time),
            realised_this_month=int(this_month),
            open_guaranteed=open_guaranteed,
            open_liability_by_exchange=dict(sorted(liability.items())),
            realised_by_bookmaker=by_bookmaker,
            open_count=len(open_bets),
            settled_count=int(settled_count),
        )


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
