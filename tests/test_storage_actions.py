"""Repository behaviour gaps found in review: validation, no-ops, audit completeness, reopen,
settle, adjust, venues, filters, summary boundaries, backfill, export and clone helpers."""

from __future__ import annotations

import csv
import io
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from odds_pup.core import (
    AuditKind,
    BetStatus,
    BetType,
    LegResult,
    Side,
    StakeKind,
    TwoWayOutcome,
    ValidationError,
    VenueKind,
)
from odds_pup.storage import (
    CSV_COLUMNS,
    AuditValueKind,
    BetFilter,
    IllegalActionError,
    NewBet,
    NewLeg,
    NotFoundError,
    Repository,
    audit_value_kind,
    export_csv,
    write_csv,
)
from odds_pup.storage import repository as repository_module
from tests.storage_helpers import FakeClock, f1_bet, snr_bet

D = Decimal
F1_LEGS = f1_bet().legs


def trail(repo: Repository, bet_id: str) -> list[tuple[str, str | None, str | None, str | None]]:
    return [(r.kind.value, r.field, r.old_value, r.new_value) for r in repo.audit_trail(bet_id)]


def dutch(**overrides: object) -> NewBet:
    fields: dict[str, object] = {
        "bet_type": BetType.DUTCHING,
        "event_name": "Three-way",
        "market_outcomes": 3,
        "legs": [
            NewLeg(Side.BACK, "Bet365", D("2.50"), 1200, selection=0),
            NewLeg(Side.BACK, "Coral", D("3.40"), 882, selection=1),
            NewLeg(Side.BACK, "Betfred", D("3.10"), 968, selection=2),
        ],
    }
    fields.update(overrides)
    return NewBet(**fields)  # type: ignore[arg-type]


# ------------------------------------------------------------- timestamps on edit


@pytest.mark.parametrize(
    "bad",
    ["tomorrow", date(2026, 9, 1), datetime(2026, 9, 1, 12, 0), 1_790_000_000],
    ids=["str", "date", "naive", "int"],
)
@pytest.mark.parametrize("field", ["placed_at", "event_at"])
def test_edit_rejects_non_aware_timestamps_without_writing(repo: Repository, field, bad):
    bet = repo.create_bet(f1_bet())
    with pytest.raises(ValidationError):
        repo.edit_bet(bet.id, **{field: bad})
    assert repo.get_bet(bet.id) == bet
    assert len(repo.list_bets()) == 1
    repo.summary()  # the ledger is still readable


def test_microsecond_only_timestamp_change_is_a_no_op(repo: Repository):
    bet = repo.create_bet(f1_bet())
    same = repo.edit_bet(bet.id, placed_at=bet.placed_at + timedelta(microseconds=900_000))
    assert same == bet
    assert len(repo.audit_trail(bet.id)) == 1


def test_placed_at_cannot_be_cleared(repo: Repository):
    bet = repo.create_bet(f1_bet())
    with pytest.raises(ValidationError):
        repo.edit_bet(bet.id, placed_at=None)


# ---------------------------------------------------------------- edit / correct


def test_correction_refuses_leg_results(repo: Repository):
    bet = repo.create_bet(f1_bet())
    repo.settle_two_way(bet.id, TwoWayOutcome.BACK_WON)
    voided = [
        NewLeg(Side.BACK, "Bet365", D("2.00"), 1000, result=LegResult.VOID),
        NewLeg(Side.LAY, "Smarkets", D("2.10"), 962, commission_bp=200, result=LegResult.VOID),
    ]
    with pytest.raises(IllegalActionError, match="reopen"):
        repo.correct_bet(bet.id, reason="void it", legs=voided)
    assert [r.leg.result for r in repo.get_bet(bet.id).legs] == [LegResult.WON, LegResult.LOST]


