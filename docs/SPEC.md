# odds-pup — Specification

Version: 1 (2026-09-12). This document is the source of truth for the domain model, the
profit/loss mathematics, rounding, persistence and the state machine. Code follows this document;
when they disagree, fix one of them and record the decision in §15.

Contents

1. Purpose, scope, non-goals
2. Glossary
3. Domain model
4. Money, odds, commission, rounding
5. Profit/loss model
6. Bet-type templates and optimal lay stakes
7. Golden fixtures
8. Settlement and realised P/L
9. State machine
10. Audit trail
11. Storage
12. Time and locale
13. User interface (v0.1)
14. Validation and warnings
15. Decisions log
16. Later (v0.2+)

---

## 1. Purpose, scope, non-goals

odds-pup is a single-user desktop ledger for matched betting. It is a **calculator plus log**: the
user enters the back and lay parameters, the app pre-fills the optimal lay stake, shows the P/L for
every outcome before the bet is placed, records the bet, settles it with the real outcome, and keeps
an append-only audit trail of every later change.

**v0.1 scope**

- Bet types with a UI: `QUALIFYING`, `FREE_BET_SNR`, `FREE_BET_SR` (one back leg, one lay leg).
- `DUTCHING` exists in the model and schema (n back legs, no lay) but has no UI in v0.1.
- New bet dialog with live calculator; ledger with filters and text search; settle dialog; adjust
  (audited manual override) dialog; summary strip; CSV export; backfill of historical settled bets.
- One SQLite file per data directory, rotating backups, single-instance lock.
- UK only: GBP, decimal odds canonical with fractional input accepted, UK venue seed list.

**Non-goals (v0.1)**

- No network access of any kind, no live odds, no oddsmatcher integration, no telemetry.
- No bookmaker/exchange balance or bankroll tracking.
- No multi-currency, no tax reporting, no sync between machines.
- No each-way, Rule 4, dead-heat, or casino/bingo offers.
- No undo stack (soft delete plus the audit trail cover mistakes).
- No modelling of second exchange bets (lock-ins, top-ups) in the UI; the schema supports them.

---

## 2. Glossary

| Term | Meaning |
|---|---|
| Back | A bet that a selection wins, placed at a bookmaker (or an exchange). |
| Lay | A bet that a selection does not win, placed at an exchange. The layer is liable for `L × (O − 1)`. |
| Selection | The thing backed or laid within a market (a team, a horse, "over 2.5"). |
| Market | A set of mutually exclusive outcomes; exactly one occurs. |
| Qualifying bet (QL) | A cash back bet placed to unlock an offer, hedged with a lay. Its equalised P/L is the qualifying loss, usually negative. |
| Free bet SNR | Stake Not Returned: only winnings are paid; the free stake itself is never returned. |
| Free bet SR | Stake Returned: the free stake is paid back with any winnings, as if it were cash. |
| Dutching | Backing every (or several) outcomes of a market at different venues so the return is similar whichever wins. |
| Commission | The exchange's cut of net winnings in a market. Stored in basis points (bp); 200 bp = 2%. |
| Liability | The amount an exchange holds against a lay: `L × (O − 1)`. |
| Guaranteed P/L | The minimum P/L across all outcomes of the position. |
| Rating | The percentage of stake retained (QL, SR) or converted to cash (SNR). See §6. |

Symbols used below: `S` back stake, `B` back odds, `L` lay stake, `O` lay odds,
`c = commission_bp / 10000`.

---

## 3. Domain model

A **Bet** is a position in one market. It has a header and one or more **Legs**. A bet's P/L is a
vector with one entry per market outcome, computed from its legs (§5).

### 3.1 Enumerations

```
BetType     QUALIFYING | FREE_BET_SNR | FREE_BET_SR | DUTCHING
BetStatus   OPEN | PARTIALLY_SETTLED | SETTLED | VOID          (derived from leg results, §9)
Side        BACK | LAY
StakeKind   CASH | FREE_SNR | FREE_SR                           (LAY legs are always CASH)
LegResult   PENDING | WON | LOST | VOID | CASHED_OUT
VenueKind   BOOKMAKER | EXCHANGE
AuditKind   CREATED | EDITED | SETTLED | REOPENED | ADJUSTMENT | CORRECTION | DELETED | RESTORED
```

### 3.2 Bet (header)

