"""SQLite schema, migrations and the UK venue seed list. SPEC §11.2, §11.3, §9 D9."""

from __future__ import annotations

from odds_pup.core import VenueKind

SCHEMA_VERSION = 1

DDL_V1 = """
CREATE TABLE venues (
  id                    TEXT PRIMARY KEY,
  name                  TEXT NOT NULL COLLATE NOCASE UNIQUE,
  kind                  TEXT NOT NULL CHECK (kind IN ('BOOKMAKER', 'EXCHANGE')),
  default_commission_bp INTEGER NOT NULL DEFAULT 0
                        CHECK (default_commission_bp BETWEEN 0 AND 9999),
  min_stake_pence       INTEGER CHECK (min_stake_pence IS NULL OR min_stake_pence >= 0),
  ladder                TEXT CHECK (ladder IS NULL OR ladder IN ('BETFAIR')),
  created_at            TEXT NOT NULL
) STRICT;

CREATE TABLE offers (
  id         TEXT PRIMARY KEY,
  venue_id   TEXT REFERENCES venues(id),
  title      TEXT NOT NULL,
  notes      TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
) STRICT;

CREATE TABLE bets (
  id                       TEXT PRIMARY KEY,
  bet_type                 TEXT NOT NULL CHECK (bet_type IN
                             ('QUALIFYING', 'FREE_BET_SNR', 'FREE_BET_SR', 'DUTCHING')),
  status                   TEXT NOT NULL CHECK (status IN
                             ('OPEN', 'PARTIALLY_SETTLED', 'SETTLED', 'VOID')),
  event_name               TEXT NOT NULL,
  selection                TEXT NOT NULL DEFAULT '',
  market                   TEXT NOT NULL DEFAULT '',
  market_outcomes          INTEGER NOT NULL DEFAULT 2 CHECK (market_outcomes >= 2),
  offer_id                 TEXT REFERENCES offers(id),
  parent_bet_id            TEXT REFERENCES bets(id),
  currency                 TEXT NOT NULL DEFAULT 'GBP',
  placed_at                TEXT NOT NULL,
  event_at                 TEXT,
  settled_at               TEXT,
  expected_pl_pence        INTEGER NOT NULL,
  actual_pl_pence          INTEGER,
  actual_pl_override_pence INTEGER,
  needs_review             INTEGER NOT NULL DEFAULT 0 CHECK (needs_review IN (0, 1)),
  notes                    TEXT NOT NULL DEFAULT '',
  created_at               TEXT NOT NULL,
  updated_at               TEXT NOT NULL,
  deleted_at               TEXT
) STRICT;

CREATE TABLE bet_legs (
  id                   TEXT PRIMARY KEY,
  bet_id               TEXT NOT NULL REFERENCES bets(id),
  leg_index            INTEGER NOT NULL,
  side                 TEXT NOT NULL CHECK (side IN ('BACK', 'LAY')),
  venue_id             TEXT NOT NULL REFERENCES venues(id),
  selection_index      INTEGER NOT NULL DEFAULT 0 CHECK (selection_index >= 0),
  odds                 TEXT NOT NULL,
  odds_text            TEXT,
  stake_pence          INTEGER NOT NULL CHECK (stake_pence >= 0),
  stake_kind           TEXT NOT NULL CHECK (stake_kind IN ('CASH', 'FREE_SNR', 'FREE_SR')),
  commission_bp        INTEGER NOT NULL DEFAULT 0 CHECK (commission_bp BETWEEN 0 AND 9999),
  result               TEXT NOT NULL DEFAULT 'PENDING' CHECK (result IN
                         ('PENDING', 'WON', 'LOST', 'VOID', 'CASHED_OUT')),
  settled_amount_pence INTEGER,
  created_at           TEXT NOT NULL,
  UNIQUE (bet_id, leg_index),
  CHECK (side = 'BACK' OR stake_kind = 'CASH'),
  CHECK ((result = 'CASHED_OUT') = (settled_amount_pence IS NOT NULL))
) STRICT;

CREATE TABLE adjustments (
  id        TEXT PRIMARY KEY,
  bet_id    TEXT NOT NULL REFERENCES bets(id),
  action_id TEXT NOT NULL,
  at        TEXT NOT NULL,
  kind      TEXT NOT NULL CHECK (kind IN ('CREATED', 'EDITED', 'SETTLED', 'REOPENED',
                                          'ADJUSTMENT', 'CORRECTION', 'DELETED', 'RESTORED')),
  field     TEXT,
  old_value TEXT,
  new_value TEXT,
  reason    TEXT,
  CHECK (kind NOT IN ('ADJUSTMENT', 'CORRECTION')
         OR (reason IS NOT NULL AND length(trim(reason)) > 0))
) STRICT;

CREATE TRIGGER adjustments_append_only_u BEFORE UPDATE ON adjustments
  BEGIN SELECT RAISE(ABORT, 'adjustments is append-only'); END;
CREATE TRIGGER adjustments_append_only_d BEFORE DELETE ON adjustments
  BEGIN SELECT RAISE(ABORT, 'adjustments is append-only'); END;

CREATE INDEX bets_status_idx      ON bets(status, deleted_at);
CREATE INDEX bets_placed_idx      ON bets(placed_at);
CREATE INDEX bets_settled_idx     ON bets(settled_at);
CREATE INDEX bet_legs_bet_idx     ON bet_legs(bet_id);
CREATE INDEX bet_legs_venue_idx   ON bet_legs(venue_id);
CREATE INDEX adjustments_bet_idx  ON adjustments(bet_id, at);

PRAGMA user_version = 1;
"""

MIGRATIONS: tuple[tuple[int, str], ...] = ((1, DDL_V1),)
"""Forward-only, each applied in one transaction, in order (SPEC §11.3)."""

# name, kind, default commission (bp), minimum stake (pence) or None, ladder or None
SEED_VENUES: tuple[tuple[str, VenueKind, int, int | None, str | None], ...] = (
    ("Bet365", VenueKind.BOOKMAKER, 0, None, None),
    ("William Hill", VenueKind.BOOKMAKER, 0, None, None),
    ("Ladbrokes", VenueKind.BOOKMAKER, 0, None, None),
    ("Coral", VenueKind.BOOKMAKER, 0, None, None),
    ("Paddy Power", VenueKind.BOOKMAKER, 0, None, None),
    ("Betfair Sportsbook", VenueKind.BOOKMAKER, 0, None, None),
    ("Sky Bet", VenueKind.BOOKMAKER, 0, None, None),
    ("BetVictor", VenueKind.BOOKMAKER, 0, None, None),
    ("Unibet", VenueKind.BOOKMAKER, 0, None, None),
    ("Betway", VenueKind.BOOKMAKER, 0, None, None),
    ("888sport", VenueKind.BOOKMAKER, 0, None, None),
    ("Betfred", VenueKind.BOOKMAKER, 0, None, None),
    ("BoyleSports", VenueKind.BOOKMAKER, 0, None, None),
    ("Virgin Bet", VenueKind.BOOKMAKER, 0, None, None),
    ("LiveScore Bet", VenueKind.BOOKMAKER, 0, None, None),
    ("Betfair Exchange", VenueKind.EXCHANGE, 500, 200, "BETFAIR"),
    ("Smarkets", VenueKind.EXCHANGE, 200, 5, None),
    ("Betdaq", VenueKind.EXCHANGE, 200, None, None),
    ("Matchbook", VenueKind.EXCHANGE, 400, None, None),
)