@pytest.mark.parametrize("settle_first", [False, True])
def test_identical_legs_write_nothing(repo: Repository, clock: FakeClock, settle_first: bool):
    bet = repo.create_bet(f1_bet())
    if settle_first:
        bet = repo.settle_two_way(bet.id, TwoWayOutcome.BACK_WON)
    rows = len(repo.audit_trail(bet.id))
    clock.advance(minutes=5)
    if settle_first:
        after = repo.correct_bet(bet.id, reason="nothing really", legs=list(F1_LEGS))
    else:
        after = repo.edit_bet(bet.id, legs=list(F1_LEGS))
    assert after == bet
    assert [r.id for r in after.legs] == [r.id for r in bet.legs]
    assert len(repo.audit_trail(bet.id)) == rows


def test_edit_audits_every_changed_leg_field_under_one_action(repo: Repository):
    bet = repo.create_bet(f1_bet())
    repo.edit_bet(
        bet.id,
        legs=[
            NewLeg(
                Side.BACK,
                "Coral",
                D("2.10"),
                1100,
                selection=1,
                stake_kind=StakeKind.FREE_SNR,
                odds_text="11/10",
            ),
            NewLeg(Side.LAY, "Betfair Exchange", D("2.20"), 1000, selection=1, commission_bp=500),
        ],
    )
    rows = [r for r in repo.audit_trail(bet.id) if r.kind is AuditKind.EDITED]
    assert len({r.action_id for r in rows}) == 1
    fields = {(r.field, r.old_value, r.new_value) for r in rows}
    assert {
        ("legs[0].venue", "Bet365", "Coral"),
        ("legs[0].odds", "2.0000", "2.1000"),
        ("legs[0].stake", "1000", "1100"),
        ("legs[0].selection", "0", "1"),
        ("legs[0].stake_kind", "CASH", "FREE_SNR"),
        ("legs[0].odds_text", None, "11/10"),
        ("legs[1].venue", "Smarkets", "Betfair Exchange"),
        ("legs[1].odds", "2.1000", "2.2000"),
        ("legs[1].stake", "962", "1000"),
        ("legs[1].selection", "0", "1"),
        ("legs[1].commission_bp", "200", "500"),
    } <= fields
    assert any(f == "expected_pl_pence" for f, _, _ in fields)


def test_edit_can_add_and_remove_legs_with_audit(repo: Repository):
    bet = repo.create_bet(dutch())
    repo.edit_bet(
        bet.id, legs=[*dutch().legs, NewLeg(Side.BACK, "Unibet", D("9.0"), 100, selection=2)]
    )
    added = [r for r in repo.audit_trail(bet.id) if r.field and r.field.startswith("legs[3].")]
    assert {r.old_value for r in added} == {None}
    repo.edit_bet(bet.id, legs=list(dutch().legs)[:2])
    removed = {(r.field, r.old_value, r.new_value) for r in repo.audit_trail(bet.id)}
    assert ("legs[2]", "present", "removed") in removed
    assert ("legs[3]", "present", "removed") in removed
    assert len(repo.get_bet(bet.id).legs) == 2


def test_header_only_correction_carries_reason_and_keeps_money(repo: Repository):
    bet = repo.create_bet(f1_bet())
    repo.settle_two_way(bet.id, TwoWayOutcome.BACK_WON)
    corrected = repo.correct_bet(
        bet.id, reason="typo", event_name="Arsenal v Chelsea FC", notes="n"
    )
    assert corrected.status is BetStatus.SETTLED
    assert (corrected.expected_pl_pence, corrected.actual_pl_pence) == (-58, -58)
    assert corrected.has_adjustments
    rows = [r for r in repo.audit_trail(bet.id) if r.kind is AuditKind.CORRECTION]
    assert {r.field for r in rows} == {"event_name", "notes"}
    assert {r.reason for r in rows} == {"typo"}


