# odds-pup

A lightweight desktop ledger for matched betting. Log qualifying bets, free bets (SNR and SR) and
their lay hedges, see the guaranteed profit or qualifying loss before you place, settle with the
real outcome, and keep an audit trail when a position is adjusted.

**Status:** pre-alpha. The specification is written (`docs/SPEC.md`) and the calculator core is
under test. The desktop UI and storage layer are in progress. Not yet usable as an app.

## What it does (v0.1 target)

- Calculator-plus-log: enter back stake, back odds, lay odds and commission; the app fills in the
  optimal lay stake (editable to what actually matched) and shows liability and the P/L for both
  outcomes.
- Bet types: qualifying, free bet SNR, free bet SR. Dutching is modelled in the schema but has no
  UI yet.
- Settle a bet by outcome (back won, lay won, void, cashed out). Realised P/L is derived, never
  typed in blind.
- Audited adjustments: manual overrides carry a reason and are logged in an append-only table.
- Ledger with filters (status, bookmaker, bet type, date range, free text) and a summary strip
  (all-time, this month, open exposure, per-bookmaker).
- CSV export. One SQLite file, automatic rotating backups.

## Non-goals (for now)

No live odds feeds, no account balance tracking, no sync or network access of any kind, GBP only,
no casino, each-way, Rule 4 or dead-heat handling. See `docs/SPEC.md` §1 for the full list.

## Run

Requires Python 3.13 or newer and [uv](https://docs.astral.sh/uv/).

```sh
uv sync            # first run downloads PySide6 (several hundred MB)
uv run odds-pup    # launch (UI not yet implemented)
uv run pytest      # tests
```

Data lives in `~/.local/share/odds-pup/` (override with `--data-dir` or `ODDS_PUP_DATA_DIR`). The
database is created with owner-only permissions and is git-ignored. A matched-betting ledger is
sensitive personal data: keep it out of shared folders and public repositories.

## Development

`CLAUDE.md` holds the working rules for humans and agents. `docs/SPEC.md` is the source of truth
for the domain model, exact formulas, rounding rules, schema and the decisions log.

## Licence

MIT.
