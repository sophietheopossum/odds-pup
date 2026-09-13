"""The ledger: one connection, one transaction per user action. SPEC §8-§11.

Every public method raises only :class:`odds_pup.core.CoreError` subclasses (bad input) or
:class:`odds_pup.storage.StorageError` subclasses (state, lock or database problems). Every
mutation reads the bet and checks legality *inside* its ``BEGIN IMMEDIATE`` transaction, so a
decision is never taken on stale state.
"""

from __future__ import annotations

import enum
import sqlite3
import uuid
from collections import defaultdict
from collections.abc import Mapping
from contextlib import suppress
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from odds_pup.core import (
    LADDERS,
    MAX_MONEY_PENCE,
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
from odds_pup.core.model import validate_commission_bp, validate_pence
from odds_pup.storage.backup import create_backup
from odds_pup.storage.database import (
    check_version,
    connect,
    enable_wal,
    migrate,
    tighten_permissions,
    transaction,
    translated,
)
from odds_pup.storage.errors import IllegalActionError, NotFoundError, StorageError, translate
from odds_pup.storage.lock import InstanceLock
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
from odds_pup.storage.timestamps import (
    from_db,
    local_month_bounds,
    require_aware,
    to_db,
    utc_now,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence
    from pathlib import Path


class _Unset(enum.Enum):
    token = 0


UNSET = _Unset.token
"""Sentinel for "leave this field alone" in update calls."""

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
_HEADER_FIELDS = (
    "bet_type",
    "event_name",
    "selection",
    "market",
    "market_outcomes",
    "offer_id",
    "parent_bet_id",
    "placed_at",
    "event_at",
    "notes",
)
_CORRECTION_ONLY_FIELDS = ("settled_at",)

type ResultSpec = LegResult | tuple[LegResult, Pence | None]


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


def _require_text(value: object, name: str, *, allow_empty: bool = True) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{name} must be text, not {type(value).__name__}")
    if not allow_empty and not value.strip():
        raise ValidationError(f"{name} must not be empty")
    return value


def _require_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{name} must be an int, not {type(value).__name__}")
    return value


def _enum[E: enum.Enum](kind: type[E], value: object, name: str) -> E:
    try:
        return kind(value)
    except ValueError as exc:
        raise ValidationError(f"{name} {value!r} is not one of {[m.value for m in kind]}") from exc


def _parse_results(results: object) -> dict[int, tuple[LegResult, Pence | None]]:
    if not isinstance(results, Mapping) or not results:
        raise ValidationError("nothing to settle")
    parsed: dict[int, tuple[LegResult, Pence | None]] = {}
    for key, spec in results.items():
        index = _require_int(key, "leg index")
        raw_result, amount = spec if isinstance(spec, tuple) else (spec, None)
        result = _enum(LegResult, raw_result, "result")
        if result is LegResult.PENDING:
            raise ValidationError("PENDING is not a settlement result")
        if amount is not None:
            validate_pence(amount, name="settled amount", minimum=-MAX_MONEY_PENCE)
        parsed[index] = (result, amount)
    return parsed


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class Repository:
    """All reads and writes against one ledger database."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        clock: Callable[[], datetime] = utc_now,
        paths: DataPaths | None = None,
        lock: InstanceLock | None = None,
    ) -> None:
        self._conn = connection
        self._clock = clock
        self._lock = lock
        self.paths = paths

    # ----------------------------------------------------------------- lifecycle

    @classmethod
    def open(
        cls,
        paths: DataPaths,
        *,
        clock: Callable[[], datetime] = utc_now,
        backups: bool = True,
        lock: bool = True,
    ) -> Repository:
        """Lock, connect, check version, back up, migrate, seed (SPEC §11.3-§11.6).

        Holds the single-instance lock until :meth:`close` unless ``lock`` is false. A database
        from a newer odds-pup is refused before anything writes to it. On any failure the
        connection is closed and the lock released.
        """
        prepare(paths)
        instance_lock = InstanceLock(paths.lock) if lock else None
        if instance_lock is not None:
            instance_lock.acquire()
        connection: sqlite3.Connection | None = None
        try:
            connection = connect(paths.database)
            check_version(connection)
            tighten_permissions(paths.database)
            enable_wal(connection)
            repo = cls(connection, clock=clock, paths=paths, lock=instance_lock)
            opened = connection

            def backup() -> None:
                create_backup(opened, paths.backups, now=clock())

            migrate(connection, before_migration=backup if backups else None)
            repo.seed_venues()
            if backups:
                backup()
            tighten_permissions(paths.database)
        except BaseException as exc:
            if connection is not None:
                connection.close()
            if instance_lock is not None:
                instance_lock.release()
            if isinstance(exc, sqlite3.Error):
                raise translate(exc) from exc
            raise
        return repo

    def close(self) -> None:
        try:
            with suppress(sqlite3.Error):
                self._conn.execute("PRAGMA optimize")
            self._conn.close()
        finally:
            if self._lock is not None:
                self._lock.release()

    def backup(self) -> Path:
        if self.paths is None:
            raise StorageError("this repository has no data directory to back up into")
        with translated():
            return create_backup(self._conn, self.paths.backups, now=self._clock())

    # -------------------------------------------------------------------- venues

    @staticmethod
    def _venue(row: sqlite3.Row) -> Venue:
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
            params = (_enum(VenueKind, kind, "venue kind").value,)
        sql += " ORDER BY name COLLATE NOCASE"
        with translated():
            return [self._venue(r) for r in self._conn.execute(sql, params)]

    def get_venue(self, venue_id: str) -> Venue:
        with translated():
            row = self._conn.execute("SELECT * FROM venues WHERE id = ?", (venue_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"no venue {venue_id}")
        return self._venue(row)

    def find_venue(self, name: str) -> Venue | None:
        """Case-insensitive lookup by name (the column collates NOCASE)."""
        cleaned = _require_text(name, "venue name").strip()
        with translated():
            row = self._conn.execute("SELECT * FROM venues WHERE name = ?", (cleaned,)).fetchone()
        return None if row is None else self._venue(row)

    def venue_usage(self, venue_id: str) -> int:
        """How many legs and offers refer to the venue (0 means it can be deleted)."""
        with translated():
            legs = self._conn.execute(
                "SELECT COUNT(*) FROM bet_legs WHERE venue_id = ?", (venue_id,)
            ).fetchone()[0]
            offers = self._conn.execute(
                "SELECT COUNT(*) FROM offers WHERE venue_id = ?", (venue_id,)
            ).fetchone()[0]
        return int(legs) + int(offers)

    @staticmethod
    def _validate_venue_fields(
        default_commission_bp: int, min_stake_pence: Pence | None, ladder: str | None
    ) -> None:
        validate_commission_bp(default_commission_bp)
        if min_stake_pence is not None:
            validate_pence(min_stake_pence, name="minimum stake")
        if ladder is not None and ladder not in LADDERS:
            raise ValidationError(f"unknown price ladder {ladder!r}; known: {sorted(LADDERS)}")

    def _insert_venue(
        self,
        name: str,
        kind: VenueKind,
        default_commission_bp: int,
        min_stake_pence: Pence | None,
        ladder: str | None,
    ) -> Venue:
        cleaned = _require_text(name, "venue name", allow_empty=False).strip()
        kind = _enum(VenueKind, kind, "venue kind")
        self._validate_venue_fields(default_commission_bp, min_stake_pence, ladder)
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
        kind = _enum(VenueKind, kind, "venue kind")
        with transaction(self._conn):
            existing = self.find_venue(name)
            if existing is not None:
                if existing.kind is not kind:
                    raise IllegalActionError(
                        f"{existing.name} is a {existing.kind.value.lower()}, "
                        f"not a {kind.value.lower()}"
                    )
                return existing
            return self._insert_venue(name, kind, default_commission_bp, min_stake_pence, ladder)

    def update_venue(
        self,
        venue_id: str,
        *,
        name: str | _Unset = UNSET,
        kind: VenueKind | _Unset = UNSET,
        default_commission_bp: int | _Unset = UNSET,
        min_stake_pence: Pence | _Unset | None = UNSET,
        ladder: str | _Unset | None = UNSET,
    ) -> Venue:
        """Change a venue. Renaming fixes a typo everywhere (legs refer to the venue by id);
        the kind can only change while nothing refers to the venue."""
        with transaction(self._conn):
            current = self.get_venue(venue_id)
            new_name = current.name
            if not isinstance(name, _Unset):
                new_name = _require_text(name, "venue name", allow_empty=False).strip()
                clash = self.find_venue(new_name)
                if clash is not None and clash.id != current.id:
                    raise IllegalActionError(f"a venue called {clash.name} already exists")
            new_kind = current.kind if isinstance(kind, _Unset) else _enum(VenueKind, kind, "kind")
            if new_kind is not current.kind and self.venue_usage(current.id):
                raise IllegalActionError(
                    f"{current.name} is used by bets or offers, so its kind cannot change"
                )
            bp = (
                current.default_commission_bp
                if isinstance(default_commission_bp, _Unset)
                else default_commission_bp
            )
            stake = (
                current.min_stake_pence if isinstance(min_stake_pence, _Unset) else min_stake_pence
            )
            new_ladder = current.ladder if isinstance(ladder, _Unset) else ladder
            self._validate_venue_fields(bp, stake, new_ladder)
            self._conn.execute(
                "UPDATE venues SET name = ?, kind = ?, default_commission_bp = ?,"
                " min_stake_pence = ?, ladder = ? WHERE id = ?",
                (new_name, new_kind.value, bp, stake, new_ladder, venue_id),
            )
        return self.get_venue(venue_id)

    def delete_venue(self, venue_id: str) -> None:
        """Delete a venue nothing refers to (a typo made through autocomplete)."""
        with transaction(self._conn):
            venue = self.get_venue(venue_id)
            if self.venue_usage(venue.id):
                raise IllegalActionError(
                    f"{venue.name} is used by bets or offers; rename it instead of deleting it"
                )
            self._conn.execute("DELETE FROM venues WHERE id = ?", (venue.id,))

    def seed_venues(self) -> int:
        """Insert the UK seed list into an empty venues table. Returns how many were added."""
        with transaction(self._conn):
            count = self._conn.execute("SELECT COUNT(*) FROM venues").fetchone()[0]
            if count:
                return 0
            for name, kind, bp, min_stake, ladder in SEED_VENUES:
                self._insert_venue(name, kind, bp, min_stake, ladder)
        return len(SEED_VENUES)

    # -------------------------------------------------------------------- offers

    @staticmethod
    def _offer(row: sqlite3.Row) -> Offer:
        return Offer(
            id=row["id"],
            venue_id=row["venue_id"],
            title=row["title"],
            notes=row["notes"],
            created_at=from_db(row["created_at"]),
        )

    def create_offer(self, title: str, *, venue_id: str | None = None, notes: str = "") -> Offer:
        cleaned = _require_text(title, "offer title", allow_empty=False).strip()
        _require_text(notes, "notes")
        offer_id = _new_id()
        with transaction(self._conn):
            if venue_id is not None:
                self.get_venue(venue_id)
            self._conn.execute(
                "INSERT INTO offers (id, venue_id, title, notes, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (offer_id, venue_id, cleaned, notes, to_db(self._clock())),
            )
        return self.get_offer(offer_id)

    def get_offer(self, offer_id: str) -> Offer:
        with translated():
            row = self._conn.execute("SELECT * FROM offers WHERE id = ?", (offer_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"no offer {offer_id}")
        return self._offer(row)

    def list_offers(self) -> list[Offer]:
        with translated():
            rows = self._conn.execute("SELECT * FROM offers ORDER BY created_at DESC, title")
            return [self._offer(r) for r in rows]

    # ---------------------------------------------------------------- bet reads

    def _bet_rows(self, sql: str, params: Sequence[object]) -> list[BetRecord]:
        with translated():
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
        _require_text(bet_id, "bet id")
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
            params.extend(sorted(_enum(BetStatus, s, "status").value for s in f.statuses))
        if f.bet_types is not None:
            if not f.bet_types:
                return []
            clauses.append(f"b.bet_type IN ({','.join('?' * len(f.bet_types))})")
            params.extend(sorted(_enum(BetType, t, "bet type").value for t in f.bet_types))
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
                # Non-correlated: evaluated once. The correlated EXISTS form took ~10 s on a
                # 10k-bet ledger because the planner walked every leg at the venue per bet.
                clauses.append(
                    "b.id IN (SELECT l.bet_id FROM bet_legs l JOIN venues v ON v.id = l.venue_id"
                    " WHERE l.side = ? AND v.name = ?)"
                )
                params.extend([side.value, name.strip()])
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        limit = ""
        if f.limit is not None:
            limit = " LIMIT ?"
            params.append(max(0, _require_int(f.limit, "limit")))
        return self._bet_rows(
            f"SELECT b.* FROM bets b{where}"
            f" ORDER BY b.placed_at DESC, b.created_at DESC, b.id{limit}",
            params,
        )

    def audit_trail(self, bet_id: str) -> list[AuditRow]:
        self.get_bet(bet_id)
        with translated():
            rows = self._conn.execute(
                "SELECT * FROM adjustments WHERE bet_id = ? ORDER BY at, rowid", (bet_id,)
            ).fetchall()
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

    # --------------------------------------------------------------- bet writes

    def _resolve_leg(self, new: NewLeg) -> tuple[Venue, Leg]:
        """Turn form input into a venue row and a validated domain leg (inside a transaction)."""
        if not isinstance(new, NewLeg):
            raise ValidationError(f"legs must be NewLeg values, not {type(new).__name__}")
        side = _enum(Side, new.side, "side")
        venue = self.find_venue(_require_text(new.venue, "venue", allow_empty=False))
        if venue is None:
            kind = VenueKind.EXCHANGE if side is Side.LAY else VenueKind.BOOKMAKER
            # a new exchange remembers the commission it was first used with
            default_bp = new.commission_bp or 0 if kind is VenueKind.EXCHANGE else 0
            venue = self._insert_venue(new.venue, kind, default_bp, None, None)
        elif side is Side.LAY and venue.kind is not VenueKind.EXCHANGE:
            raise IllegalActionError(
                f"cannot lay at {venue.name}: it is a bookmaker, not an exchange"
            )
        if new.commission_bp is not None:
            commission_bp = new.commission_bp
        else:
            commission_bp = venue.default_commission_bp if venue.kind is VenueKind.EXCHANGE else 0
        leg = Leg(
            side=side,
            venue=venue.name,
            odds=new.odds,
            stake=new.stake,
            selection=new.selection,
            stake_kind=_enum(StakeKind, new.stake_kind, "stake kind"),
            commission_bp=commission_bp,
            result=_enum(LegResult, new.result, "result"),
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
        at: str,
    ) -> None:
        self._conn.execute(
            "INSERT INTO adjustments (id, bet_id, action_id, at, kind, field, old_value,"
            " new_value, reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (_new_id(), bet_id, action_id, at, kind.value, field, _text(old), _text(new), reason),
        )

    def _load(self, bet_id: str) -> BetRecord:
        """Read a bet inside the caller's transaction."""
        return self.get_bet(bet_id)

    def _check_references(
        self, offer_id: object, parent_bet_id: object, *, self_id: str | None = None
    ) -> None:
        if offer_id is not None:
            self.get_offer(_require_text(offer_id, "offer id"))
        if parent_bet_id is not None:
            parent = _require_text(parent_bet_id, "parent bet id")
            if parent == self_id:
                raise ValidationError("a bet cannot be its own parent")
            self.get_bet(parent)

    def create_bet(self, new: NewBet) -> BetRecord:
        """Insert a bet with its legs; legs carrying results make it a backfilled bet (§8.5)."""
        if not isinstance(new, NewBet):
            raise ValidationError("create_bet needs a NewBet")
        bet_type = _enum(BetType, new.bet_type, "bet type")
        event_name = _require_text(new.event_name, "event name", allow_empty=False).strip()
        for name in ("selection", "market", "notes"):
            _require_text(getattr(new, name), name)
        if not new.legs:
            raise ValidationError("a bet needs at least one leg")
        now_dt = self._clock()
        now = to_db(now_dt)
        placed_at = require_aware(new.placed_at or now_dt, "placed_at")
        event_at = None if new.event_at is None else require_aware(new.event_at, "event_at")
        given_settled_at = (
            None if new.settled_at is None else require_aware(new.settled_at, "settled_at")
        )
        bet_id = _new_id()
        with transaction(self._conn):
            self._check_references(new.offer_id, new.parent_bet_id)
            resolved = [self._resolve_leg(leg) for leg in new.legs]
            core_legs = tuple(leg for _, leg in resolved)
            validate_position(core_legs, new.market_outcomes)
            status = status_of(core_legs)
            expected = guaranteed_pl(
                tuple(leg.reopened() for leg in core_legs), new.market_outcomes
            )
            settled_at = None
            if status in SETTLED_STATUSES:
                settled_at = given_settled_at or require_aware(now_dt)
            self._conn.execute(
                "INSERT INTO bets (id, bet_type, status, event_name, selection, market,"
                " market_outcomes, offer_id, parent_bet_id, currency, placed_at, event_at,"
                " settled_at, expected_pl_pence, actual_pl_pence, actual_pl_override_pence,"
                " needs_review, notes, created_at, updated_at, deleted_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'GBP', ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, NULL)",
                (
                    bet_id,
                    bet_type.value,
                    status.value,
                    event_name,
                    new.selection,
                    new.market,
                    new.market_outcomes,
                    new.offer_id,
                    new.parent_bet_id,
                    to_db(placed_at),
                    None if event_at is None else to_db(event_at),
                    None if settled_at is None else to_db(settled_at),
                    expected,
                    realised_pl(core_legs),
                    1 if new.needs_review else 0,
                    new.notes,
                    now,
                    now,
                ),
            )
            self._insert_legs(bet_id, resolved, now)
            backfilled = any(leg.is_settled for leg in core_legs)
            self._audit(
                bet_id,
                _new_id(),
                AuditKind.CREATED,
                new="backfill" if backfilled else None,
                at=now,
            )
        return self.get_bet(bet_id)

    # -- edit / correct ---------------------------------------------------------

    def edit_bet(self, bet_id: str, **changes: Any) -> BetRecord:
        """Change an OPEN bet's parameters; the placement expectation is recomputed (§9.2)."""
        with transaction(self._conn):
            bet = self._load(bet_id)
            if bet.status is not BetStatus.OPEN:
                raise IllegalActionError(
                    "only an OPEN bet can be edited; use correct_bet with a reason"
                )
            self._apply_changes(bet, AuditKind.EDITED, None, changes)
        return self.get_bet(bet_id)

    def correct_bet(self, bet_id: str, *, reason: str, **changes: Any) -> BetRecord:
        """Fix a data-entry error on a bet that has left OPEN; expectation stays frozen (§9.2).

        Leg results are not correctable (use :meth:`reopen`); ``settled_at`` is, on a SETTLED or
        VOID bet, so a mistyped backfill date can be put right.
        """
        if not isinstance(reason, str) or not reason.strip():
            raise ValidationError("a correction needs a reason")
        with transaction(self._conn):
            bet = self._load(bet_id)
            if bet.status is BetStatus.OPEN:
                raise IllegalActionError("an OPEN bet is edited, not corrected")
            self._apply_changes(bet, AuditKind.CORRECTION, reason.strip(), changes)
        return self.get_bet(bet_id)

    def _header_changes(
        self, bet: BetRecord, changes: Mapping[str, Any], kind: AuditKind
    ) -> list[tuple[str, object, object]]:
        allowed = set(_HEADER_FIELDS) | {"legs"}
        if kind is AuditKind.CORRECTION:
            allowed |= set(_CORRECTION_ONLY_FIELDS)
        unknown = set(changes) - allowed
        if unknown:
            raise ValidationError(f"unknown or uncorrectable bet fields: {sorted(unknown)}")
        header: list[tuple[str, object, object]] = []
        for name in (*_HEADER_FIELDS, *_CORRECTION_ONLY_FIELDS):
            if name not in changes or isinstance(changes[name], _Unset):
                continue
            value = changes[name]
            if name == "bet_type":
                value = _enum(BetType, value, "bet type")
            elif name == "event_name":
                value = _require_text(value, "event name", allow_empty=False).strip()
            elif name in ("selection", "market", "notes"):
                value = _require_text(value, name)
            elif name == "market_outcomes":
                value = _require_int(value, "market_outcomes")
            elif name in ("offer_id", "parent_bet_id") and value is not None:
                value = _require_text(value, name)
            elif name == "placed_at":
                value = require_aware(value, "placed_at")
            elif name == "event_at" and value is not None:
                value = require_aware(value, "event_at")
            elif name == "settled_at":
                if bet.status not in SETTLED_STATUSES:
                    raise IllegalActionError("settled_at can only be corrected on a settled bet")
                value = require_aware(value, "settled_at")
            old = getattr(bet, name)
            if value != old:
                header.append((name, old, value))
        return header

    def _plan_legs(
        self,
        bet: BetRecord,
        new_legs: Sequence[NewLeg],
        kind: AuditKind,
        market_outcomes: int,
    ) -> tuple[list[tuple[Venue, Leg]], list[tuple[str, object, object]]]:
        """Resolve new legs and list the per-field differences, without touching bet_legs."""
        if any(
            not isinstance(leg, NewLeg)
            or _enum(LegResult, leg.result, "result") is not LegResult.PENDING
            or leg.settled_amount is not None
            for leg in new_legs
        ):
            if kind is AuditKind.EDITED:
                raise IllegalActionError("settle a bet with settle(), not by editing legs")
            raise IllegalActionError("leg results cannot be corrected; reopen the leg instead")
        if kind is AuditKind.CORRECTION and len(new_legs) != len(bet.legs):
            raise IllegalActionError(
                "a correction cannot add or remove legs; reopen and recreate the bet"
            )
        resolved: list[tuple[Venue, Leg]] = []
        for index, new_leg in enumerate(new_legs):
            venue, leg = self._resolve_leg(new_leg)
            if index < len(bet.legs):  # keep recorded results: they are not editable here
                stored = bet.legs[index].leg
                leg = leg.settled(stored.result, stored.settled_amount)
            resolved.append((venue, leg))
        validate_position(tuple(leg for _, leg in resolved), market_outcomes)
        diffs: list[tuple[str, object, object]] = []
        for index, (_, leg) in enumerate(resolved):
            old_leg = bet.legs[index].leg if index < len(bet.legs) else None
            for field_name in _LEG_FIELDS:
                old_v = None if old_leg is None else getattr(old_leg, field_name)
                new_v = getattr(leg, field_name)
                if old_v != new_v:
                    diffs.append((f"legs[{index}].{field_name}", old_v, new_v))
        diffs.extend(
            (f"legs[{index}]", "present", "removed")
            for index in range(len(resolved), len(bet.legs))
        )
        return resolved, diffs

    def _apply_changes(
        self,
        bet: BetRecord,
        kind: AuditKind,
        reason: str | None,
        changes: Mapping[str, Any],
    ) -> None:
        """Write header and leg changes inside the caller's transaction; no-ops write nothing."""
        if bet.is_deleted:
            raise IllegalActionError("restore the bet before changing it")
        header = self._header_changes(bet, changes, kind)
        market_outcomes = bet.market_outcomes
        for name, _, value in header:
            if name == "market_outcomes":
                market_outcomes = int(value)  # type: ignore[call-overload]
            elif name in ("offer_id", "parent_bet_id"):
                self._check_references(
                    value if name == "offer_id" else None,
                    value if name == "parent_bet_id" else None,
                    self_id=bet.id,
                )
        raw_legs = changes.get("legs")
        resolved: list[tuple[Venue, Leg]] | None = None
        leg_diffs: list[tuple[str, object, object]] = []
        if raw_legs is not None and not isinstance(raw_legs, _Unset):
            resolved, leg_diffs = self._plan_legs(bet, list(raw_legs), kind, market_outcomes)
        core_legs = (
            bet.core_legs
            if not leg_diffs or resolved is None
            else tuple(leg for _, leg in resolved)
        )
        validate_position(core_legs, market_outcomes)
        expected = bet.expected_pl_pence
        if kind is AuditKind.EDITED:
            expected = guaranteed_pl(tuple(leg.reopened() for leg in core_legs), market_outcomes)
        if not header and not leg_diffs and expected == bet.expected_pl_pence:
            return
        now = to_db(self._clock())
        action_id = _new_id()
        for field_name, old, new in (*leg_diffs, *header):
            self._audit(
                bet.id, action_id, kind, field=field_name, old=old, new=new, reason=reason, at=now
            )
        if expected != bet.expected_pl_pence:
            self._audit(
                bet.id,
                action_id,
                kind,
                field="expected_pl_pence",
                old=bet.expected_pl_pence,
                new=expected,
                reason=reason,
                at=now,
            )
        if leg_diffs and resolved is not None:
            self._conn.execute("DELETE FROM bet_legs WHERE bet_id = ?", (bet.id,))
            self._insert_legs(bet.id, resolved, now)
        values: dict[str, object] = {}
        for name, _, value in header:
            if isinstance(value, datetime):
                values[name] = to_db(value)
            elif isinstance(value, enum.Enum):
                values[name] = value.value
            else:
                values[name] = value
        values.update(
            status=status_of(core_legs).value,
            expected_pl_pence=expected,
            actual_pl_pence=realised_pl(core_legs),
            updated_at=now,
        )
        assignments = ", ".join(f"{k} = ?" for k in values)
        self._conn.execute(
            f"UPDATE bets SET {assignments} WHERE id = ?", (*values.values(), bet.id)
        )

    # -- settle / reopen / adjust ---------------------------------------------------

    def _settle(
        self,
        bet: BetRecord,
        results: Mapping[int, tuple[LegResult, Pence | None]],
        settled_at: datetime | None,
    ) -> None:
        if bet.is_deleted:
            raise IllegalActionError("restore the bet before settling it")
        legs = list(bet.core_legs)
        now_dt = self._clock()
        now = to_db(now_dt)
        action_id = _new_id()
        for index, (result, amount) in results.items():
            if not 0 <= index < len(legs):
                raise NotFoundError(f"bet has no leg {index}")
            if legs[index].is_settled:
                raise IllegalActionError(f"leg {index} already has a result; reopen it first")
            legs[index] = legs[index].settled(result, amount)
        for index, (result, amount) in results.items():
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
                    new=amount,
                    at=now,
                )
        status = status_of(legs)
        settled_text = None
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
            (status.value, realised_pl(legs), settled_text, now, bet.id),
        )

    def settle(
        self,
        bet_id: str,
        results: Mapping[int, ResultSpec],
        *,
        settled_at: datetime | None = None,
    ) -> BetRecord:
        """Record results for pending legs by index; derive status and realised P/L (§8.3).

        ``settled_at`` is used only if this call completes the bet. CASHED_OUT takes the leg's
        *net* amount; convert a bookmaker's return with :meth:`Leg.cashed_out_from_return`.
        """
        parsed = _parse_results(results)
        when = None if settled_at is None else require_aware(settled_at, "settled_at")
        with transaction(self._conn):
            self._settle(self._load(bet_id), parsed, when)
        return self.get_bet(bet_id)

    def settle_two_way(
        self, bet_id: str, outcome: TwoWayOutcome, *, settled_at: datetime | None = None
    ) -> BetRecord:
        """The Settle dialog's three buttons for a one-back-one-lay bet (§8.3)."""
        chosen = _enum(TwoWayOutcome, outcome, "outcome")
        when = None if settled_at is None else require_aware(settled_at, "settled_at")
        with transaction(self._conn):
            bet = self._load(bet_id)
            if [r.leg.side for r in bet.legs] != [Side.BACK, Side.LAY]:
                raise IllegalActionError(
                    "settle_two_way needs a bet with exactly one back then one lay leg"
                )
            if any(r.leg.is_settled for r in bet.legs):
                raise IllegalActionError("a leg already has a result; reopen it first")
            back, lay = core_settle_two_way(bet.legs[0].leg, bet.legs[1].leg, chosen)
            self._settle(bet, {0: (back.result, None), 1: (lay.result, None)}, when)
        return self.get_bet(bet_id)

    def reopen(self, bet_id: str, leg_indexes: Iterable[int] | None = None) -> BetRecord:
        """Return settled legs to PENDING; clears realised figures and any override (§9.2)."""
        wanted = (
            None if leg_indexes is None else [_require_int(i, "leg index") for i in leg_indexes]
        )
        with transaction(self._conn):
            bet = self._load(bet_id)
            if bet.is_deleted:
                raise IllegalActionError("restore the bet before reopening it")
            if bet.status is BetStatus.OPEN:
                raise IllegalActionError("the bet is already open")
            legs = list(bet.core_legs)
            indexes = (
                [i for i, leg in enumerate(legs) if leg.is_settled] if wanted is None else wanted
            )
            if not indexes:
                raise ValidationError("no legs to reopen")
            for index in indexes:
                if not 0 <= index < len(legs):
                    raise NotFoundError(f"bet has no leg {index}")
                if not legs[index].is_settled:
                    raise IllegalActionError(f"leg {index} is not settled")
            now = to_db(self._clock())
            action_id = _new_id()
            for index in indexes:
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
                        at=now,
                    )
            status = status_of(legs)
            if bet.actual_pl_override_pence is not None:
                self._audit(
                    bet.id,
                    action_id,
                    AuditKind.REOPENED,
                    field="actual_pl_override_pence",
                    old=bet.actual_pl_override_pence,
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
                " settled_at = NULL, updated_at = ? WHERE id = ?",
                (status.value, realised_pl(legs), now, bet.id),
            )
        return self.get_bet(bet_id)

    def adjust(self, bet_id: str, override_pence: Pence | None, reason: str) -> BetRecord:
        """Audited manual override of the realised P/L on a settled bet (§8.4)."""
        if not isinstance(reason, str) or not reason.strip():
            raise ValidationError("an adjustment needs a reason")
        if override_pence is not None:
            validate_pence(override_pence, name="override", minimum=-MAX_MONEY_PENCE)
        with transaction(self._conn):
            bet = self._load(bet_id)
            if bet.is_deleted:
                raise IllegalActionError("restore the bet before adjusting it")
            if bet.status not in SETTLED_STATUSES:
                raise IllegalActionError("only a SETTLED or VOID bet can be adjusted")
            if override_pence == bet.actual_pl_override_pence:
                return bet
            now = to_db(self._clock())
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
        return self.get_bet(bet_id)

    def set_needs_review(self, bet_id: str, flag: bool, reason: str | None = None) -> BetRecord:
        """Toggle the user's review flag; allowed in any status on a bet that is not deleted."""
        if not isinstance(flag, bool):
            raise ValidationError("the review flag must be True or False")
        if reason is not None:
            _require_text(reason, "reason")
        with transaction(self._conn):
            bet = self._load(bet_id)
            if bet.is_deleted:
                raise IllegalActionError("restore the bet before flagging it")
            if bet.needs_review == flag:
                return bet
            now = to_db(self._clock())
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
        return self.get_bet(bet_id)

    def soft_delete(self, bet_id: str) -> BetRecord:
        with transaction(self._conn):
            bet = self._load(bet_id)
            if bet.is_deleted:
                return bet
            now = to_db(self._clock())
            self._audit(bet.id, _new_id(), AuditKind.DELETED, field="deleted_at", new=now, at=now)
            self._conn.execute(
                "UPDATE bets SET deleted_at = ?, updated_at = ? WHERE id = ?", (now, now, bet.id)
            )
        return self.get_bet(bet_id)

    def restore(self, bet_id: str) -> BetRecord:
        with transaction(self._conn):
            bet = self._load(bet_id)
            if not bet.is_deleted:
                return bet
            now = to_db(self._clock())
            self._audit(
                bet.id,
                _new_id(),
                AuditKind.RESTORED,
                field="deleted_at",
                old=bet.deleted_at,
                at=now,
            )
            self._conn.execute(
                "UPDATE bets SET deleted_at = NULL, updated_at = ? WHERE id = ?", (now, bet.id)
            )
        return self.get_bet(bet_id)

    # ------------------------------------------------------------------- summary

    def summary(self, now: datetime | None = None) -> Summary:
        """The summary strip figures (SPEC §5.4). Buckets by Europe/London month (§12)."""
        moment = require_aware(now or self._clock(), "now")
        month_start, month_end = local_month_bounds(moment)
        realised_sql = (
            "SELECT COALESCE(SUM(COALESCE(actual_pl_override_pence, actual_pl_pence)), 0),"
            " COUNT(*) FROM bets WHERE status IN ('SETTLED', 'VOID') AND deleted_at IS NULL"
        )
        with translated():
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