def test_settled_at_is_correctable_only_on_settled_bets(repo: Repository):
    bet = repo.create_bet(f1_bet())
    when = datetime(2026, 8, 1, 17, 0, tzinfo=UTC)
    with pytest.raises(ValidationError):
        repo.edit_bet(bet.id, settled_at=when)
    repo.settle(bet.id, {0: LegResult.WON})
    with pytest.raises(IllegalActionError):
        repo.correct_bet(bet.id, reason="date", settled_at=when)
    repo.settle(bet.id, {1: LegResult.LOST})
    fixed = repo.correct_bet(bet.id, reason="backfill date was wrong", settled_at=when)
    assert fixed.settled_at == when
    assert ("CORRECTION", "settled_at") in {(k, f) for k, f, _, _ in trail(repo, bet.id)}


def test_edit_market_outcomes_revalidates_and_recomputes(repo: Repository):
    bet = repo.create_bet(dutch())
    with pytest.raises(ValidationError, match="outside"):
        repo.edit_bet(bet.id, market_outcomes=2)
    assert repo.get_bet(bet.id).market_outcomes == 3
    grown = repo.edit_bet(bet.id, market_outcomes=4)
    assert grown.expected_pl_pence == -3050  # the uncovered fourth outcome loses every stake
    fields = {f for _, f, _, _ in trail(repo, bet.id)}
    assert {"market_outcomes", "expected_pl_pence"} <= fields
    for bad in (True, "3"):
        with pytest.raises(ValidationError):
            repo.edit_bet(bet.id, market_outcomes=bad)


def test_edit_checks_references_before_writing(repo: Repository):
    bet = repo.create_bet(f1_bet())
    changed = [NewLeg(Side.BACK, "Bet365", D("2.50"), 1000), F1_LEGS[1]]
    with pytest.raises(NotFoundError):
        repo.edit_bet(bet.id, offer_id="nope", legs=changed)
    with pytest.raises(NotFoundError):
        repo.edit_bet(bet.id, parent_bet_id="nope")
    with pytest.raises(ValidationError):
        repo.edit_bet(bet.id, parent_bet_id=bet.id)
    assert repo.get_bet(bet.id) == bet
    assert len(repo.audit_trail(bet.id)) == 1


def test_create_rolls_back_a_new_venue_when_a_reference_is_missing(repo: Repository):
    with pytest.raises(NotFoundError):
        repo.create_bet(
            f1_bet(parent_bet_id="nope", legs=[NewLeg(Side.BACK, "Ghost Bookie", D("2.0"), 100)])
        )
    assert repo.find_venue("Ghost Bookie") is None


# ------------------------------------------------------------------ settle


def test_settle_validates_everything_before_writing(repo: Repository):
    bet = repo.create_bet(f1_bet())
    with pytest.raises(NotFoundError):
        repo.settle(bet.id, {1: LegResult.WON, 5: LegResult.WON})
    with pytest.raises(ValidationError):
        repo.settle(bet.id, {"0": LegResult.WON})  # type: ignore[dict-item]
    with pytest.raises(ValidationError):
        repo.settle(bet.id, {0: "MAYBE"})  # type: ignore[dict-item]
    with pytest.raises(ValidationError):
        repo.settle(bet.id, {0: (LegResult.CASHED_OUT, 10.5)})  # type: ignore[dict-item]
    assert repo.get_bet(bet.id) == bet
    assert len(repo.audit_trail(bet.id)) == 1


def test_settle_uses_settled_at_only_when_completing(repo: Repository, clock: FakeClock):
    when = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
    bet = repo.create_bet(f1_bet())
    partial = repo.settle(bet.id, {0: LegResult.WON}, settled_at=when)
    assert partial.status is BetStatus.PARTIALLY_SETTLED
    assert partial.settled_at is None
    done = repo.settle(bet.id, {1: LegResult.LOST}, settled_at=when)
    assert done.settled_at == when
    other = repo.create_bet(f1_bet())
    assert repo.settle_two_way(other.id, TwoWayOutcome.VOID, settled_at=when).settled_at == when


def test_settle_refuses_to_overwrite_a_recorded_result(repo: Repository):
    bet = repo.create_bet(f1_bet())
    repo.settle(bet.id, {0: LegResult.WON})
    with pytest.raises(IllegalActionError):
        repo.settle(bet.id, {0: LegResult.LOST})
    assert repo.get_bet(bet.id).legs[0].leg.result is LegResult.WON