| Field | Type | Notes |
|---|---|---|
| id | UUID text | uuid4 |
| bet_type | BetType | A template that generated the legs; does not change the maths. |
| status | BetStatus | Stored, but always recomputed from legs by the storage layer (§9). |
| event_name | text | "Arsenal v Chelsea" |
| selection | text | "Arsenal" (display only; the leg's `selection_index` drives the maths) |
| market | text | "Match Odds" (display only) |
| market_outcomes | int ≥ 2 | Number of mutually exclusive outcomes the P/L vector ranges over. 2 for back/lay on one selection; n for a dutch of an n-way market. |
| offer_id | UUID or null | Optional link to an offer. |
| parent_bet_id | UUID or null | For a free bet: the qualifying bet that earned it. |
| currency | text | Always `GBP` in v0.1. |
| placed_at | UTC timestamp | Editable, defaults to now. Drives "open" bucketing. |
| event_at | UTC timestamp or null | Optional event start; drives "awaiting result" views. |
| settled_at | UTC timestamp or null | Set when status becomes SETTLED or VOID. Drives realised bucketing. |
| expected_pl_pence | int | Guaranteed P/L (min over outcomes) at placement. Frozen once status leaves OPEN. |
| actual_pl_pence | int or null | Derived from legs when SETTLED or VOID (§8). |
| actual_pl_override_pence | int or null | Audited manual override. Effective actual P/L = override if not null, else actual. |
| needs_review | bool | User-set flag. |
| notes | text | Free text. Never put passwords here. |
| created_at / updated_at / deleted_at | UTC timestamps | `deleted_at` non-null means soft-deleted: hidden by default and excluded from every total. |

### 3.3 Leg

| Field | Type | Notes |
|---|---|---|
| id, bet_id, leg_index | | Legs are ordered within a bet. |
| side | Side | |
| venue_id | UUID | Venue where the leg was placed. Commission netting groups by venue (§5.3). |
| selection_index | int | 0 ≤ index < `market_outcomes`. Which outcome this leg is on. |
| odds | Decimal text | > 1, ≤ 4 dp. Canonical decimal odds. |
| odds_text | text or null | As typed ("11/8"), for display. |
| stake_pence | int ≥ 0 | For BACK: the stake. For LAY: the lay stake (the backer's stake the layer accepts). |
| stake_kind | StakeKind | BACK only; LAY is CASH. |
| commission_bp | int, 0..9999 | Snapshot at placement. 0 for bookmakers. |
| result | LegResult | Default PENDING. |
| settled_amount_pence | int or null | Required when result is CASHED_OUT: the leg's **net** P/L in pence as realised (early payout, cash-out, partial cash-out). For a CASH back leg this is the return credited **minus the stake**; for a free-bet leg it is the return itself; for an exchange leg it is the cash-out figure the exchange shows, which is already net. The UI asks for the return and converts (§8.4). |

### 3.4 Venue and Offer

Venue: `id, name (unique, case-insensitive), kind, default_commission_bp, min_stake_pence,
ladder ('BETFAIR' or null)`. Seeded with common UK bookmakers and exchanges; new names typed into the
form create venues on the fly (a lay at an unknown name creates an exchange, anything else a
bookmaker). Venues can be renamed (legs refer to them by id, so a typo is fixed on every bet), have
their defaults changed, change kind only while nothing refers to them, and be deleted only while
nothing refers to them. Venue changes are not part of any bet's audit trail. Offer:
`id, venue_id, title, notes`. Both are minimal in v0.1.

---

## 4. Money, odds, commission, rounding

- **Money** is an integer number of pence in core and storage. Display formats as `£12.34` or
  `-£0.58`, sign before the symbol. Never `float`.
- **Odds** are `decimal.Decimal`, strictly greater than 1, at most 4 decimal places, stored as
  TEXT (`'2.1000'`). Input accepts decimal strings (`2.1`, `2.375`) and fractions (`11/8`,
  `100/30`, `1/1`), plus the keywords `evens`, `evs` and `even` matched case-insensitively after
  trimming (UK price lists show `EVS`); fractions convert as `1 + a/b` quantized to 4 dp
  ROUND_HALF_UP.
  Non-terminating fractions therefore carry a tiny error at large stakes (e.g. `100/30` at £1000
  differs from the exact fraction by 3p); the typed text is kept in `odds_text`. Decimal input with
  more than 4 dp or a comma decimal separator is rejected.
- **Commission** is an integer in basis points, `0 ≤ commission_bp < 10000`. `c` below is
  `commission_bp / 10000` as a Decimal.
- **Rounding** is `ROUND_HALF_UP` and happens at exactly these points, nowhere else:
  1. A suggested lay stake (§6) or dutch stake (§6.4) is rounded to whole pence.
  2. Each leg's gross figure for an outcome (§5.1) is computed exactly in Decimal from integer pence
     and Decimal odds, then rounded to whole pence.
  3. Commission for a venue group in an outcome is `HALF_UP(net_positive_gross × c)` in pence (§5.3).
  4. Liability `L × (O − 1)` is rounded to whole pence for display and storage.
  5. Rating and book percentage are quantized to 2 dp for display only. Book percentage is
     computed with exact rationals and rounded once, so an exact tie rounds up and the result
     does not depend on the order of the prices.
- Decimal arithmetic uses a context with precision 28. No float ever enters a calculation.

---

## 5. Profit/loss model

### 5.1 Leg gross P/L

For a market with outcomes `k = 0 .. market_outcomes − 1`, a leg on `selection_index = s` has,
before commission:

| Leg | outcome `k == s` (selection wins) | outcome `k != s` (selection loses) |
|---|---|---|
| BACK, CASH | `+S × (B − 1)` | `−S` |
| BACK, FREE_SNR | `+S × (B − 1)` | `0` |
| BACK, FREE_SR | `+S × B` | `0` |
| LAY | `−L × (O − 1)` | `+L` |

Each cell is rounded to pence (§4 point 2). A leg that is already settled contributes a constant
regardless of `k` (§8.1).

### 5.2 Position vector

`pl_vector(legs, market_outcomes)` returns one pence value per outcome: the sum over venue groups of
that group's net figure for the outcome (§5.3).

- `guaranteed_pl = min(pl_vector)`. This is what `expected_pl_pence` stores at placement and what
  the "open" summary sums. The worst case is the number a matched bettor cares about; there is no
  probability weighting.
- `liability` of a lay leg is `HALF_UP(L × (O − 1))`. Open exposure per exchange is the sum of
  liabilities of PENDING lay legs.

### 5.3 Commission

Exchanges charge commission on **net winnings per market**, not per bet. For each outcome `k` and
each venue group (legs sharing `venue_id`; all legs in a group must share `commission_bp`):

```
G = sum of the group's rounded gross figures for outcome k
net = G − HALF_UP(G × c)   if G > 0 and c > 0
net = G                     otherwise
```

For a single lay leg this is `L − HALF_UP(L × c)` when the lay wins, the familiar `L × (1 − c)`
up to the commission rounding (it differs from `HALF_UP(L × (1 − c))` by 1p on a half-penny tie,
see F16), and `−L × (O − 1)` when it loses. Commission is never charged when the lay loses (the
exchange has no winnings to tax), and never applies to bookmaker legs (`commission_bp = 0`). The
netting rule matters once a position holds two exchange legs in one market (§16.1).

**Netting is per bet.** Real exchanges net every bet you hold in a market; v0.1 does not identify
the exchange market, so lays belonging to different bets in the same real market (a qualifier on
Arsenal and a free bet on Chelsea in one Match Odds market) are not netted against each other. If
the difference matters, record it with an ADJUSTMENT. An exchange-market key is a §16 candidate.

### 5.4 Derived figures

- `expected_pl_pence` = guaranteed P/L at placement (frozen when the bet leaves OPEN).
- Rating (§6.3), liability (§5.2), and all ledger totals are derived by query or by `core`
  functions. Nothing cumulative is stored.
- **Summary buckets.** *Realised* figures sum the effective actual P/L (override if set, else
  `actual_pl_pence`) over bets with status SETTLED or VOID. *Open guaranteed P/L* sums, over bets
  with status OPEN or PARTIALLY_SETTLED, the live `min(pl_vector)` computed from the current legs
  (settled legs as constants, §8.1); for an OPEN bet that equals `expected_pl_pence`. *Open
  liability* sums the liabilities of PENDING lay legs. Soft-deleted bets are excluded from every
  bucket.

---

## 6. Bet-type templates and optimal lay stakes

A template turns form input into legs. The formulas equalise the two outcomes of a one-back-one-lay
position (`market_outcomes = 2`, both legs on `selection_index = 0`). The suggested lay stake is
rounded to pence and is **editable**: the stored legs hold what was actually matched, and the P/L
vector is always computed from the stored legs, so an over- or under-lay simply shows two different
branch figures.

### 6.1 Closed forms (before rounding)

| Template | Back leg kind | Optimal lay stake `L*` | Equalised P/L | P/L if back wins | P/L if lay wins |
|---|---|---|---|---|---|
| QUALIFYING | CASH | `S × B / (O − c)` | `S × (B(1−c)/(O−c) − 1)` | `S(B−1) − L(O−1)` | `L(1−c) − S` |
| FREE_BET_SNR | FREE_SNR | `S × (B − 1) / (O − c)` | `S(B−1)(1−c)/(O−c)` | `S(B−1) − L(O−1)` | `L(1−c)` |
| FREE_BET_SR | FREE_SR | `S × B / (O − c)` | `S × B(1−c)/(O−c)` | `S×B − L(O−1)` | `L(1−c)` |

Derivations: set the two branch expressions equal and solve for `L`. Useful identities for tests:
`P_SR = QL + S` at the same `S, B, O, c`; with `B = O` and `c = 0`, `QL = 0` and the SNR profit is
`S(B−1)/B`.

### 6.2 What the previous spec got wrong

The earlier CLAUDE.md gave one formula, `S(B−1) − L(O−1)`, for both QL and SNR. That is the
back-wins branch only; it is the same for both types and is the branch where commission never
applies. The types differ in the lay-wins branch (`L(1−c) − S` versus `L(1−c)`) and in `L*`.
"Adjusted for commission when back bet wins" was backwards. This section replaces it.

### 6.3 Rating

Displayed to 2 dp. `QUALIFYING` and `FREE_BET_SR`: `100 × B(1−c)/(O−c)` (percentage of stake
retained). `FREE_BET_SNR`: `100 × (B−1)(1−c)/(O−c)` (percentage of the free bet converted to
cash). A qualifier at 100% has zero qualifying loss.

### 6.4 Dutching (model only in v0.1)

For `n` cash back legs on distinct selections with odds `B_i`, target return `R`:
`S_i = HALF_UP(R / B_i)`; total `T = Σ S_i`; P/L if outcome `i` wins is
`HALF_UP(S_i × B_i) − T` (identical to the §5.1 leg table since `S_i` is integer). Book percentage
`100 × Σ 1/B_i`; profit is possible only when it is below 100. A free-bet SNR leg `k` uses
`S_i = S_k(B_k − 1)/B_i` for the other legs. Partial cover (fewer legs than `market_outcomes`)
is legal; the uncovered outcome loses every cash stake, and `guaranteed_pl` reflects that.

---

## 7. Golden fixtures

All values in pence. `bp` is commission in basis points. Every fixture is a mandatory test in
`tests/test_golden.py`; recompute by hand from §4–§6 before changing any of them.

### 7.1 Two-way templates (suggested lay stake used)

| # | Type | S | B | O | bp | L* | liability | back wins | lay wins | guaranteed | rating |
|---|---|---|---|---|---|---|---|---|---|---|---|
| F1 | QUALIFYING | 1000 | 2.00 | 2.10 | 200 | 962 | 1058 | −58 | −57 | −58 | 94.23 |
| F2 | FREE_BET_SNR | 1000 | 2.00 | 2.10 | 200 | 481 | 529 | 471 | 471 | 471 | 47.12 |
| F3 | FREE_BET_SR | 1000 | 2.00 | 2.10 | 200 | 962 | 1058 | 942 | 943 | 942 | 94.23 |
| F4 | FREE_BET_SNR | 2000 | 6.0 | 6.4 | 200 | 1567 | 8462 | 1538 | 1536 | 1536 | 76.80 |
| F5 | QUALIFYING | 2500 | 3.5 | 3.5 | 0 | 2500 | 6250 | 0 | 0 | 0 | 100.00 |
| F8 | QUALIFYING | 5000 | 4.3333 | 4.4 | 500 | 4981 | 16935 | −268 | −268 | −268 | 94.64 |
| F9 | FREE_BET_SNR | 500 | 11.0 | 12.0 | 200 | 417 | 4587 | 413 | 409 | 409 | 81.80 |
| F10 | QUALIFYING | 1000 | 1.50 | 1.52 | 200 | 1000 | 520 | −20 | −20 | −20 | 98.00 |
| F11 | FREE_BET_SR | 2500 | 3.0 | 3.2 | 0 | 2344 | 5157 | 2343 | 2344 | 2343 | 93.75 |

Worked F1: `L* = 1000 × 2.00 / (2.10 − 0.02) = 961.538…` → 962. Back wins:
`1000 × 1.00 − 962 × 1.10 = 1000 − 1058.2` → gross legs `+1000` and `−1058` → −58. Lay wins:
bookmaker `−1000`; exchange gross `+962`, commission `HALF_UP(962 × 0.02) = HALF_UP(19.24) = 19`,
net `943`; total −57. Guaranteed −58. Rating `100 × 2 × 0.98 / 2.08 = 94.23`.

### 7.2 Non-optimal lay stakes (user overrode the suggestion)

| # | Type | S | B | O | bp | L (given) | liability | back wins | lay wins | guaranteed |
|---|---|---|---|---|---|---|---|---|---|---|
| F6 | QUALIFYING | 1000 | 2.00 | 2.10 | 200 | 962 | 1058 | −58 | −57 | −58 |
| F7 | QUALIFYING | 1000 | 2.00 | 2.10 | 200 | 1000 | 1100 | −100 | −20 | −100 |

### 7.3 Dutching

F12: three-way market, odds `2.50 / 3.40 / 3.10`, target return 3000 → stakes `1200 / 882 / 968`,
total 3050, P/L vector `[−50, −51, −49]`, guaranteed −51, book 101.67%.

F12b: same shape around a free bet. SNR free stake 1000 on selection 0 at `3.0`, cash legs at
`3.4` and `3.1` → stakes `1000 / 588 / 645`, P/L vector `[767, 766, 767]`, guaranteed 766.

### 7.4 Settlement of F1 (legs: back 1000 @ 2.00 at a bookmaker, lay 962 @ 2.10 at 2%)

| # | Back leg | Lay leg | Realised |
|---|---|---|---|
| F13a | WON | LOST | −58 |
| F13b | LOST | WON | −57 |
| F13c | VOID | VOID | 0 |
| F13d | VOID | WON | 943 |
| F13e | VOID | LOST | −1058 |
| F14a | CASHED_OUT, net +1000 (early payout credited £20.00 return on the £10.00 cash stake) | WON | 1943 |
| F14b | CASHED_OUT, net +1000 | LOST | −58 |

### 7.5 Commission rounding

F16: lay 975 pence at 200 bp wins: commission `HALF_UP(19.5) = 20`, net 955.
F16b: lay 925 pence at 200 bp wins: commission `HALF_UP(18.5) = 19`, net 906 (half-even rounding
would give 907; this fixture tells the two apart).

### 7.6 Odds parsing

Whole-number prices never come back in exponent form: `"9/1"` → `10` (`str` is `10`, storage form
`10.0000`).

`"11/8"` → `2.375`; `"100/30"` → `4.3333`; `"1/1"` → `2`; `"evens"`, `"EVS"`, `"Evens"` → `2`;
`"2.1"` → `2.1`; `"1.00"`, `"0/1"`, `"2,10"`, `"2.12345"`, `"abc"` → rejected.

---

## 8. Settlement and realised P/L

### 8.1 Settled legs are constants

When a leg has a result other than PENDING it contributes the same gross figure to every outcome:

| result | BACK gross | LAY gross |
|---|---|---|
| WON | as "selection wins" in §5.1 | `+L` (the selection lost) |
| LOST | as "selection loses" in §5.1 | `−L × (O − 1)` (the selection won) |
| VOID | 0 | 0 |
| CASHED_OUT | `settled_amount_pence` (net, already after any exchange commission the user saw) | same |

Commission netting (§5.3) then applies per venue group as usual, except that `CASHED_OUT`
amounts are excluded from the group's gross before commission is computed because they are already
net figures; they are added back afterwards.

### 8.2 Realised P/L

`realised_pl(legs)` is defined only when no leg is PENDING; it is `pl_vector[0]` (all entries are
equal once every leg is constant). The storage layer writes it to `actual_pl_pence` when the bet
becomes SETTLED or VOID and sets `settled_at`. The effective actual P/L shown and summed is
`actual_pl_override_pence` if set, else `actual_pl_pence`.

### 8.3 The Settle action

The dialog offers, for a two-way bet: **Back won**, **Lay won** (selection lost), **Void** (both
legs), and a per-leg editor for anything else (one-sided void, cash-out or early payout entered as
the return credited, stored as net per §8.4).
For a dutch it offers the winning selection or void. Settling writes leg results, recomputes status
and `actual_pl_pence`, sets `settled_at`, and appends `SETTLED` audit rows.

### 8.4 Adjust (v0.1 meaning)

"Adjust" is an audited manual override: the user enters the realised P/L and a mandatory reason;
`actual_pl_override_pence` is set and an `ADJUSTMENT` row records old and new values. Real
modelling of lock-ins and top-ups as extra legs is §16. Early payouts and cash-outs do not need an
override: settle the back leg as `CASHED_OUT` with its **net** P/L. The per-leg editor asks for the
return credited by the bookmaker (a 2-up early payout on F1's £10 @ 2.00 credits £20.00; a cash-out
button shows the return including stake) and stores return minus cash stake for a CASH leg, or the
return itself for a free-bet leg (`Leg.cashed_out_from_return` in core).

### 8.5 Backfill

The New Bet form may create a bet directly with results and a past `placed_at`/`settled_at`. The
audit row kind is `CREATED` with `new_value` noting `backfill`.

---

## 9. State machine

### 9.1 Bet status is derived from leg results

```
all legs PENDING                          -> OPEN
some legs settled, some PENDING           -> PARTIALLY_SETTLED
all legs settled, at least one not VOID   -> SETTLED
all legs VOID                             -> VOID
```

Storage recomputes and stores `status` inside the same transaction as any leg change.
Soft-deleted bets keep their status but are excluded from views and totals.

### 9.2 Legal actions

| Action | Allowed when | Effect | Audit kind |
|---|---|---|---|
| Create | always | legs PENDING (or results given for backfill) | CREATED |
| Edit parameters | status OPEN | legs/header changed, `expected_pl_pence` recomputed | EDITED |
| Correct parameters | status not OPEN | legs/header changed (type, odds, stakes, venues, text, and `settled_at` on a SETTLED or VOID bet); a leg's `result` is not correctable and a correction that tries is refused (use Reopen); legs cannot be added or removed; the live P/L vector and `actual_pl_pence` follow the corrected legs; `expected_pl_pence` is **never** changed after the bet has left OPEN | CORRECTION |
| Settle | any leg PENDING | leg results set; status, `actual_pl_pence`, `settled_at` recomputed | SETTLED |
| Reopen | status PARTIALLY_SETTLED, SETTLED or VOID | chosen legs back to PENDING; `actual_pl_pence`, `settled_at` and the override cleared if set. Reopen is the only way to change a recorded leg result. | REOPENED |
| Adjust | status SETTLED or VOID | `actual_pl_override_pence` set with reason | ADJUSTMENT |
| Flag for review | not deleted, any status | `needs_review` toggled | EDITED (field `needs_review`) |
| Soft delete / restore | any | `deleted_at` set / cleared | DELETED / RESTORED |

A change that alters nothing (same values, or timestamps equal to the second) writes nothing: no
audit row, no new leg ids, no `updated_at` bump. Every action on a soft-deleted bet other than
restore is refused.

There is no `CANCELLED` status. A bet that was never placed is soft-deleted; a voided market is
`VOID`; a one-sided void is a `VOID` result on one leg.

`has_adjustments` is not a column: it is true when the bet has any `ADJUSTMENT` or `CORRECTION`
audit row, and the ledger shows a badge for it.

---

## 10. Audit trail

Table `adjustments` (name kept from the original spec; it holds every kind of change) is
append-only: `BEFORE UPDATE` and `BEFORE DELETE` triggers raise. One user action writes one or more
rows sharing an `action_id`; each row records `field`, `old_value`, `new_value` as text and an
optional `reason`. `ADJUSTMENT` and `CORRECTION` require a non-empty reason. Values are serialised
as the stored text form (pence as integers, odds as decimal strings, timestamps as UTC strings).

---

## 11. Storage

### 11.1 Engine and connection

Standard-library `sqlite3`, one connection owned by the storage layer, one `BEGIN IMMEDIATE`
transaction per user action. The bet is read and the action's legality checked **inside** that
transaction, never before it. Per connection: `PRAGMA foreign_keys = ON`,
`PRAGMA synchronous = FULL`, `PRAGMA busy_timeout = 5000`. Once per database, after the version
check: `PRAGMA journal_mode = WAL`, waiting up to the busy timeout. Tables are `STRICT`. No default
adapters or converters: timestamps are written and read as text explicitly.

Errors: storage methods raise only `CoreError` subclasses (bad input) or `StorageError` subclasses.
Raw sqlite3 errors are translated: busy or locked becomes `DatabaseBusyError`, disk or size limit full
becomes `DiskFullError`, not a database or malformed becomes `CorruptDatabaseError`, a failed
migration becomes `MigrationError`, constraint failures become `StorageError`. A rollback after SQLite
has already rolled back never replaces the original error.

### 11.2 Schema (version 1)

```sql
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
```

Bets are never hard-deleted by the app (soft delete only). The foreign keys take SQLite's default
`NO ACTION`, which still refuses to delete a referenced row; that is intended.

### 11.3 Migrations

`PRAGMA user_version` holds the schema version. It is read before anything writes to the file
(before the WAL switch). If it is **newer** than the app knows, the app refuses to open the file and
leaves it byte-for-byte untouched. Forward-only migrations, each in one transaction. Before any
migration of an existing database runs, a backup is taken (§11.5). A failed migration is rolled back,
the connection is closed and the lock released.

### 11.4 Location and permissions

Data directory: `platformdirs.user_data_dir("odds-pup")` (`~/.local/share/odds-pup` on Linux),
overridable by `--data-dir` or `ODDS_PUP_DATA_DIR` (in that order of precedence). Files:
`odds-pup.sqlite3` (+ `-wal`, `-shm`), `backups/`, `odds-pup.lock`. Directories are `0700`; the
database, its sidecars, backups, the lock file and CSV exports are `0600`. Existing files with looser
permissions are tightened on open, and the database is tightened before WAL is enabled so SQLite
creates the sidecars with the same mode.

### 11.5 Backups

On startup and before every migration: `VACUUM INTO 'backups/odds-pup-YYYYMMDD-HHMMSS.sqlite3'`
(UTC stamp; `-1`, `-2` … for more than one in a second, ordered after the unsuffixed name). `VACUUM
INTO` does not fsync, so the copy and the directory are fsynced before anything is pruned; then the
newest 10 are kept. The About box shows the data folder so the user can copy backups elsewhere.

### 11.6 Single instance

`odds-pup.lock` in the data directory, held with `fcntl.flock`. `Repository.open` takes it before
connecting and `close` releases it, so two repositories can never write one ledger, even in one
process; the kernel releases it if the process dies. A second launch reports that the ledger is
already open and exits. Tests use a temporary `--data-dir`, so they never collide.

### 11.7 CSV export (v0.1) and import (later)

Export writes the currently filtered ledger, one row per leg joined with its bet header, UTF-8,
comma-separated, header row first. The query runs before the file is opened and the file is written
to a temporary sibling then renamed, so a failure never leaves a truncated export or destroys an
existing one. A `deleted` column marks soft-deleted bets when they are included. Money columns are 2 dp strings, odds are decimal strings,
timestamps UTC. The first column is `odds_pup_schema_version` (constant per file) so an old export
stays importable once import exists. Import is deferred until a real sample sheet is available.

---

## 12. Time and locale

- Store every timestamp as UTC text `YYYY-MM-DDTHH:MM:SSZ` (fixed width, explicit `Z`).
- Timestamps given to storage must be timezone-aware; naive values (which Qt returns) are refused
  with a `ValidationError` rather than guessed at in the system zone. Local calendar-day filters use
  `local_date_range(first, last)`, which returns `[start of first, start of the day after last)` in
  UTC.
- Display in local time (Europe/London). Period summaries ("this month") bucket by the **local**
  calendar date of `settled_at` (realised) or `placed_at` (open). A bet settled at 00:30 BST on
  1 September is 23:30Z on 31 August and must count in September.
- Fixed en_GB display formats: `DD/MM/YYYY`, 24-hour clock, Monday week start, `£`.
- Money input accepts `10`, `10.00`, `£10.00`, `-0.58`; rejects thousands separators and comma
  decimals. Parsing is done by `core`, never by locale-aware Qt validators.

---

## 13. User interface (v0.1)

- **Ledger**: `QTableView` over a `QAbstractTableModel` backed by storage, with
  `QSortFilterProxyModel` for filters: status, bookmaker, bet type, date range, free text over
  event/selection/notes. Columns: placed, event, selection, type, bookmaker, exchange, back stake,
  back odds, lay stake, lay odds, expected, actual, status, badges (adjusted, needs review).
  P/L cells carry an explicit sign; colour is secondary.
- **Summary strip**: realised all-time, realised this month, open guaranteed P/L, open liability per
  exchange, per-bookmaker realised table. All derived by query after each committed transaction;
  changing a filter re-queries only the ledger. Search is debounced (250 ms; Enter applies at once).
- **Bookmakers and exchanges** (Settings menu): rename, set default commission, minimum lay stake
  and price ladder, add, and delete unused venues.
- **New bet**: type selector, event, selection, bookmaker (autocomplete, creates venues), back
  stake, back odds (decimal or fraction), exchange (remembers last, pre-fills its commission),
  lay odds, lay stake (pre-filled with `L*`, editable), live panel showing liability, P/L if back
  wins, P/L if lay wins, guaranteed, rating, and warnings (§14). Enter saves, Esc cancels, Ctrl+N
  new, Clone duplicates a row with a fresh id and timestamp. Optional: offer tag, parent bet,
  notes, past dates and results for backfill.
- **Settle** and **Adjust** dialogs as in §8.3–§8.4. **Detail pane** shows legs and the audit
  trail.
- **Accessibility**: every control keyboard-reachable, `accessibleName` on inputs, system font
  scaling respected, no colour-only indicators.
- **Performance goal**: interactive ledger within 2 s of launch; 10,000 rows filter and scroll
  without perceptible lag.

---

## 14. Validation and warnings

Hard errors (the form cannot save):

- odds ≤ 1, or above 10000, or more than 4 dp, or unparsable (ASCII digits only)
- any money amount above £10,000,000,000 in magnitude (a guard that keeps Decimal arithmetic
  exact at precision 28; not a business limit)
- back stake ≤ 0 on a new bet; any stake < 0
- commission outside `0 ≤ bp < 10000`
- `selection_index ≥ market_outcomes`; two legs in one venue group with different commission
- `CASHED_OUT` without an amount, or an amount on any other result
- a LAY leg with a stake kind other than CASH
- `ADJUSTMENT`/`CORRECTION` without a reason

Warnings (shown, saving allowed):

- lay odds not on the venue's tick ladder (Betfair ladder: 0.01 steps to 2, 0.02 to 3, 0.05 to 4,
  0.1 to 6, 0.2 to 10, 0.5 to 20, 1 to 30, 2 to 50, 5 to 100, 10 to 1000)
- lay stake below the venue's minimum stake (Betfair £2)
- commission above 10%
- rating below 80% on a qualifier (likely a typo in the odds)

---

## 15. Decisions log

All decided 2026-09-12 with the developer, taking the recommended defaults.

| # | Decision |
|---|---|
| D1 | Stack: Python ≥ 3.13 (developed on 3.14), PySide6 (LGPL), stdlib sqlite3 in WAL mode, uv, ruff, mypy strict on core/storage, pytest + Hypothesis. No Tauri, no JS toolchain. Project licence MIT. |
| D2 | Calculator-plus-log: the app pre-fills the optimal lay stake; the stored lay stake is what was matched; both branches are stored via the leg model and `expected_pl_pence` is the guaranteed (minimum) figure. |
| D3 | Bets + legs schema from day one. v0.1 UI exposes QUALIFYING, FREE_BET_SNR, FREE_BET_SR as one-back-one-lay templates; DUTCHING is in the model only. |
| D4 | "Adjust" in v0.1 is an audited manual override of realised P/L with a mandatory reason. Second exchange legs (lock-in, top-up) are v0.2 and need no migration. |
| D5 | Settle records a result per leg (WON / LOST / VOID / CASHED_OUT with a net amount) and derives realised P/L. Hand-typed figures are ADJUSTMENT or CORRECTION rows, never silent edits. |
| D6 | Status is OPEN → PARTIALLY_SETTLED → SETTLED, or VOID when every leg is void, derived from legs. No ADJUSTED or CANCELLED status; adjusted is a derived badge; mistakes are soft-deleted; reopen is an audited action. |
| D7 | Append-only audit table with per-field old/new rows grouped per action; CORRECTION distinct from ADJUSTMENT; placement expectation frozen once a bet leaves OPEN; all totals derived by query. |
| D8 | Money as integer pence; odds as Decimal ≤ 4 dp stored as TEXT with fractional input accepted; commission in basis points; ROUND_HALF_UP at the points in §4 only; timestamps split into placed/event/settled, stored UTC, bucketed by Europe/London date. |
| D9 | UK only: GBP with a `currency` column defaulting to GBP, no tax reporting, UK venue seed list with default commission, Betfair ladder and minimum-stake warnings. |
| D10 | Offers: nullable `offer_id` and `parent_bet_id` plus a minimal offers table. Bankroll and account balances out of scope for v0.1. |
| D11 | No existing data to import. CSV export in v0.1; import when a sample sheet exists. Backfill of settled bets with past dates supported in the New Bet form. |
| D12 | Housekeeping first: short CLAUDE.md pointing here, README, MIT LICENSE, .gitignore excluding ledger files; data in the XDG data dir with 0600 permissions, single-instance lock, rotating backups, zero network. Agent commits on feature branches; the developer signs and merges. Git author name follows the current config (Sophie). |
| D15 | 2026-09-13, after adversarial review of storage: legality checks run inside the write transaction; `Repository.open` holds the instance lock; version checked before any write; raw sqlite3 errors translated into `StorageError` subclasses; existing files tightened to 0600; backups fsynced before pruning; atomic CSV export; naive timestamps refused; bet type and `settled_at` correctable; review flag allowed in any non-deleted status; venues renamable and deletable while unused; venue filters use a non-correlated subquery (the correlated form took ~10 s at 10k bets). |
| D14 | 2026-09-13, after adversarial review of the core: odds capped at 10000 and money at £10bn as precision guards; venue grouping key is the trimmed, case-folded name; book percentage computed exactly; the Settle helper is leg-based with outcomes BACK_WON / LAY_WON / VOID and refuses to overwrite a recorded result; commission percent parsing lives in core. |
| D13 | Commission is netted per venue within a bet (an approximation of the per-market netting exchanges apply; bets are not netted against each other in v0.1). For a single lay leg this equals the per-bet formula up to the F16 rounding; it matters for §16.1. |

---

## 16. Later (v0.2+)

### 16.1 Lock-in after an early payout

After a bookmaker pays a back as won early, the position is: bookmaker leg fixed at `+S(B−1)`,
naked lay `L @ O` on the exchange. Backing `X` on the exchange at the new odds `O'` in the same
market equalises the exchange group. With per-market commission netting, both exchange branches are
negative whenever `O' < O`, so no commission is due and `X = L × O / O'`. Example from F1 with
`O' = 1.50`: `X = 962 × 2.10 / 1.50 = 1346.8` → 1347; exchange vector `[−384, −385]`; total with
the fixed +1000 is `[616, 615]`. (The per-bet calculator formula `X = L(O−c)/(O'(1−c)+c)` gives
1343 and, by its own accounting, claims a lock-in of about 600; under the netting rule the same 1343
actually yields `[614, 619]`. That formula overstates commission because it taxes the exchange
back's winnings even though the market nets negative.) The solver for this is a v0.2 feature; the
schema already holds the legs.

### 16.2 Also deferred

Lay top-ups at new odds; underlay/overlay target modes; refund-if-lose promos; offer entity with
completion state; balances; CSV import with column mapping; Smarkets/Matchbook ladders; PyInstaller
onedir bundle; Windows/macOS.
