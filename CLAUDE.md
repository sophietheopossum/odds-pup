# CLAUDE.md — odds-pup

odds-pup is a small PySide6 desktop ledger for matched betting (qualifying bets, free bets, lay
hedges). This file is the working agreement for anyone, human or agent, editing the code. The
product and domain specification lives in `docs/SPEC.md` and is the source of truth: if this file
and the spec disagree, the spec wins and this file gets fixed.

## Read first

- `docs/SPEC.md`: domain model, exact P/L formulas, rounding rules, schema, state machine,
  golden fixtures, decisions log.
- `README.md`: what it is and how to run it.

## Layout

```
src/odds_pup/core      pure domain: money, odds, legs, P/L, templates. No Qt, no sqlite, no I/O.
src/odds_pup/storage   sqlite3 persistence, migrations, backups, CSV. No Qt.
src/odds_pup/ui        PySide6 widgets and models. The only package allowed to import Qt.
tests/                 pytest. The golden fixtures from SPEC §7 must always pass.
docs/                  SPEC.md and design notes.
```

## Hard rules

1. Money is integer pence (`int`) everywhere in core and storage. Never `float` for money or odds.
2. Odds are `decimal.Decimal`, greater than 1, at most 4 decimal places. Commission is integer
   basis points (200 = 2%).
3. Rounding happens only at the points named in SPEC §4, always `ROUND_HALF_UP`. Do not add
   rounding anywhere else.
4. Commission applies only to net positive exchange winnings, per venue, per market. It never
   touches the branch where the lay loses.
5. `core` and `storage` must not import PySide6 or Qt. `core` must not import sqlite3. A test
   enforces this.
6. Timestamps are stored as UTC strings `YYYY-MM-DDTHH:MM:SSZ`. Local-date bucketing (monthly
   totals) uses Europe/London.
7. No network code anywhere, no telemetry. The ledger is private data.
8. Every calculator change ships with a golden test in exact pence and, where sensible, a
   Hypothesis property.
9. Validate at the boundary: odds > 1, back stake > 0, lay stake >= 0, 0 <= commission_bp < 10000.
10. Storage writes are one transaction per user action. The audit table is append-only and
    triggers enforce that.

## Tooling

```sh
uv sync                                   # create .venv with all deps (PySide6 is large)
uv run odds-pup                           # launch the app
uv run pytest                             # tests
uv run ruff check . && uv run ruff format --check .
uv run mypy                               # strict on core, storage and tests
```

## Style

PEP 8 via ruff, line length 100. Type annotations on everything in core and storage. Prefer frozen
dataclasses for domain values, small pure functions over stateful classes. Docstrings say what and
why, not how.

## Git

Work on feature branches. The developer signs and merges to main. Commit subjects are imperative;
bodies explain why. Never commit `*.sqlite3`, backups or CSV exports (see `.gitignore`).

## When changing the spec

Edit `docs/SPEC.md` first, add or adjust the golden fixtures, then change the code. Record the
decision with its date in SPEC §15.