def test_settle_two_way_requires_back_then_lay(repo: Repository):
    bet = repo.create_bet(f1_bet(legs=[F1_LEGS[1], F1_LEGS[0]]))
    with pytest.raises(IllegalActionError):
        repo.settle_two_way(bet.id, TwoWayOutcome.BACK_WON)
    with pytest.raises(ValidationError):
        repo.settle_two_way(bet.id, "SOMETIMES")  # type: ignore[arg-type]


# ------------------------------------------------------------------ reopen


def test_reopen_one_leg_of_a_settled_bet_is_partial(repo: Repository):
    bet = repo.create_bet(f1_bet())
    repo.settle_two_way(bet.id, TwoWayOutcome.BACK_WON)
    partial = repo.reopen(bet.id, [1])
    assert partial.status is BetStatus.PARTIALLY_SETTLED
    assert partial.actual_pl_pence is None
    assert partial.settled_at is None
    assert ("REOPENED", "status", "SETTLED", "PARTIALLY_SETTLED") in trail(repo, bet.id)


def test_reopen_rejects_bad_requests(repo: Repository):
    bet = repo.create_bet(f1_bet())
    repo.settle_two_way(bet.id, TwoWayOutcome.BACK_WON)
    with pytest.raises(NotFoundError):
        repo.reopen(bet.id, [0, 7])
    with pytest.raises(ValidationError):
        repo.reopen(bet.id, [])
    assert repo.get_bet(bet.id).legs[0].leg.result is LegResult.WON
    repo.soft_delete(bet.id)
    with pytest.raises(IllegalActionError):
        repo.reopen(bet.id)


def test_reopen_void_and_cashed_out(repo: Repository):
    void = repo.create_bet(f1_bet())
    repo.settle_two_way(void.id, TwoWayOutcome.VOID)
    reopened = repo.reopen(void.id)
    assert (reopened.status, reopened.actual_pl_pence, reopened.settled_at) == (
        BetStatus.OPEN,
        None,
        None,
    )
    cashed = repo.create_bet(f1_bet())
    repo.settle(cashed.id, {0: (LegResult.CASHED_OUT, 1000)})
    repo.reopen(cashed.id)
    assert ("REOPENED", "legs[0].settled_amount", "1000", None) in trail(repo, cashed.id)


# ------------------------------------------------------------------ adjust / flags


def test_adjust_void_bet_no_op_and_bad_input(repo: Repository):
    bet = repo.create_bet(f1_bet())
    repo.settle_two_way(bet.id, TwoWayOutcome.VOID)
    adjusted = repo.adjust(bet.id, 5, "goodwill")
    assert adjusted.effective_actual_pl == 5
    assert repo.summary().realised_all_time == 5
    rows = len(repo.audit_trail(bet.id))
    repo.adjust(bet.id, 5, "duplicate")
    assert len(repo.audit_trail(bet.id)) == rows
    for bad in (True, "5", 10**13):
        with pytest.raises(ValidationError):
            repo.adjust(bet.id, bad, "bad")  # type: ignore[arg-type]
    fresh = repo.create_bet(f1_bet())
    repo.settle_two_way(fresh.id, TwoWayOutcome.BACK_WON)
    unchanged = repo.adjust(fresh.id, None, "nothing to clear")
    assert not unchanged.has_adjustments
    repo.soft_delete(fresh.id)
    with pytest.raises(IllegalActionError):
        repo.adjust(fresh.id, 0, "deleted")


def test_needs_review_allowed_on_settled_but_not_deleted(repo: Repository):
    bet = repo.create_bet(f1_bet())
    repo.settle_two_way(bet.id, TwoWayOutcome.BACK_WON)
    assert repo.set_needs_review(bet.id, True).needs_review
    repo.soft_delete(bet.id)
    with pytest.raises(IllegalActionError):
        repo.set_needs_review(bet.id, False)
    with pytest.raises(IllegalActionError):
        repo.correct_bet(bet.id, reason="x", notes="x")
    with pytest.raises(ValidationError):
        repo.set_needs_review(bet.id, 1)  # type: ignore[arg-type]


# ------------------------------------------------------------------ venues / offers


def test_update_venue_unset_leaves_fields_and_none_clears(repo: Repository):
    betfair = repo.find_venue("Betfair Exchange")
    assert betfair is not None
    updated = repo.update_venue(betfair.id, default_commission_bp=200)
    assert (updated.default_commission_bp, updated.min_stake_pence, updated.ladder) == (
        200,
        200,
        "BETFAIR",
    )
    cleared = repo.update_venue(betfair.id, min_stake_pence=None, ladder=None)
    assert (cleared.min_stake_pence, cleared.ladder) == (None, None)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"default_commission_bp": 10000},
        {"default_commission_bp": -1},
        {"min_stake_pence": -5},
        {"ladder": "SMARKETS"},
        {"name": "  "},
    ],
)
def test_update_venue_validates(repo: Repository, kwargs):
    smarkets = repo.find_venue("Smarkets")
    assert smarkets is not None
    with pytest.raises(ValidationError):
        repo.update_venue(smarkets.id, **kwargs)


def test_rename_and_delete_venues(repo: Repository):
    bet = repo.create_bet(f1_bet(legs=[NewLeg(Side.BACK, "Bet 365", D("2.0"), 1000), F1_LEGS[1]]))
    typo = repo.find_venue("Bet 365")
    assert typo is not None
    with pytest.raises(IllegalActionError):
        repo.update_venue(typo.id, name="bet365")  # clashes with the seeded venue
    with pytest.raises(IllegalActionError):
        repo.delete_venue(typo.id)  # a leg uses it
    with pytest.raises(IllegalActionError):
        repo.update_venue(typo.id, kind=VenueKind.EXCHANGE)
    repo.update_venue(typo.id, name="Bet365 (old account)")
    assert repo.get_bet(bet.id).bookmaker == "Bet365 (old account)"
    unused = repo.ensure_venue("Never Used", VenueKind.BOOKMAKER)
    assert repo.venue_usage(unused.id) == 0
    repo.update_venue(unused.id, kind=VenueKind.EXCHANGE)
    repo.delete_venue(unused.id)
    assert repo.find_venue("Never Used") is None
    with pytest.raises(NotFoundError):
        repo.delete_venue(unused.id)


def test_create_offer_checks_the_venue(repo: Repository):
    with pytest.raises(NotFoundError):
        repo.create_offer("t", venue_id="nope")
    with pytest.raises(ValidationError):
        repo.create_offer(None)  # type: ignore[arg-type]


# ------------------------------------------------------------------ list_bets


def test_list_bets_combines_filters_and_searches_selection_and_market(repo: Repository, clock):
    target = repo.create_bet(f1_bet(event_name="Leeds v Villa", selection="Leeds", market="BTTS"))
    clock.advance(days=1)
    repo.create_bet(f1_bet(event_name="Leeds v Spurs"))
    clock.advance(days=1)
    repo.create_bet(snr_bet(market="BTTS"))
    combined = BetFilter(
        statuses=frozenset({BetStatus.OPEN}),
        bet_types=frozenset({BetType.QUALIFYING}),
        bookmaker="Bet365",
        exchange="Smarkets",
        text="btts",
        placed_from=target.placed_at,
        placed_to=target.placed_at + timedelta(hours=1),
    )
    assert [b.id for b in repo.list_bets(combined)] == [target.id]
    assert {b.event_name for b in repo.list_bets(BetFilter(text="leeds"))} == {
        "Leeds v Villa",
        "Leeds v Spurs",
    }
    assert len(repo.list_bets(BetFilter(text="btts"))) == 2
    assert len(repo.list_bets(BetFilter(limit=1))) == 1
    assert repo.list_bets(BetFilter(limit=1))[0].bet_type is BetType.FREE_BET_SNR


def test_bet_rows_are_complete_across_chunks(repo: Repository, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(repository_module, "_CHUNK", 2)
    ids = [repo.create_bet(f1_bet()).id for _ in range(5)]
    repo.settle_two_way(ids[0], TwoWayOutcome.BACK_WON)
    repo.adjust(ids[0], 0, "oldest one")
    bets = repo.list_bets()
    assert all(len(b.legs) == 2 for b in bets)
    assert {b.id for b in bets if b.has_adjustments} == {ids[0]}


# ------------------------------------------------------------------ summary


def test_summary_excludes_deleted_open_bets_and_settled_lays(repo: Repository):
    a = repo.create_bet(f1_bet())
    b = repo.create_bet(snr_bet())
    before = repo.summary()
    assert (before.open_count, before.open_guaranteed) == (2, -58 + 471)
    repo.soft_delete(a.id)
    after = repo.summary()
    assert (after.open_count, after.open_guaranteed) == (1, 471)
    assert after.open_liability_by_exchange == {"Smarkets": 529}
    repo.settle(b.id, {1: LegResult.WON})  # only the lay settled
    assert repo.summary().open_liability_by_exchange == {}


def test_summary_month_boundaries_are_exact(repo: Repository, clock: FakeClock):
    instants = [
        "2026-08-31T22:59:59Z",
        "2026-08-31T23:00:00Z",
        "2026-09-30T22:59:59Z",
        "2026-09-30T23:00:00Z",
    ]
    for text in instants:
        clock.now = datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        bet = repo.create_bet(f1_bet())
        repo.settle_two_way(bet.id, TwoWayOutcome.BACK_WON)
    september = repo.summary(now=datetime(2026, 9, 15, tzinfo=UTC))
    assert september.realised_this_month == -58 * 2
    assert repo.summary(now=datetime(2026, 8, 15, tzinfo=UTC)).realised_this_month == -58
    assert repo.summary(now=datetime(2026, 10, 15, tzinfo=UTC)).realised_this_month == -58
    with pytest.raises(ValidationError):
        repo.summary(now=datetime(2026, 9, 15))


# ------------------------------------------------------------------ backfill


def test_partial_backfill_has_no_settled_at(repo: Repository):
    bet = repo.create_bet(
        f1_bet(
            settled_at=datetime(2026, 8, 1, tzinfo=UTC),
            legs=[NewLeg(Side.BACK, "Bet365", D("2.00"), 1000, result=LegResult.WON), F1_LEGS[1]],
        )
    )
    assert (bet.status, bet.settled_at, bet.actual_pl_pence) == (
        BetStatus.PARTIALLY_SETTLED,
        None,
        None,
    )
    assert repo.audit_trail(bet.id)[0].new_value == "backfill"


def test_backfill_with_cashed_out_leg(repo: Repository):
    bet = repo.create_bet(
        f1_bet(
            legs=[
                NewLeg(
                    Side.BACK,
                    "Bet365",
                    D("2.00"),
                    1000,
                    result=LegResult.CASHED_OUT,
                    settled_amount=1000,
                ),
                NewLeg(
                    Side.LAY, "Smarkets", D("2.10"), 962, commission_bp=200, result=LegResult.WON
                ),
            ]
        )
    )
    assert bet.actual_pl_pence == 1943
    assert bet.expected_pl_pence == -58


# ------------------------------------------------------------------ export


def test_export_columns_for_an_adjusted_cashed_out_bet(repo: Repository):
    bet = repo.create_bet(
        f1_bet(
            event_at=datetime(2026, 9, 13, 15, tzinfo=UTC),
            legs=[NewLeg(Side.BACK, "Bet365", D("2.375"), 1000, odds_text="11/8"), F1_LEGS[1]],
        )
    )
    repo.settle(bet.id, {0: (LegResult.CASHED_OUT, 1000)})
    repo.settle(bet.id, {1: LegResult.WON})
    repo.adjust(bet.id, 2000, "x")
    repo.set_needs_review(bet.id, True)
    out = io.StringIO()
    write_csv(repo, out)
    rows = [
        dict(zip(CSV_COLUMNS, r, strict=True))
        for r in list(csv.reader(io.StringIO(out.getvalue())))[1:]
    ]
    back, lay = rows
    assert (back["has_adjustments"], back["needs_review"], back["deleted"]) == ("1", "1", "0")
    assert (back["override_pl"], back["effective_pl"], back["actual_pl"]) == (
        "20.00",
        "20.00",
        "19.43",
    )
    assert (back["settled_amount"], lay["settled_amount"]) == ("10.00", "")
    assert back["odds_text"] == "11/8"
    assert back["event_at"] == "2026-09-13T15:00:00Z"


def test_export_include_deleted_and_failure_keeps_existing_file(
    repo: Repository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    keep = repo.create_bet(f1_bet())
    gone = repo.create_bet(f1_bet())
    repo.soft_delete(gone.id)
    target = tmp_path / "ledger.csv"
    assert export_csv(repo, target) == 2
    assert export_csv(repo, target, BetFilter(include_deleted=True)) == 4
    previous = target.read_bytes()

    def boom(_filter=None):
        raise RuntimeError("query failed")

    monkeypatch.setattr(repo, "list_bets", boom)
    with pytest.raises(RuntimeError):
        export_csv(repo, target)
    assert target.read_bytes() == previous
    assert not any(p.name.endswith(".tmp") for p in tmp_path.iterdir())
    assert keep.id


# ------------------------------------------------------------------ helpers


def test_clone_helpers_copy_parameters_not_results(repo: Repository):
    bet = repo.create_bet(f1_bet(notes="clone me"))
    repo.settle_two_way(bet.id, TwoWayOutcome.BACK_WON)
    clone = repo.create_bet(NewBet.from_record(repo.get_bet(bet.id)))
    assert clone.id != bet.id
    assert clone.status is BetStatus.OPEN
    assert (clone.expected_pl_pence, clone.bookmaker, clone.exchange, clone.notes) == (
        -58,
        "Bet365",
        "Smarkets",
        "clone me",
    )
    assert [r.leg.commission_bp for r in clone.legs] == [0, 200]
    kept = NewLeg.from_leg(repo.get_bet(bet.id).legs[0].leg, keep_result=True)
    assert kept.result is LegResult.WON


def test_every_audited_field_has_a_display_kind(repo: Repository):
    bet = repo.create_bet(dutch(offer_id=None))
    repo.edit_bet(
        bet.id,
        market_outcomes=4,
        event_at=datetime(2026, 9, 20, tzinfo=UTC),
        notes="n",
        legs=[*dutch().legs, NewLeg(Side.BACK, "Unibet", D("9.0"), 100, selection=3)],
    )
    repo.settle(
        bet.id,
        {0: (LegResult.CASHED_OUT, 5), 1: LegResult.LOST, 2: LegResult.LOST, 3: LegResult.WON},
    )
    repo.adjust(bet.id, 1, "x")
    repo.set_needs_review(bet.id, True)
    repo.correct_bet(bet.id, reason="date", settled_at=datetime(2026, 9, 1, tzinfo=UTC))
    repo.reopen(bet.id, [0])
    repo.soft_delete(bet.id)
    repo.restore(bet.id)
    text_fields = {"notes", "event_name", "selection", "market", "offer_id", "parent_bet_id"}
    for row in repo.audit_trail(bet.id):
        kind = audit_value_kind(row.field)
        if row.field is None:
            assert kind is AuditValueKind.NONE
            continue
        is_text = (
            row.field in text_fields
            or row.field.endswith((".venue", ".odds_text"))
            or (row.field.startswith("legs[") and "." not in row.field)
        )
        assert (kind is AuditValueKind.TEXT) == is_text, row.field
